import json
import logging
import os
import time
from enum import StrEnum
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from interact.benchmarks.published import (
    PublishedEntry,
    PublishedTable,
)
from interact.data import PackageData
from interact.formats import CoordFormat

_log = logging.getLogger(__name__)

def _litellm():
    """Import litellm lazily — it costs ~2.5s to import, and only the litellm-fallback
    registry path and validate_environment need it; the common models.json path does not.
    Importing it at module top made every `import interact.models` (status, doctor,
    providers, the TUI worker) pay that cost up front."""
    import importlib

    try:
        return importlib.import_module("litellm")
    except ImportError:  # pragma: no cover
        return None

ModelRole = Literal["image", "component", "video"]


class ModelSpec(BaseModel):
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    supports_response_schema: bool = False
    # Capability tags carried from the upstream catalog (litellm) into models.json, so a
    # model's behaviour (e.g. its grounding default) is DERIVED from a live source, not a
    # hardcoded list. See generate-models.py for how these are sourced.
    capabilities: list[str] = Field(default_factory=list)


class ProviderSpec(BaseModel):
    model_config = {"populate_by_name": True}

    env_keys: list[str] = Field(default_factory=list, alias="envKeys")
    models: dict[str, ModelSpec] = {}


class ModelsConfig(BaseModel):
    """Typed schema for models.json content."""

    model_config = {"populate_by_name": True}

    providers: dict[str, ProviderSpec] = {}
    recommendations: dict[str, list[str]] = {}
    coord_formats: dict[str, dict] = Field(default_factory=dict, alias="coordFormats")
    defaults: dict[str, str] = {}


class ModelCapability(StrEnum):
    LLM = "llm"
    VLM = "vlm"
    GUI_GROUNDING = "gui_grounding"
    # Native computer-use: the model emits click coordinates directly (Anthropic/OpenAI
    # computer-use tool). Sourced from litellm's `supports_computer_use` — not hardcoded.
    COMPUTER_USE = "computer_use"
    VIDEO = "video"  # native video input — from litellm's `supports_video_input`


class RegistryMixin:
    """Per-subclass registry. Each subclass gets its own ``_registry`` list."""

    _registry: ClassVar[list]

    def __init_subclass__(cls, **kw: object) -> None:
        super().__init_subclass__(**kw)
        cls._registry = []

    @classmethod
    def registry(cls) -> list[Self]:
        return cls._registry  # type: ignore[return-value]

    @classmethod
    def by_id(cls, id: str) -> "Self | None":
        return next((x for x in cls._registry if x.id == id), None)

    @classmethod
    def _register(cls, item: Self) -> None:
        for i, ex in enumerate(cls._registry):
            if ex.id == item.id:
                cls._registry[i] = item
                return
        cls._registry.append(item)

    @classmethod
    def _reset(cls) -> None:
        cls._registry.clear()


