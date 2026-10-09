import re
import json
import logging
import os
import time
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from galaius.benchmarks.published import (
    PublishedEntry,
    PublishedTable,
)
from galaius.data import PackageData
from galaius.formats import CoordFormat

if TYPE_CHECKING:
    from galaius.ollama import OllamaModel

_log = logging.getLogger(__name__)

def _litellm():
    """Import litellm lazily — costs ~2.5s, and only the litellm-fallback registry path and
    validate_environment need it; the common models.json path doesn't. Importing at module top
    made every `import galaius.models` (status, doctor, providers, TUI worker) pay that cost
    up front."""
    import importlib

    try:
        return importlib.import_module("litellm")
    except ImportError:  # pragma: no cover
        return None

ModelRole = Literal["image", "component", "video", "audio"]


class ModelSpec(BaseModel):
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    #: Prompt tokens served FROM the provider's prompt cache, and tokens WRITTEN to it (litellm's
    #: `cache_read_input_token_cost` / `cache_creation_input_token_cost`, default cache lifetime).
    #: None = the source quotes no cache rate for this model, never "same as input".
    cache_read_cost_per_million: float | None = None
    cache_write_cost_per_million: float | None = None
    supports_response_schema: bool = False
    #: How strong the model is, on the catalog's own scale. Present in models.json all along,
    #: dropped on load — why nothing at runtime could order a chain by quality.
    intelligence_score: float | None = None
    # Capability tags carried from upstream catalog (litellm) into models.json, so a model's
    # behaviour (e.g. grounding default) is DERIVED from a live source, not a hardcoded list.
    # See generate-models.py for how these are sourced.
    capabilities: list[str] = Field(default_factory=list)
    #: What the model PRODUCES, in litellm's words (chat, responses, image_generation,
    #: video_generation, embedding, audio_speech, audio_transcription, realtime); None = unknown.
    mode: str | None = None


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
    VIDEO = "video"  # native video input — see is_native_video_model
    AUDIO = "audio"  # audio understanding / transcription — see is_audio_model


# Native video INPUT and audio understanding are per-FAMILY facts litellm's
# `supports_video_input`/`supports_audio_input` flags do NOT reliably populate (return nothing
# for every current model), so — like CoordFormat's grounding table — these are curated
# substring tables grounded in each provider's own docs (web-verified 2026-06-24):
#   • Native video input: Gemini (all 2.x/3.x, incl. YouTube URLs), Qwen-VL/Qwen3-VL, InternVL,
#     LLaVA-Video, Amazon Nova Lite/Pro/Premier. OpenAI + Anthropic are FRAMES-ONLY (no native
#     video media type) — galaius still drives them by ffmpeg-sampling a recording to images,
#     but they aren't "video models".
#   • Audio understanding/transcription: Gemini, GPT-4o-audio/-transcribe, Whisper, Qwen-Omni.
#     (Anthropic has no audio input.)
# Substring match against the litellm id (provider prefix included), case-insensitive.
_VIDEO_FAMILIES: tuple[str, ...] = (
    "gemini",
    "qwen2-vl", "qwen2.5-vl", "qwen3-vl", "qwen-vl", "qwenvl",
    "internvl",
    "llava-video", "llava-next-video",
    "nova-lite", "nova-pro", "nova-premier",  # Nova Micro is text-only — match the video tiers
)
_AUDIO_FAMILIES: tuple[str, ...] = (
    "gemini",
    "gpt-4o-audio", "gpt-4o-mini-audio", "gpt-audio",
    "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper",
    "qwen-omni", "qwen2.5-omni", "qwen3-omni", "qwen2-audio",
)

# Generation/TTS/embedding variants of a family (e.g. gemini-*-image-preview, *-tts, imagen,
# veo) OUTPUT media or vectors — they don't UNDERSTAND video/audio input, so excluded from that
# capability even though the name carries the family substring.
_NOT_UNDERSTANDING: tuple[str, ...] = (
    "-image", "image-preview", "image-generation", "-tts", "-embedding",
    "embedding", "imagen", "veo",
)


def _is_understanding(model_id: str) -> bool:
    lid = model_id.lower()
    return not any(x in lid for x in _NOT_UNDERSTANDING)


def is_native_video_model(model_id: str) -> bool:
    """Whether a model accepts NATIVE video input (a clip), per the curated family table."""
    lid = model_id.lower()
    return _is_understanding(lid) and any(fam in lid for fam in _VIDEO_FAMILIES)


def is_audio_model(model_id: str) -> bool:
    """Whether a model can understand or transcribe AUDIO, per the curated family table."""
    lid = model_id.lower()
    return _is_understanding(lid) and any(fam in lid for fam in _AUDIO_FAMILIES)


# Families litellm sends NATIVE inline video to (a Gemini `inline_data` part) — matched as a
# substring so a bare `gemini-2.5-pro`, `gemini/…` and `vertex_ai/gemini-…` all qualify. Other
# providers have no inline-video transform in litellm and SILENTLY DROP a video content part
# (HTTP 200 + a hallucinated answer, never an error) — so this MUST be a positive allowlist, not
# exception catching. Qwen/DashScope is URL-only through litellm, stays on frame sampling (#48).
_NATIVE_VIDEO_INLINE_FAMILIES: tuple[str, ...] = ("gemini", "vertex_ai")


def supports_native_video_inline(model_id: str) -> bool:
    """Whether litellm will send this model native inline video rather than us ffmpeg-sampling
    frames — a Gemini/Vertex understanding model only, today (#48)."""
    lid = model_id.lower()
    return is_native_video_model(lid) and any(fam in lid for fam in _NATIVE_VIDEO_INLINE_FAMILIES)


# Pure speech-to-text models — transcribe but can't take audio in a chat completion. The
# transcribe tool answers a `query` about one of these over its TRANSCRIPT (via the image
# model), not by routing audio into an acoustic chat call it can't serve.
_TRANSCRIBE_ONLY: tuple[str, ...] = ("whisper", "-transcribe")