class Model(RegistryMixin, BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    id: str
    provider: str
    capabilities: set[ModelCapability]
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    supports_structured_output: bool = False
    coord_format: CoordFormat | None = None

    # provider -> required env keys, populated from models.json at load time
    _provider_keys: ClassVar[dict[str, list[str]]] = {}
    # ordered component (UI-grounding) model recommendations, from the loaded config
    _component_recommendations: ClassVar[list[str]] = []

    def can(self, cap: ModelCapability) -> bool:
        return cap in self.capabilities

    def grounding_strategy(self) -> str:
        """How this model should be driven to act on a target — DERIVED from its capabilities,
        not a hardcoded per-model list (the default sits at the bottom of the provider→model
        override hierarchy):

        - ``"coords"``  — the model emits click coordinates itself (native computer-use, or a
          known GUI-grounding box convention). Fewest round-trips.
        - ``"ref_list"`` — give it the DOM/accessibility ref list and let it pick (the safe
          default; works for every model including non-grounding ones, no VLM cost).

        Computer-use is the strongest signal (the model literally returns click points), then a
        registered grounding box-format; otherwise refs. An explicit per-call / per-config
        override layers on top of this default."""
        if self.can(ModelCapability.COMPUTER_USE):
            return "coords"
        if self.can(ModelCapability.GUI_GROUNDING):
            return "coords"
        return "ref_list"

    def litellm_id(self) -> str:
        return self.id

    def is_available(self) -> bool:
        """Whether this model's API key is present in the environment.

        A **pure env-var check** against the provider's declared ``envKeys`` — never a
        ``litellm.validate_environment`` call, which can BLOCK on an interactive provider
        auth flow / network (it hung CI here). A provider absent from the catalog (no
        declared keys) can't be confirmed without that call, so it's treated unavailable.
        Keyless providers (``envKeys: []``, e.g. a local Ollama) are available.
        """
        keys = Model._provider_keys.get(self.provider)
        if not keys:
            # None (unknown provider) OR [] (no declared API key). Empty means we can't
            # confirm a non-interactive credential — e.g. the `chatgpt` provider has no
            # key env var and litellm would trigger an interactive device-code OAuth poll
            # that BLOCKS FOREVER. Never auto-select such a provider (this hung CI and would
            # hang a real server's fallback chain). Explicitly-configured models still run —
            # is_available only filters automatic fallback candidates.
            return False
        return all(os.environ.get(k) for k in keys)

    @property
    def cost_score(self) -> float:
        return Model.cost_of(self.input_cost_per_million, self.output_cost_per_million)

    @staticmethod
    def cost_of(input_cost: float | None, output_cost: float | None) -> float:
        """Sum of input/output cost-per-million; missing values count as 0."""
        return (input_cost or 0.0) + (output_cost or 0.0)

    @staticmethod
    def quality_per_dollar(score: float, cost: float | None) -> float | None:
        """Score divided by total cost; returns None when cost is missing or zero."""
        if cost is None or cost <= 0:
            return None
        return score / cost

    @classmethod
    def match_published(cls, name: str) -> "Model | None":
        """Case-insensitive substring match of a published model name against the registry."""
        needle = name.lower()
        for m in cls.registry():
            bare = m.id.split("/", 1)[-1].lower()
            if needle in bare or bare in needle:
                return m
        return None

    @classmethod
    def by_capability(
        cls, cap: ModelCapability, available_only: bool = True
    ) -> list[Self]:
        results = [m for m in cls._registry if m.can(cap)]
        if available_only:
            results = [m for m in results if m.is_available()]
        results.sort(key=lambda m: m.cost_score)
        return results  # type: ignore[return-value]

    @classmethod
    def cheapest(cls, cap: ModelCapability) -> Self | None:
        models = cls.by_capability(cap, available_only=True)
        return models[0] if models else None  # type: ignore[return-value]

    @classmethod
    def _from_litellm_cost(cls, model_id: str, provider: str, cost_entry: dict) -> Self:
        """Build a Model from a litellm model_cost entry."""
        caps: set[ModelCapability] = set()
        if cost_entry.get("supports_vision"):
            caps.add(ModelCapability.VLM)
        if cost_entry.get("supports_computer_use"):  # litellm flag → native coordinate output
            caps.add(ModelCapability.COMPUTER_USE)
        if cost_entry.get("supports_video_input"):
            caps.add(ModelCapability.VIDEO)
        fmt = CoordFormat.for_model(model_id)
        if fmt != CoordFormat():
            caps.add(ModelCapability.GUI_GROUNDING)
        return cls(
            id=model_id,
            provider=provider,
            capabilities=caps,
            input_cost_per_million=cost_entry.get("input_cost_per_token", 0) * 1_000_000
            if cost_entry.get("input_cost_per_token")
            else None,
            output_cost_per_million=cost_entry.get("output_cost_per_token", 0)
            * 1_000_000
            if cost_entry.get("output_cost_per_token")
            else None,
            supports_structured_output=bool(cost_entry.get("supports_response_schema")),
            coord_format=fmt if fmt != CoordFormat() else None,
        )  # type: ignore[return-value]

    @classmethod
    def from_litellm_id(cls, model_id: str) -> Self:
        existing = cls.by_id(model_id)
        if existing is not None:
            return existing  # type: ignore[return-value]
        lm = _litellm()
        if lm is None:
            return cls(
                id=model_id, provider="unknown", capabilities={ModelCapability.VLM}
            )  # type: ignore[return-value]
        cost_entry = lm.model_cost.get(model_id, {})
        provider = cost_entry.get("litellm_provider", "unknown")
        model = cls._from_litellm_cost(model_id, provider, cost_entry)
        cls._register(model)
        return model  # type: ignore[return-value]

    @classmethod
    def _load_from_json(cls, raw: str) -> None:
        """Populate registry from INTERACT_MODELS_JSON."""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            _log.warning("Invalid INTERACT_MODELS_JSON, skipping")
            return

        models_config = ModelsConfig.model_validate(data)
        cls._provider_keys = {
            name: spec.env_keys for name, spec in models_config.providers.items()
        }
        cls._component_recommendations = list(
            models_config.recommendations.get("component", [])
        )
        component_recs = set(cls._component_recommendations)

        for provider_name, provider_spec in models_config.providers.items():
            for model_id, model_spec in provider_spec.models.items():
                caps: set[ModelCapability] = {ModelCapability.VLM}
                fmt = cls._match_coord_format(model_id, models_config.coord_formats)
                if fmt is not None or model_id in component_recs:
                    caps.add(ModelCapability.GUI_GROUNDING)
                # Capability tags carried from the upstream catalog (litellm) — e.g.
                # computer_use / video — so behaviour derives from a live source, not a
                # second hardcoded list. Unknown tags are ignored.
                for tag in model_spec.capabilities:
                    try:
                        caps.add(ModelCapability(tag))
                    except ValueError:
                        pass
                cls._register(
                    cls(
                        id=model_id,
                        provider=provider_name,
                        capabilities=caps,
                        input_cost_per_million=model_spec.input_cost_per_million,
                        output_cost_per_million=model_spec.output_cost_per_million,
                        supports_structured_output=model_spec.supports_response_schema,
                        coord_format=fmt,
                    )
                )

    @classmethod
    def _load_from_litellm(cls) -> None:
        """Populate registry from litellm.model_cost."""
        lm = _litellm()
        if lm is None:
            _log.warning("litellm not available, registry empty")
            return
        for model_id, info in lm.model_cost.items():
            if not info.get("supports_vision"):
                continue
            provider = info.get("litellm_provider", "unknown")
            cls._register(cls._from_litellm_cost(model_id, provider, info))

    @staticmethod
    def _match_coord_format(model_id: str, coord_formats: dict) -> CoordFormat | None:
        model_lower = model_id.lower()
        for prefix, spec in coord_formats.items():
            if model_lower.startswith(prefix.lower()):
                return CoordFormat(**spec)
        return None

    @classmethod
    def hydrate_measured(cls, grounding_json: str) -> None:
        """Populate Benchmark._measured from an injected grounding-scores blob."""
        try:
            data = json.loads(grounding_json)
        except (ValueError, TypeError):
            _log.warning("Invalid grounding JSON, skipping hydration")
            return
        if not isinstance(data, dict):
            return
        for model_id, result in data.items():
            if not isinstance(result, dict):
                continue
            dataset = result.get("dataset", "")
            bench_id = _DATASET_TO_BENCHMARK.get(dataset)
            score = result.get("overall_accuracy")
            if bench_id is None or score is None:
                continue
            bench = Benchmark.by_id(bench_id)
            if bench is None:
                continue
            bench._measured[model_id] = float(score)

    @classmethod
    def component_recommendations(cls) -> list[str]:
        """Ordered curated UI-grounding model ids from the loaded ``models.json``."""
        return cls._component_recommendations

    @classmethod
    def available_providers(cls) -> list[str]:
        """Catalog providers whose declared API key(s) are all set in the environment.

        Providers that declare no key are omitted — there is nothing to configure,
        so their presence is not evidence the user has set anything up.
        """
        return sorted(
            provider
            for provider, keys in cls._provider_keys.items()
            if keys and all(os.environ.get(k) for k in keys)
        )

    @classmethod
    def available_by_capability(cls, cap: ModelCapability) -> list[Self]:
        """Models with ``cap`` from providers whose API keys are configured, cheapest first."""
        available = set(cls.available_providers())
        models = [m for m in cls._registry if m.can(cap) and m.provider in available]
        models.sort(key=lambda m: m.cost_score)
        return models  # type: ignore[return-value]

    @classmethod
    def recommended_grounding(cls) -> list[Self]:
        """Configured grounding models in preference order.

        Cheapest-first surfaces free general/image-gen VLMs that mislocate boxes,
        so rank: curated ``recommendations.component`` (models tuned for UI
        grounding) → ScreenSpot-scored → remaining grounding models by cost. Only
        providers whose key is set are included. Used by ``interact detect`` and
        the dashboard so "best grounding model" is defined in one place.
        """
        seen: set[str] = set()
        ranked: list[Self] = []
        configured = set(cls.available_providers())

        def add(model: "Model | None") -> None:
            if (
                model is not None
                and model.id not in seen
                and model.can(ModelCapability.GUI_GROUNDING)
                and model.provider in configured
            ):
                seen.add(model.id)
                ranked.append(model)  # type: ignore[arg-type]

        for model_id in cls._component_recommendations:
            add(cls.by_id(model_id))
        bench = Benchmark.by_id("screenspot")
        if bench is not None:
            for rec in bench.recommend(prefer="both", available_only=True):
                add(rec.model)
        for model in cls.available_by_capability(ModelCapability.GUI_GROUNDING):
            add(model)
        return ranked

    @classmethod
    def load_registry(cls, models_json: str | None = None) -> None:
        """Populate the registry from models.json, falling back to litellm.

        Source order: the ``models_json`` argument → ``INTERACT_MODELS_JSON`` →
        the catalog bundled in :mod:`interact.data` → ``litellm.model_cost``.
        Measured grounding scores are hydrated from :meth:`PackageData.grounding_raw`.
        """
        cls._reset()
        cls._provider_keys = {}
        cls._component_recommendations = []
        raw = models_json or PackageData.models_raw()
        if raw:
            cls._load_from_json(raw)
        else:
            cls._load_from_litellm()
        grounding_raw = PackageData.grounding_raw()
        if grounding_raw:
            cls.hydrate_measured(grounding_raw)


__all__ = [
    "Benchmark",
    "BenchmarkRecommendation",
    "CircuitBreaker",
    "Model",
    "ModelCapability",
    "ModelChain",
    "ModelSpec",
    "ModelsConfig",
    "ProviderSpec",
    "PublishedEntry",
    "PublishedTable",
    "RegistryMixin",
]


class Benchmark(RegistryMixin, BaseModel):
    """A benchmark for evaluating VLM capability.

    Scores come from published online leaderboards (:attr:`published`). Optional
    measured scores — injected via ``INTERACT_GROUNDING_JSON`` (e.g. fetched from an
    online source), never from our own paid eval — live in :attr:`_measured`.
    """

    id: str
    name: str
    description: str
    # Which capability the benchmark measures, so the UI can group + explain by task.
    category: Literal["image", "gui_grounding", "video"] = "gui_grounding"
    metric: str = "accuracy"
    url: str = ""
    published: PublishedTable | None = None

    _measured: dict[str, float] = PrivateAttr(default_factory=dict)

    def score_for(self, model: "Model") -> float | None:
        """Measured [0, 1] score for ``model``, or None if not evaluated."""
        return self._measured.get(model.id)

    def quality_per_dollar(self, model: "Model") -> float | None:
        score = self.score_for(model)
        if score is None:
            return None
        return Model.quality_per_dollar(score, model.cost_score)

    def measured_scores(self) -> dict[str, float]:
        return dict(self._measured)

    def published_models_in_registry(self) -> list[tuple["Model", float]]:
        """Published entries whose model_name resolves to a registered Model."""
        if self.published is None:
            return []
        out: list[tuple[Model, float]] = []
        for entry in self.published.entries:
            m = Model.match_published(entry.model_name)
            if m is not None:
                out.append((m, entry.score))
        return out

    def lib_recommendation_model(self) -> "Model | None":
        if self.published is None or not self.published.lib_recommendation:
            return None
        return Model.match_published(self.published.lib_recommendation)

    def recommend(
        self,
        *,
        prefer: Literal["published", "measured", "both"] = "both",
        available_only: bool = True,
        min_score: float = 0.0,
        top_n: int | None = None,
    ) -> "list[BenchmarkRecommendation]":
        rows: list[BenchmarkRecommendation] = []

        if prefer in ("published", "both") and self.published is not None:
            for entry in self.published.entries:
                m = Model.match_published(entry.model_name)
                if m is None:
                    continue
                if available_only and not m.is_available():
                    continue
                if entry.score < min_score:
                    continue
                rows.append(
                    BenchmarkRecommendation(
                        benchmark=self,
                        model=m,
                        source="published",
                        rank=0,
                        score=entry.score,
                    )
                )

        if prefer in ("measured", "both"):
            for model_id, score in self._measured.items():
                m = Model.by_id(model_id)
                if m is None:
                    continue
                if available_only and not m.is_available():
                    continue
                if score < min_score:
                    continue
                rows.append(
                    BenchmarkRecommendation(
                        benchmark=self,
                        model=m,
                        source="measured",
                        rank=0,
                        score=score,
                    )
                )

        def sort_key(r: "BenchmarkRecommendation") -> tuple[float, float]:
            qpd = r.quality_per_dollar or 0.0
            return (qpd, r.score)

        rows.sort(key=sort_key, reverse=True)
        if top_n is not None:
            rows = rows[:top_n]
        for i, r in enumerate(rows, 1):
            r.rank = i
        return rows


class BenchmarkRecommendation(BaseModel):
    """A ranked model recommendation for a specific benchmark.

    Score is materialised because the same model can appear with both a
    published and a measured score that differ.
    """

    # field name `model` collides with pydantic v2 protected namespace; silence it
    model_config = ConfigDict(protected_namespaces=())

    benchmark: Benchmark
    model: Model
    source: Literal["published", "measured"]
    rank: int
    score: float

    @property
    def quality_per_dollar(self) -> float | None:
        return Model.quality_per_dollar(self.score, self.model.cost_score)

    @property
    def cost_per_million(self) -> float | None:
        return self.model.cost_score or None


class CircuitBreaker:
    """Track failed models with TTL-based cooldown."""

    def __init__(self, ttl: float = 300.0):
        self._trips: dict[str, float] = {}
        self._ttl = ttl

    def tripped(self, model_id: str) -> bool:
        ts = self._trips.get(model_id)
        if ts is None:
            return False
        if time.monotonic() - ts >= self._ttl:
            del self._trips[model_id]
            return False
        return True

    def trip(self, model_id: str) -> None:
        self._trips[model_id] = time.monotonic()

    def clear(self) -> None:
        self._trips.clear()


class ModelChain(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    role: ModelRole
    preferences: list[Model]

    def active(self, breaker: CircuitBreaker | None = None) -> Model | None:
        for model in self.preferences:
            if breaker and breaker.tripped(model.id):
                continue
            if not model.is_available():
                continue
            return model
        return None

    @classmethod
    def from_config(
        cls, role: ModelRole, configured_model: str, recommendations: list[str]
    ) -> Self:
        seen: set[str] = set()
        preferences: list[Model] = []

        for model_id in [configured_model] + recommendations:
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            preferences.append(Model.from_litellm_id(model_id))

        # Add cheapest available VLM as final fallback
        cheapest = Model.cheapest(ModelCapability.VLM)
        if cheapest and cheapest.id not in seen:
            preferences.append(cheapest)

        return cls(role=role, preferences=preferences)  # type: ignore[return-value]


_ = PublishedTable  # re-exported via __all__; reference here silences unused-import lint

# Pre-register known grounding benchmarks at import time.
# Published tables for these benchmarks live in interact.benchmarks.published.
# GUI grounding — "can the model point at the right on-screen element to click?"
Benchmark._register(
    Benchmark(
        id="screenspot",
        name="ScreenSpot",
        category="gui_grounding",
        description=(
            "GUI grounding: given an instruction, click the right single element across "
            "iOS/Android/macOS/Windows/Web. Measures whether a model can localize where to act."
        ),
        url="https://huggingface.co/datasets/rootsautomation/ScreenSpot",
        published=PublishedTable.load("screenspot"),
    )
)
Benchmark._register(
    Benchmark(
        id="screenspot_pro",
        name="ScreenSpot-Pro",
        category="gui_grounding",
        description=(
            "Hard GUI grounding on professional high-resolution apps (23 apps, 5 industries, "
            "3 OSes) with tiny cluttered targets — the closest benchmark to real desktop automation."
        ),
        url="https://huggingface.co/datasets/TIGER-Lab/ScreenSpot-Pro",
        published=PublishedTable.load("screenspot_pro"),
    )
)
# Image understanding — "how well does the model reason over a static image?"
Benchmark._register(
    Benchmark(
        id="mmmu",
        name="MMMU",
        category="image",
        description=(
            "College-exam-level multi-discipline reasoning over diagrams, charts and figures "
            "(14 disciplines) — the headline image-understanding benchmark."
        ),
        url="https://mmmu-benchmark.github.io/",
        published=PublishedTable.load("mmmu"),
    )
)
Benchmark._register(
    Benchmark(
        id="mmbench",
        name="MMBench",
        category="image",
        description=(
            "Broad multiple-choice perception + reasoning over images (EN/CN), with "
            "robustness checks — a wide general image-understanding measure."
        ),
        url="https://github.com/open-compass/MMBench",
        published=PublishedTable.load("mmbench"),
    )
)
# Video understanding — "can the model reason over time across frames?"
Benchmark._register(
    Benchmark(
        id="video_mme",
        name="Video-MME",
        category="video",
        description=(
            "Full-spectrum video understanding: 900 videos (11s–1hr) across 6 domains with "
            "2,700 QA pairs — the canonical video benchmark."
        ),
        url="https://video-mme.github.io/",
        published=PublishedTable.load("video_mme"),
    )
)
Benchmark._register(
    Benchmark(
        id="mvbench",
        name="MVBench",
        category="video",
        description=(
            "20 temporal-reasoning tasks (action/sequence understanding) that can't be solved "
            "from a single frame — tests genuine video, not stills."
        ),
        url="https://github.com/OpenGVLab/Ask-Anything",
        published=PublishedTable.load("mvbench"),
    )
)


# Canonical dataset name → Benchmark.id mapping. Source of truth for the
# eval pipeline; do not add string aliases — match exactly the HF dataset id.
_DATASET_TO_BENCHMARK: dict[str, str] = {
    "rootsautomation/ScreenSpot": "screenspot",
    "TIGER-Lab/ScreenSpot-Pro": "screenspot_pro",
}