def is_transcription_only_model(model_id: str) -> bool:
    lid = model_id.lower()
    return any(t in lid for t in _TRANSCRIBE_ONLY)


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
    def named(cls, catalog_id: str) -> "Self":
        """The row `<catalog provider>/<id>` names (`catalog_id`): this catalog's, else one built from
        litellm's price for that id (None prices when it has none), NOT registered. A ranking made
        elsewhere (this PC's server) can name a model the bundled snapshot never heard of; it carries
        no score, so it never enters a ranking made here."""
        known = next((x for x in cls._registry if x.catalog_id == catalog_id), None)
        if known is not None:
            return known
        provider, _, model_id = catalog_id.partition("/")
        litellm = _litellm()
        cost = dict(getattr(litellm, "model_cost", None) or {}) if litellm is not None else {}
        return cls._from_litellm_cost(model_id, provider, cost.get(model_id) or cost.get(catalog_id) or {})

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


def _ordinal(n: int) -> str:
    """1 → "1st". A rank reads as a rank, not as a second score."""
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }".replace(" ", "")


class TokenMix(BaseModel):
    """How a workload's tokens split between fresh input, cache reads, cache writes and output —
    relative weights, so one price per model can be read for that workload.

    Default 3:1 input:output, the usual chat shape; an agent role's MEASURED mix (its runs' token
    counts) replaces it when there is one — agents read mostly from the prompt cache.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input: float = Field(default=3.0, ge=0)
    cache_read: float = Field(default=0.0, ge=0)
    cache_write: float = Field(default=0.0, ge=0)
    output: float = Field(default=1.0, ge=0)

    @property
    def shares(self) -> tuple[float, float, float, float]:
        """The four weights as fractions of one (input, cache read, cache write, output)."""
        weights = (self.input, self.cache_read, self.cache_write, self.output)
        total = sum(weights)
        return tuple(weight / total for weight in weights)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _some_tokens(self) -> Self:
        if self.input + self.cache_read + self.cache_write + self.output <= 0:
            raise ValueError("a token mix needs at least one token")
        return self

    def __str__(self) -> str:
        names = ("input", "cache read", "cache write", "output")
        return " · ".join(f"{name} {share:.0%}" for name, share in zip(names, self.shares) if share >= 0.005)


class Model(RegistryMixin, BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    id: str
    provider: str
    capabilities: set[ModelCapability]
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    #: Prompt-cache rates, as `ModelSpec` carries them; None = no cache rate quoted (priced as input).
    cache_read_cost_per_million: float | None = None
    cache_write_cost_per_million: float | None = None
    #: Capability score from the catalog — what best-first ordering sorts on. None for a model
    #: nobody has scored, which sorts LAST rather than first: unknown is not the same as good.
    intelligence_score: float | None = None
    supports_structured_output: bool = False
    coord_format: CoordFormat | None = None

    # provider -> required env keys, populated from models.json at load time
    _provider_keys: ClassVar[dict[str, list[str]]] = {}
    # ordered component (UI-grounding) model recommendations, from the loaded config
    _component_recommendations: ClassVar[list[str]] = []
    #: provider -> model ids it was PROVEN to serve, by asking it directly at load time
    #: (currently: a running Ollama daemon answered ``/api/tags``). A declared env key is a
    #: PROXY for "this will run"; an answer from the thing itself outranks the key — what makes
    #: a local Ollama, needing no key at all, usable rather than invisible. Also makes
    #: availability SHARPER not just broader: the bundled catalog lists ~70 Ollama models, and a
    #: key alone made every one look available though the user pulled three. Once the daemon
    #: says what it has, that answer governs.
    _served: ClassVar[dict[str, set[str]]] = {}
    #: coordFormats from the loaded catalog, kept so a model discovered AFTER the JSON pass can be
    #: matched against the same grounding table rather than a second copy of it.
    _coord_formats: ClassVar[dict] = {}

    def can(self, cap: ModelCapability) -> bool:
        return cap in self.capabilities

    def grounding_strategy(self) -> str:
        """How this model should be driven to act on a target — DERIVED from its capabilities,
        not a hardcoded per-model list (default sits at the bottom of the provider→model
        override hierarchy):

        - ``"coords"``  — model emits click coordinates itself (native computer-use, or a known
          GUI-grounding box convention). Fewest round-trips.
        - ``"ref_list"`` — give it the DOM/accessibility ref list, let it pick (safe default;
          works for every model including non-grounding ones, no VLM cost).

        Computer-use is the strongest signal (model literally returns click points), then a
        registered grounding box-format, otherwise refs. An explicit per-call/per-config
        override layers on top."""
        if self.can(ModelCapability.COMPUTER_USE):
            return "coords"
        if self.can(ModelCapability.GUI_GROUNDING):
            return "coords"
        return "ref_list"

    def litellm_id(self) -> str:
        return self.id

    def key_missing(self) -> bool:
        """True only when we can PROVE this model cannot run: its provider is known, declares
        API keys, and they're absent.

        Distinct from ``not is_available()``, also true for a provider we've never heard of — a
        self-hosted endpoint, a local runner, any id outside the catalog. Treating that as
        "cannot run" would let auto-selection quietly override somebody's pinned local model —
        the opposite of respecting a choice they made.
        """
        if self.provider in Model._served:
            # It answered us, so no key to be missing. Deliberately NOT "and it serves this
            # id": key_missing gates whether auto-selection may WALK PAST AN EXPLICIT PIN, and a
            # model the daemon doesn't list today may be one the user is about to pull.
            # Respecting the pin stays the rule; is_available below is where precision belongs.
            return False
        keys = Model._provider_keys.get(self.provider)
        if not keys:
            return False  # unknown provider, or one that needs no key: not our call to overrule
        return not all(os.environ.get(k) for k in keys)

    def competence(self) -> str:
        """This model's capability score, spelled out: the namespaced variable, the number, and
        WHERE it sits among models carrying the same measure.

        "Models should also spell out their intelligence score, such that we can compare the most
        competents and trust one agent more than another in some situations (isn't absolute)" —
        so a bare number is never enough: names its source (two leaderboards rarely agree),
        gives the rank that makes it comparable; a model nobody measured says so, not a zero.
        """
        # WHO measured it comes from the variable registry, which already owns that fact — a
        # literal here would keep announcing this board's name if a second one supplied the same
        # field. Imported inside the method because `galaius.criteria` imports this module at
        # import time: a genuine cycle, not a lazy-loading habit.
        from galaius.criteria import Variables

        variable = Variables.by_name("aa.intelligence")
        source = (variable.source if variable else "") or "an unnamed source"
        if self.intelligence_score is None:
            return "aa.intelligence — not scored"
        # ONE MODEL, ONE RIVAL, the SAME population every selection path uses.
        #
        # Two bugs met here: retired models were counted, ranking this one against models the
        # product refuses to choose from; and every SPELLING of a model counted separately, so a
        # model was "joint 1st" with itself, saying "17th of 697" while the ranked list one
        # command away said "2nd of 433" — two numbers for one fact, the exact disagreement
        # live-board rescoring was written to end.
        from galaius.model_catalog import bare_model_name, live_scores

        # Rank over the BOARD, never over what this machine happens to reach — same population
        # `agents models` ranks over: a denominator of "models you can route to" is a local-
        # availability measure wearing an intelligence label — one model read 10th from one
        # directory, 13th from another with different credentials in scope.
        population = list(live_scores().values())
        if not population:
            # No board on this machine: fall back to what's known, one row per MODEL. Every
            # spelling counted separately made a model "joint 1st" with itself.
            best: dict[str, float] = {}
            for other in self.catalog():
                if other.intelligence_score is None or getattr(other, "retired", False):
                    continue
                key = bare_model_name(other.id)
                best[key] = max(best.get(key, float("-inf")), other.intelligence_score)
            population = list(best.values())
        scored = population
        rank = sum(1 for value in scored if value > self.intelligence_score) + 1
        # Equal scores share ONE place and say so. The panel's twin has said this since written;
        # here two models at one score printed 1st and 2nd, manufacturing exactly the difference
        # the measure denies.
        joint = "joint " if sum(1 for v in scored if v == self.intelligence_score) > 1 else ""
        return (f"aa.intelligence {self.intelligence_score:.1f} — "
                f"{joint}{_ordinal(rank)} of {len(scored)} scored ({source})")

    def is_available(self) -> bool:
        """Whether this model's API key is present in the environment.

        A **pure env-var check** against the provider's declared ``envKeys`` — never a
        ``litellm.validate_environment`` call, which can BLOCK on an interactive provider auth
        flow/network (hung CI here). A provider absent from the catalog (no declared keys)
        can't be confirmed without that call, treated unavailable.

        The one thing outranking the key check is a provider we ALREADY confirmed by asking it
        — see ``_served``. Still a pure local check: the daemon was asked once, at registry
        load, short timeout; this method only reads the answer. Answer is per-MODEL, so a
        catalog row for something the user never pulled is correctly NOT available even though
        its provider is running.
        """
        if self.provider in Model._served:
            return self.is_served()
        keys = Model._provider_keys.get(self.provider)
        if not keys:
            # None (unknown provider) OR [] (no declared API key). Empty means we can't confirm
            # a non-interactive credential — e.g. `chatgpt` has no key env var and litellm
            # would trigger an interactive device-code OAuth poll that BLOCKS FOREVER. Never
            # auto-select such a provider (hung CI, would hang a real server's fallback chain).
            # Explicitly-configured models still run — is_available only filters automatic
            # fallback candidates.
            return False
        return all(os.environ.get(k) for k in keys)

    def is_served(self) -> bool:
        """False only when a LIVE provider itself told us it does not have this model.

        Split out of :meth:`is_available` because the two questions compose differently: this
        one is purely "has the provider ruled it out?", so a caller that already checked the
        provider key (``available_by_capability``) can add it without re-asking the key
        question. Keeping them as one method let the two disagree — ``is_available`` counting 4
        models while ``available_by_capability`` still offered 21 from the same provider.
        """
        served = Model._served.get(self.provider)
        if served is None:
            return True
        # Joined on the NORMALIZED name, like every cross-source comparison here. Ollama spells
        # one model two ways — `kimi-k3` from its cloud endpoint, `kimi-k3:cloud` from the local
        # daemon — whichever answered discovery decided which spelling the set holds, and a pin
        # written in the other read as "not served".
        from galaius.model_catalog import bare_model_name

        mine = bare_model_name(self.id)
        return any(bare_model_name(name) == mine for name in served)

    @property
    def cost_score(self) -> float:
        return Model.cost_of(self.input_cost_per_million, self.output_cost_per_million)

    @property
    def catalog_id(self) -> str:
        """`<catalog provider>/<id>` — the one spelling that names this row and no other."""
        return f"{self.provider}/{self.id}"

    def blended_cost(self, mix: "TokenMix") -> float | None:
        """$ per million tokens spent in ``mix``'s proportions; None when the price is unknown.

        A local daemon serving the model is the one real zero. A missing cache rate is priced at
        the input rate — the most it can cost, never a discount nobody quoted.
        """
        if self.serves_itself:
            return 0.0
        if self.input_cost_per_million is None or self.output_cost_per_million is None:
            return None
        if self.input_cost_per_million == 0 and self.output_cost_per_million == 0:
            # Nobody serves it here, yet the catalog says free: a placeholder, not a price.
            return None
        rates = (
            self.input_cost_per_million,
            self.input_cost_per_million if self.cache_read_cost_per_million is None else self.cache_read_cost_per_million,
            self.input_cost_per_million if self.cache_write_cost_per_million is None else self.cache_write_cost_per_million,
            self.output_cost_per_million,
        )
        return sum(share * rate for share, rate in zip(mix.shares, rates))

    @staticmethod
    def cost_of(input_cost: float | None, output_cost: float | None) -> float:
        """Sum of input/output cost-per-million; missing values count as 0."""
        return (input_cost or 0.0) + (output_cost or 0.0)

    @property
    def serves_itself(self) -> bool:
        """Whether a LIVE local daemon is serving this model — the one honest zero.

        Distinct from :meth:`is_served`, which answers "has the provider ruled it out?" — True
        for everything a daemon never mentioned. This one is positive: this provider is running
        AND holds this model, so a token costs nothing — nobody is billing for it.
        """
        return self.id in (Model._served.get(self.provider) or ())

    @property
    def thrift(self) -> tuple[int, float, float]:
        """How to order models when the criterion is satisfied and only price is left to decide.

        Three facts, in order. FIRST whether price is known at all: `cost_of` counts a missing
        price as 0, so the 50 scored models with no price — `chatgpt/gpt-5.4-pro` among them —
        sorted ahead of everything genuinely cheap and won every criterion. Free and unmeasured
        are different facts, only one an argument for choosing a model — unpriced goes LAST
        unless a local daemon serves it, the real zero.

        THEN price. THEN the measure, descending: at one price, take the better model. Sorting
        on price alone left ties in registry order — an arbitrary pick wearing the words
        "cheapest that clears".
        """
        priced = self.serves_itself or self.input_cost_per_million is not None \
            or self.output_cost_per_million is not None
        return (0 if priced else 1, self.cost_score, -(self.intelligence_score or 0.0))

    @staticmethod
    def quality_per_dollar(score: float, cost: float | None) -> float | None:
        """Score divided by total cost; returns None when cost is missing or zero."""
        if cost is None or cost <= 0:
            return None
        return score / cost

    @classmethod
    def match_published(cls, name: str) -> "Model | None":
        """Match only an exact normalized model identity; substrings silently misroute models."""
        needle = re.sub(r"\s*\(.*$", "", name).lower()
        needle = re.sub(r"[^a-z0-9]", "", needle)
        for m in cls.registry():
            bare = re.sub(r"[^a-z0-9]", "", m.id.split("/", 1)[-1].lower())
            if needle == bare:
                return m
        return None

    @classmethod
    def by_capability(
        cls, cap: ModelCapability, available_only: bool = True
    ) -> list[Self]:
        results = [m for m in cls._registry if m.can(cap)]
        if available_only:
            results = [m for m in results if m.is_available()]
        results.sort(key=lambda m: m.thrift)
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
        if cost_entry.get("supports_video_input") or is_native_video_model(model_id):
            caps.add(ModelCapability.VIDEO)
        if cost_entry.get("supports_audio_input") or is_audio_model(model_id):
            caps.add(ModelCapability.AUDIO)
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
            cache_read_cost_per_million=cost_entry["cache_read_input_token_cost"] * 1_000_000
            if cost_entry.get("cache_read_input_token_cost") is not None
            else None,
            cache_write_cost_per_million=cost_entry["cache_creation_input_token_cost"] * 1_000_000
            if cost_entry.get("cache_creation_input_token_cost") is not None
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
        # "unknown" is a SENTINEL here, not a shrug: `vision/session` branches on it meaning
        # "litellm doesn't know this id, parse the prefix yourself". Inferring the provider from
        # the prefix here looked like an improvement, silently disabled those branches, sending
        # `anthropic/example-model` to the Claude CLI unstripped.
        provider = cost_entry.get("litellm_provider", "unknown")
        model = cls._from_litellm_cost(model_id, provider, cost_entry)
        cls._register(model)
        return model  # type: ignore[return-value]

    @classmethod
    def _load_from_json(cls, raw: str) -> None:
        """Populate registry from GALAIUS_MODELS_JSON."""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            _log.warning("Invalid GALAIUS_MODELS_JSON, skipping")
            return

        models_config = ModelsConfig.model_validate(data)
        cls._provider_keys = {
            name: spec.env_keys for name, spec in models_config.providers.items()
        }
        cls._component_recommendations = list(
            models_config.recommendations.get("component", [])
        )
        cls._coord_formats = dict(models_config.coord_formats)
        component_recs = set(cls._component_recommendations)

        for provider_name, provider_spec in models_config.providers.items():
            for model_id, model_spec in provider_spec.models.items():
                caps: set[ModelCapability] = {ModelCapability.VLM}
                fmt = cls._match_coord_format(model_id, models_config.coord_formats)
                if fmt is not None or model_id in component_recs:
                    caps.add(ModelCapability.GUI_GROUNDING)
                # Capability tags carried from upstream catalog (litellm) — e.g. computer_use /
                # video — so behaviour derives from a live source, not a second hardcoded list.
                # Unknown tags are ignored.
                for tag in model_spec.capabilities:
                    try:
                        caps.add(ModelCapability(tag))
                    except ValueError:
                        pass
                # Belt-and-suspenders: derive native-video / audio from the family table too, so
                # the registry is correct even against an un-regenerated catalog whose JSON tags
                # predate these capabilities (litellm's own flags don't populate them).
                if is_native_video_model(model_id):
                    caps.add(ModelCapability.VIDEO)
                if is_audio_model(model_id):
                    caps.add(ModelCapability.AUDIO)
                cls._register(
                    cls(
                        id=model_id,
                        provider=provider_name,
                        capabilities=caps,
                        input_cost_per_million=model_spec.input_cost_per_million,
                        output_cost_per_million=model_spec.output_cost_per_million,
                        cache_read_cost_per_million=model_spec.cache_read_cost_per_million,
                        cache_write_cost_per_million=model_spec.cache_write_cost_per_million,
                        supports_structured_output=model_spec.supports_response_schema,
                        intelligence_score=model_spec.intelligence_score,
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

        Providers declaring no key are omitted — nothing to configure, so presence isn't
        evidence the user set anything up. A provider that ANSWERED us is included whether or
        not it declares a key: it is running.
        """
        return sorted(
            {
                provider
                for provider, keys in cls._provider_keys.items()
                if keys and all(os.environ.get(k) for k in keys)
            }
            | set(cls._served)
        )

    @classmethod
    def available_by_capability(cls, cap: ModelCapability) -> list[Self]:
        """Models with ``cap`` from providers whose API keys are configured, cheapest first."""
        available = set(cls.available_providers())
        models = [
            m
            for m in cls._registry
            if m.can(cap) and m.provider in available and m.is_served()
        ]
        models.sort(key=lambda m: m.thrift)
        return models  # type: ignore[return-value]

    @classmethod
    def recommended_grounding(cls) -> list[Self]:
        """Configured grounding models in preference order.

        Cheapest-first surfaces free general/image-gen VLMs that mislocate boxes, so rank:
        curated ``recommendations.component`` (models tuned for UI grounding) → ScreenSpot-
        scored → remaining grounding models by cost. Only providers with a set key included.
        Used by ``galaius detect`` and the dashboard so "best grounding model" is defined in
        one place.
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
                and model.is_served()
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
    def live_providers(cls) -> set[str]:
        """Providers that answered us directly at load time, rather than merely declaring a key."""
        return set(cls._served)

    @classmethod
    def merge_ollama(cls, discovered: "list[OllamaModel]") -> None:
        """Fold the models a running Ollama daemon actually has into the registry.

        The baked catalog is a SNAPSHOT — can't contain a model somebody pulled after it was
        generated, exactly the model they're most likely to want. What the daemon reports is
        the truth, wins where the two disagree, ADDS where the catalog is silent; a baked row's
        scores and grounding format survive since the daemon doesn't know those and overwriting
        with nothing would be a downgrade.

        Takes CHAT-capable models only (:func:`galaius.ollama.serving`): an embedding model
        returns vectors not answers, letting it into a pool of prompt-answering candidates would
        only produce a confusing failure later.
        """
        if not discovered:
            return
        served: set[str] = set()
        for found in discovered:
            model_id = found.model_id
            caps: set[ModelCapability] = set()
            if found.vision:
                caps.add(ModelCapability.VLM)
            if "completion" in found.capabilities:
                caps.add(ModelCapability.LLM)
            fmt = cls._match_coord_format(model_id, cls._coord_formats)
            # Grounding is a claim about reading a SCREEN, so it only follows a vision model —
            # a text model matching the same id prefix must not inherit it.
            if fmt is not None and ModelCapability.VLM in caps:
                caps.add(ModelCapability.GUI_GROUNDING)
            if is_native_video_model(model_id):
                caps.add(ModelCapability.VIDEO)
            if is_audio_model(model_id):
                caps.add(ModelCapability.AUDIO)
            baked = cls.by_id(model_id)
            cls._register(
                cls(
                    id=model_id,
                    provider="ollama",
                    capabilities=caps | (baked.capabilities if baked else set()),
                    input_cost_per_million=baked.input_cost_per_million if baked else None,
                    output_cost_per_million=baked.output_cost_per_million if baked else None,
                    cache_read_cost_per_million=baked.cache_read_cost_per_million if baked else None,
                    cache_write_cost_per_million=baked.cache_write_cost_per_million if baked else None,
                    supports_structured_output=bool(baked and baked.supports_structured_output),
                    intelligence_score=baked.intelligence_score if baked else None,
                    coord_format=fmt or (baked.coord_format if baked else None),
                )
            )
            served.add(model_id)
        if served:
            # Deliberately not `{... : served}` unconditionally. A daemon up but serving
            # nothing we can prompt with (embeddings-only box — a normal RAG setup) would
            # otherwise revoke every baked row's key-based availability while still advertising
            # the provider. Knowing nothing usable isn't the same as knowing there's nothing.
            cls._served = {**cls._served, "ollama": served}

    #: Set once anything has taken charge of the registry — a load, or a test's own _register() —
    #: so `catalog()` fills it exactly once and never over a fixture.
    _catalog_loaded: ClassVar[bool] = False

    @classmethod
    def catalog(cls) -> list["Model"]:
        """The registry, loaded from bundled data the FIRST time anything asks it a question.

        Only `galaius.runtime` used to load it, so a process that never imported that module —
        the CLI's `agents spawn` — answered every criterion from an EMPTY list, read as "no
        model configured at all", while tests passed because conftest imports runtime for them.
        A registry somebody already filled (`load_registry`, or a test's `_register`) is left alone.
        """
        if not cls._registry and not cls._catalog_loaded:
            cls.load_registry()
        return cls._registry

    @classmethod
    def rescored(cls, models: list["Model"]) -> list["Model"]:
        """The same models, carrying what the LIVE board measures wherever it measures them.

        Scores in `models.json` are a snapshot that ages the day it ships, and the panel already
        fetches the real Artificial Analysis board onto the same disk. Ranking on the snapshot
        while calling the number `aa.intelligence` put two tabs of one window in open
        disagreement — one calling a model first at 60.2, the other 38.6 behind a different
        leader. The live board wins where it speaks; the snapshot answers where it doesn't, so a
        model the board never listed keeps whatever was known about it.
        """
        from galaius.model_catalog import bare_model_name, live_scores

        live = live_scores()
        if not live:
            return models  # no board on disk: whatever was baked is all anyone has
        for model in models:
            measured = live.get(bare_model_name(model.id))
            if measured is not None:
                model.intelligence_score = measured
        return models

    @classmethod
    def merge_ranked(cls, scores: dict[str, float], priced: dict[str, dict]) -> int:
        """Register the models the live BOARD ranks that the bundled snapshot never heard of.

        A criterion can only pick something the catalog knows exists. The snapshot covered 86 of
        the 450 models the board ranks, so the entire top of the leaderboard the panel DISPLAYS
        was invisible to the thing that CHOOSES — evaluated against a fifth of the field,
        answering confidently with last year's best.

        Same live-merge shape as the Ollama pass, same reason: a bundled catalog structurally
        cannot know. Rows come from live catalog or pricing sources; missing prices stay None,
        so a newly measured model remains selectable without being treated as free by `thrift`.

        `scores` is keyed by :func:`bare_model_name`, collapsing every regional spelling of one
        model onto one key; the id registered is the least-qualified one (`claude-opus-5`,
        never `au.anthropic.claude-opus-5`) — also the vendor's own base price.

        Returns how many were added — worth watching, since a snapshot caught up makes this
        pass do nothing.
        """
        from galaius.model_catalog import bare_model_name

        have = {bare_model_name(m.id) for m in cls._registry}
        best: dict[str, tuple[str, dict]] = {}
        for model_id, row in priced.items():
            key = bare_model_name(model_id)
            if key not in scores or key in have:
                continue
            # Fewest qualifiers wins: the canonical spelling is the one nobody had to prefix.
            rank = (model_id.count("/") + model_id.count("."), len(model_id))
            if key not in best or rank < (best[key][0].count("/") + best[key][0].count("."),
                                          len(best[key][0])):
                best[key] = (model_id, row)
        for key, (model_id, row) in best.items():
            model = cls._from_litellm_cost(
                model_id, row.get("litellm_provider") or model_id.split("/")[0].split(".")[0], row)
            model.intelligence_score = scores[key]
            cls._register(model)
        return len(best)

    @classmethod
    def load_registry(cls, models_json: str | None = None) -> None:
        """Populate the registry from models.json, falling back to litellm.

        Source order: ``models_json`` argument → ``GALAIUS_MODELS_JSON`` → catalog bundled in
        :mod:`galaius.data` → ``litellm.model_cost``. Measured grounding scores hydrated from
        :meth:`PackageData.grounding_raw`.

        Then the LIVE pass: a running Ollama daemon is asked what it actually has, since a
        bundled catalog structurally cannot know. Cached, short-timeout, silent — no daemon
        means no models, no error, no delay (see :mod:`galaius.ollama`).
        """
        cls._reset()
        cls._provider_keys = {}
        cls._component_recommendations = []
        cls._coord_formats = {}
        cls._served = {}
        cls._catalog_loaded = True
        raw = models_json or PackageData.models_raw()
        if raw:
            cls._load_from_json(raw)
        else:
            cls._load_from_litellm()
        grounding_raw = PackageData.grounding_raw()
        if grounding_raw:
            cls.hydrate_measured(grounding_raw)
        try:
            # Imported HERE, not at module scope: `galaius.ollama` pulls in httpx (~167ms
            # cold), and `galaius.models` is on the import path of every CLI command — same
            # reason `_litellm()` above is lazy. Module attribute lookup, so a caller can
            # substitute the daemon by swapping `ollama.discover_cached`.
            from galaius import ollama

            cls.merge_ollama(ollama.serving())
        except Exception:  # a live source must never be able to break the catalog
            _log.debug("ollama discovery failed; using the bundled catalog", exc_info=True)
        try:
            # The second live pass, same reason as the first: the board on disk ranks models the
            # bundled snapshot never heard of, and a criterion can't choose what the catalog
            # doesn't know exists. Cached, so no command pays litellm's import to find that out.
            from galaius.model_catalog import bare_model_name, live_scores, load_catalog, ranked_extras

            scores = live_scores()
            rows = ranked_extras()
            # The live model catalog can learn a release before LiteLLM's static price map does.
            # Carry those rows too; absent prices stay None and `thrift` keeps them behind priced
            # candidates. This is the same live-source merge, not a model-name exception list.
            # `bare_model_name` is the JOIN key: it folds `5.6` and `5-6` so a score finds its model
            # across resellers. The id registered must stay the catalog's own spelling, because it
            # is what a harness is launched with — codex rejects `gpt-5-6-luna` outright.
            known = {bare_model_name(model_id) for model_id in rows}
            for info in load_catalog().models:
                key = bare_model_name(info.id)
                if key not in scores or key in known:
                    continue
                known.add(key)
                provider, _, name = info.id.partition("/")
                rows[name or info.id] = {
                    "litellm_provider": provider,
                    "input_cost_per_token": info.input_cost_per_token,
                    "output_cost_per_token": info.output_cost_per_token,
                    "mode": "chat",
                    "supports_vision": "image" in info.input_modalities,
                }
            cls.merge_ranked(scores, rows)
        except Exception:
            _log.debug("board pricing unavailable; using the bundled catalog", exc_info=True)
        try:
            # Role criteria need the same source rows as the benchmark panel. A stale-but-present
            # board still measures the same models — `aa.intelligence` already ranks on it
            # unconditionally (model_catalog.live_scores never checks age); gating THESE metrics
            # on `is_live` left every one of them empty whenever the on-disk board was more than
            # `TTL_SECONDS` old, which is most of the time between refreshes. `is_live` still
            # governs what a UI may call "current" — never whether a real, if aged, measurement
            # counts for ranking.
            from galaius import benchmark_source

            board = benchmark_source.load_scores()
            if board.scores:
                cls.hydrate_benchmark_scores(
                    [(score.name, score.metrics) for score in board.scores]
                )
        except Exception:
            _log.debug("Artificial Analysis benchmark hydration failed", exc_info=True)
        # Applied at LOAD, never on every read: a test that registers its own models is stating
        # what they score, and rescoring those against this machine's board would erase it.
        cls.rescored(cls._registry)

    @classmethod
    def hydrate_benchmark_scores(
        cls, rows: list[tuple[str, dict[str, float]]]
    ) -> None:
        """Attach current source scores to registered models through the benchmark registry."""
        for model_name, metrics in rows:
            model = cls.match_published(model_name)
            if model is None:
                continue
            for benchmark_id, score in metrics.items():
                benchmark = Benchmark.by_id(benchmark_id)
                if benchmark is not None:
                    benchmark._measured[model.id] = score


__all__ = [
    "Benchmark",
    "BenchmarkRecommendation",
    "CircuitBreaker",
    "Model",
    "ModelCapability",
    "ModelSpec",
    "ModelsConfig",
    "ProviderSpec",
    "PublishedEntry",
    "PublishedTable",
    "RegistryMixin",
]


#: Words that name the KIND of a source rather than the source itself — dropped when initials
#: are derived, so "OpenCompass video leaderboard" becomes "oc" and not "ovl".
_SOURCE_NOISE = {"leaderboard", "leaderboards", "benchmark", "the", "of", "and", "for"}


def _namespace_of(source: str) -> str:
    """A short, stable namespace for a source nobody has named explicitly.

    Initials of the significant words ("Artificial Analysis" -> "aa"); a single significant word
    becomes itself ("MMAU" -> "mmau"). Deterministic, so it never shifts under a reader.
    """
    words = [w for w in re.split(r"[\s\-_]+", source) if w and w.lower() not in _SOURCE_NOISE]
    if not words:
        return "x"
    if len(words) == 1:
        return words[0].lower()
    return "".join(w[0] for w in words).lower()


class Benchmark(RegistryMixin, BaseModel):
    """A benchmark for evaluating a model capability.

    Scores come from published online leaderboards (:attr:`published`). Optional measured
    scores — injected via ``GALAIUS_GROUNDING_JSON`` (e.g. fetched from an online source),
    never our own paid eval — live in :attr:`_measured`.
    """

    id: str
    name: str
    description: str
    # Which capability the benchmark measures, so the UI can group + explain by task.
    category: Literal["text", "image", "gui_grounding", "video", "audio"] = "gui_grounding"
    # Where its live scores come from, and the env key that source needs ("" = keyless / auto).
    # Surfaced in the config so the user can supply an optional key per source — no CLI needed.
    source: str = ""
    source_auth: str = ""
    requires_auth: bool = False
    #: The variable namespace this benchmark's score is addressed by — "aa" for Artificial
    #: Analysis, "gui" for the grounding leaderboard, etc. A NAMESPACE IS THE SOURCE: a bare
    #: `screenspot` hides who measured it, and leaderboards rarely agree. Derived from the
    #: source's initials when data doesn't say, so a benchmark added tomorrow is addressable the
    #: same day with nothing to edit here.
    namespace: str = ""
    metric: str = "accuracy"
    url: str = ""
    score_url: str = ""
    methodology_url: str = ""
    #: Exact field in the live source payload. Empty means this benchmark is backed by a
    #: published table or local measurement instead.
    source_field: str = ""
    score_range: tuple[float, float] | None = None
    higher_is_better: bool | None = None
    published: PublishedTable | None = None

    _measured: dict[str, float] = PrivateAttr(default_factory=dict)

    @property
    def variable(self) -> str:
        """How a criterion names this benchmark's score: ``<namespace>.<id>``."""
        return f"{self.namespace or _namespace_of(self.source)}.{self.id}"

    def score_for(self, model: "Model") -> float | None:
        """Measured source score for ``model``, or None if not evaluated."""
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
            if not entry.qualifies(self.published.freshness):
                continue
            m = Model.match_published(entry.model_name)
            if m is not None:
                out.append((m, entry.score))
        return out

    def lib_recommendation_model(self) -> "Model | None":
        if self.published is None or not self.published.lib_recommendation:
            return None
        if not any(
            entry.model_name == self.published.lib_recommendation
            and entry.qualifies(self.published.freshness)
            for entry in self.published.entries
        ):
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
                if not entry.qualifies(self.published.freshness):
                    continue
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


_ = PublishedTable  # re-exported via __all__; reference here silences unused-import lint

# Pre-register known grounding benchmarks at import time.
# Published tables for these benchmarks live in galaius.benchmarks.published.
# GUI grounding — "can the model point at the right on-screen element to click?"
Benchmark._register(
    Benchmark(
        id="screenspot",
        name="ScreenSpot",
        category="gui_grounding",
        source="GUI-Agent grounding leaderboard",
        namespace="gui",
        source_auth="",
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
        source="GUI-Agent grounding leaderboard",
        namespace="gui",
        source_auth="",
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
        id="mmmu_pro",
        name="MMMU Pro",
        category="image",
        source="Artificial Analysis public evaluation",
        namespace="aa",
        source_auth="",
        description=(
            "Artificial Analysis MMMU Pro visual-understanding accuracy; this is not the "
            "MMMU or MMBench benchmark."
        ),
        url="https://artificialanalysis.ai/evaluations/mmmu-pro",
        score_url="https://artificialanalysis.ai/evaluations/mmmu-pro",
        methodology_url=(
            "https://artificialanalysis.ai/methodology/intelligence-benchmarking#mmmu-pro"
        ),
        score_range=(0.0, 1.0),
        higher_is_better=True,
        published=PublishedTable.load("mmmu_pro"),
    )
)
# Language-model role metrics. Names are registry data; the live adapter maps only these exact
# Artificial Analysis API fields, so an example or an unavailable field cannot become a route.
for _id, _name, _field, _range in (
    ("coding_index", "Artificial Analysis Coding Index", "artificial_analysis_coding_index", (0.0, 100.0)),
    ("agentic_index", "Artificial Analysis Agentic Index", "artificial_analysis_agentic_index", (0.0, 100.0)),
    ("scicode", "SciCode", "scicode", (0.0, 1.0)),
    ("aa_lcr", "Artificial Analysis Long Context Reasoning", "aa_lcr", (0.0, 1.0)),
    ("aa_omniscience_index", "Artificial Analysis Omniscience Index", "aa_omniscience_index", None),
    ("aa_omniscience_accuracy", "Artificial Analysis Omniscience Accuracy", "aa_omniscience_accuracy", (0.0, 1.0)),
    ("ifbench", "IFBench", "ifbench", (0.0, 1.0)),
    ("terminalbench_hard", "Terminal-Bench Hard", "terminalbench_hard", (0.0, 1.0)),
    ("terminalbench_v2_1", "Terminal-Bench v2.1", "terminalbench_v2_1", (0.0, 1.0)),
):
    Benchmark._register(
        Benchmark(
            id=_id,
            name=_name,
            category="text",
            source="Artificial Analysis public evaluation",
            namespace="aa",
            description=f"{_name} as published by Artificial Analysis.",
            url="https://artificialanalysis.ai/data-api/docs",
            score_url="https://artificialanalysis.ai/data-api/docs",
            source_field=_field,
            score_range=_range,
            higher_is_better=True,
        )
    )
# Current coding measures Artificial Analysis publishes on its site but in no API field — the API's
# coding field is the legacy index, empty for every model newer than its methodology switch.
Benchmark._register(
    Benchmark(
        id="coding_agent_index",
        name="Artificial Analysis Coding Agent Index v1.5",
        category="text",
        source="Artificial Analysis Coding Agent Index",
        namespace="aa",
        description=(
            "Equal-weight average of DeepSWE v1.1, Terminal-Bench 4.0 and SWE-Atlas-QnA, run in "
            "a coding agent. Per model: its best single-model agent row (vendor harness, max effort)."
        ),
        url="https://artificialanalysis.ai/agents/coding-agents",
        score_url="https://artificialanalysis.ai/agents/coding-agents",
        methodology_url="https://artificialanalysis.ai/methodology/coding-agents-benchmarking",
        score_range=(0.0, 100.0),
        higher_is_better=True,
        published=PublishedTable.load("coding_agent_index"),
    )
)
Benchmark._register(
    Benchmark(
        id="terminalbench_4",
        name="Terminal-Bench 4.0",
        category="text",
        source="Artificial Analysis public evaluation",
        namespace="aa",
        description=(
            "Artificial Analysis' harness-neutral Terminal-Bench 4.0 run (mini-swe-agent, pass@1, "
            "66 tasks, 3 repeats)."
        ),
        url="https://artificialanalysis.ai/evaluations/terminalbench-4-0",
        score_url="https://artificialanalysis.ai/evaluations/terminalbench-4-0",
        methodology_url="https://artificialanalysis.ai/methodology/intelligence-benchmarking",
        score_range=(0.0, 1.0),
        higher_is_better=True,
        published=PublishedTable.load("terminalbench_4"),
    )
)
Benchmark._register(
    Benchmark(
        id="mmbench",
        name="MMBench",
        category="image",
        source="OpenCompass",
        namespace="oc",
        source_auth="",
        description=(
            "Broad multiple-choice perception + reasoning over images (EN/CN), with "
            "robustness checks — a wide general image-understanding measure."
        ),
        url="https://github.com/open-compass/MMBench",
        score_range=(0.0, 1.0),
        higher_is_better=True,
        published=PublishedTable.load("mmbench"),
    )
)
# Video understanding — "can the model reason over time across frames?"
Benchmark._register(
    Benchmark(
        id="video_mme",
        name="Video-MME",
        category="video",
        source="OpenCompass video leaderboard",
        namespace="oc",
        source_auth="",
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
        source="OpenCompass video leaderboard",
        namespace="oc",
        source_auth="",
        description=(
            "20 temporal-reasoning tasks (action/sequence understanding) that can't be solved "
            "from a single frame — tests genuine video, not stills."
        ),
        url="https://github.com/OpenGVLab/Ask-Anything",
        published=PublishedTable.load("mvbench"),
    )
)
Benchmark._register(
    Benchmark(
        id="mlvu",
        name="MLVU",
        category="video",
        source="OpenCompass video leaderboard",
        namespace="oc",
        source_auth="",
        description=(
            "Multi-task LONG-video understanding: 3-minute-to-2-hour videos across 9 tasks "
            "(holistic + single-detail + multi-detail) — the long-form complement to Video-MME."
        ),
        url="https://github.com/JUNJIE99/MLVU",
        published=PublishedTable.load("mlvu"),
    )
)
# Audio understanding — "can the model reason over speech, sound and music?"
Benchmark._register(
    Benchmark(
        id="mmau",
        name="MMAU",
        category="audio",
        source="MMAU leaderboard",
        source_auth="",
        description=(
            "Massive Multi-task Audio Understanding: 10,000 clips across speech, environmental "
            "sound and music with expert QA (27 skills) — the headline audio benchmark, the audio "
            "analogue of MMMU (image) and Video-MME (video). Transcription quality is ranked "
            "separately by Word Error Rate on the HF Open ASR Leaderboard."
        ),
        url="https://sakshi113.github.io/mmau_homepage/",
        published=PublishedTable.load("mmau"),
    )
)


# Canonical dataset name → Benchmark.id mapping. Source of truth for the
# eval pipeline; do not add string aliases — match exactly the HF dataset id.
_DATASET_TO_BENCHMARK: dict[str, str] = {
    "rootsautomation/ScreenSpot": "screenspot",
    "TIGER-Lab/ScreenSpot-Pro": "screenspot_pro",
}
