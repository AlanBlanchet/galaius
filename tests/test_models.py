import json
import time
from unittest.mock import patch

import pytest

from galaius.models import (
    Benchmark,
    BenchmarkRecommendation,
    CircuitBreaker,
    Model,
    ModelCapability,
)
from tests.support.models import model

SAMPLE_JSON = json.dumps(
    {
        "providers": {
            "anthropic": {
                "envKeys": ["ANTHROPIC_API_KEY"],
                "models": {
                    "claude-3-haiku-20240307": {
                        "supports_response_schema": True,
                        "input_cost_per_million": 0.25,
                        "output_cost_per_million": 1.25,
                    },
                    "claude-4-sonnet-20250514": {
                        "supports_response_schema": True,
                        "input_cost_per_million": 3.0,
                        "output_cost_per_million": 15.0,
                    },
                },
            },
            "gemini": {
                "envKeys": ["GEMINI_API_KEY"],
                "models": {
                    "gemini/gemini-2.0-flash": {
                        "supports_response_schema": True,
                        "input_cost_per_million": 0.1,
                        "output_cost_per_million": 0.4,
                    },
                },
            },
        },
        "recommendations": {
            "component": ["claude-4-sonnet-20250514"],
            "image": ["gemini/gemini-2.0-flash"],
        },
        "coordFormats": {
            "gemini/": {"normalized": True, "box_order": "yxyx", "box_key": "box_2d"}
        },
    }
)


def _clear_measured() -> None:
    for bench in Benchmark.registry():
        bench._measured.clear()


@pytest.fixture(autouse=True)
def _clear_registry():
    """Reset Model registry + measured scores around each test.

    Benchmark.registry() itself is NOT reset — the pre-registered grounding
    benchmarks (screenspot, screenspot_pro) must remain across tests.

    ``_catalog_loaded`` is reset too — ``_reset()`` alone empties the registry but leaves that
    flag set from an earlier ``load_registry(SAMPLE_JSON)`` call in this file, so the NEXT test
    anywhere in the suite that asks ``Model.catalog()`` for the bundled catalog gets a
    permanently empty one instead (`catalog()` only self-loads when the flag is still False) —
    every criterion in that later test then quietly qualifies nothing.
    """
    Model._reset()
    Model._catalog_loaded = False
    _clear_measured()
    yield
    Model._reset()
    Model._catalog_loaded = False
    _clear_measured()


def _register_models(*models: Model) -> None:
    for m in models:
        Model._register(m)


class TestModelCapability:
    @pytest.mark.parametrize(
        "cap,expected",
        [
            (ModelCapability.VLM, True),
            (ModelCapability.GUI_GROUNDING, False),
            (ModelCapability.LLM, False),
        ],
    )
    def test_model_can_capability(self, cap, expected):
        m = model(caps={ModelCapability.VLM})
        assert m.can(cap) is expected

    @pytest.mark.parametrize(
        "input_cost,output_cost,expected",
        [
            (1.0, 2.0, 3.0),
            (None, None, 0.0),
            (0.5, None, 0.5),
            (None, 3.0, 3.0),
            (5.0, None, 5.0),
            (None, 5.0, 5.0),
            (0.0, 0.0, 0.0),
        ],
    )
    def test_model_cost_score(self, input_cost, output_cost, expected):
        """The property and the free function it delegates to agree on every case."""
        m = model(input_cost=input_cost, output_cost=output_cost)
        assert m.cost_score == Model.cost_of(input_cost, output_cost) == expected

    def test_live_scored_catalog_model_enters_registry_without_litellm_row(self, monkeypatch):
        """A fresh live model catalog can outrun LiteLLM's release map; selection must still
        see the model and carry its source price when present."""
        from types import SimpleNamespace

        from galaius import model_catalog

        monkeypatch.setattr(model_catalog, "live_scores", lambda: {"new-live": 90.0})
        monkeypatch.setattr(model_catalog, "ranked_extras", lambda: {})
        monkeypatch.setattr(model_catalog, "load_catalog", lambda: SimpleNamespace(models=[
            model_catalog.ModelInfo(
                id="vendor/new-live", input_cost_per_token=None,
                output_cost_per_token=None, input_modalities=("text",),
            ),
        ]))
        Model.load_registry(SAMPLE_JSON)
        found = Model.by_id("new-live")
        assert found is not None
        assert found.provider == "vendor"
        assert found.intelligence_score == 90.0
        assert found.input_cost_per_million is None

    def test_live_catalog_model_keeps_its_own_version_spelling(self, monkeypatch):
        """The score join folds `5.6` and `5-6` together; the id a harness is launched with must
        not. Registering the folded key sent codex `gpt-5-6-luna`, which it rejects outright:
        "The 'gpt-5-6-luna' model is not supported when using Codex with a ChatGPT account"."""
        from types import SimpleNamespace

        from galaius import model_catalog

        monkeypatch.setattr(model_catalog, "live_scores", lambda: {"gpt-5-6-luna": 80.0})
        monkeypatch.setattr(model_catalog, "ranked_extras", lambda: {})
        monkeypatch.setattr(model_catalog, "load_catalog", lambda: SimpleNamespace(models=[
            model_catalog.ModelInfo(
                id="openai/gpt-5.6-luna", input_cost_per_token=None,
                output_cost_per_token=None, input_modalities=("text",),
            ),
        ]))
        Model.load_registry(SAMPLE_JSON)
        assert Model.by_id("gpt-5.6-luna") is not None
        assert Model.by_id("gpt-5-6-luna") is None

    @pytest.mark.parametrize(
        "score, cost, expected",
        [
            (0.5, 2.0, 0.25),
            (0.5, 0.0, None),
            (0.5, None, None),
            (0.0, 1.0, 0.0),
        ],
    )
    def test_quality_per_dollar(self, score, cost, expected):
        assert Model.quality_per_dollar(score, cost) == expected

    def test_recommendation_quality_per_dollar_uses_helper(self):
        m = model(id="x/y", input_cost=2.0, output_cost=2.0)
        bench = Benchmark(id="bx", name="b", description="b")
        rec = BenchmarkRecommendation(
            benchmark=bench, model=m, source="published", rank=1, score=0.8
        )
        assert rec.quality_per_dollar == Model.quality_per_dollar(0.8, 4.0) == 0.2

        zero = model(id="z/z", input_cost=None, output_cost=None)
        rec2 = BenchmarkRecommendation(
            benchmark=bench, model=zero, source="published", rank=1, score=0.5
        )
        assert rec2.quality_per_dollar is None


class TestModelFromLitellmId:
    def test_known_in_registry(self):
        m = model(id="anthropic/haiku")
        _register_models(m)
        result = Model.from_litellm_id("anthropic/haiku")
        assert result.id == "anthropic/haiku"

    def test_unknown_from_litellm_cost(self):
        fake_cost = {
            "new/model": {
                "litellm_provider": "new_provider",
                "supports_vision": True,
                "supports_response_schema": False,
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000002,
            }
        }
        with patch("galaius.models._litellm") as litellm_fn:
            litellm_fn.return_value.model_cost = fake_cost
            result = Model.from_litellm_id("new/model")
        assert result.id == "new/model"
        assert result.provider == "new_provider"
        assert result.input_cost_per_million == 1.0
        assert result.output_cost_per_million == 2.0


class TestRegistry:
    def test_from_models_json(self):
        Model.load_registry(SAMPLE_JSON)
        registry = Model.registry()
        assert len(registry) == 3

        ids = {m.id for m in registry}
        assert "claude-3-haiku-20240307" in ids
        assert "claude-4-sonnet-20250514" in ids
        assert "gemini/gemini-2.0-flash" in ids

        gemini = next(m for m in registry if m.id == "gemini/gemini-2.0-flash")
        assert gemini.can(ModelCapability.GUI_GROUNDING)
        assert gemini.coord_format is not None
        assert gemini.coord_format.normalized is True

        sonnet = next(m for m in registry if m.id == "claude-4-sonnet-20250514")
        assert sonnet.can(ModelCapability.GUI_GROUNDING)
        assert sonnet.supports_structured_output is True

    def test_fallback_to_litellm(self):
        fake_cost = {
            "vision/model-a": {
                "litellm_provider": "openai",
                "supports_vision": True,
                "supports_response_schema": True,
                "input_cost_per_token": 0.000005,
                "output_cost_per_token": 0.00001,
            },
            "text/model-b": {
                "litellm_provider": "openai",
                "supports_vision": False,
            },
        }
        # No catalog data (env or bundled) → load_registry falls through to litellm.
        with (
            patch("galaius.models._litellm") as litellm_fn,
            patch("galaius.models.PackageData.models_raw", return_value=None),
            patch("galaius.models.PackageData.grounding_raw", return_value=None),
        ):
            litellm_fn.return_value.model_cost = fake_cost
            Model.load_registry()

        registry = Model.registry()
        assert len(registry) == 1
        assert registry[0].id == "vision/model-a"
        assert registry[0].supports_structured_output is True


class TestRegistryMixin:
    def test_registry_mixin_per_subclass_isolation(self):
        """Model and Benchmark must own independent registry lists."""
        assert Model.registry() is not Benchmark.registry()
        before_models = len(Model.registry())
        before_bench = len(Benchmark.registry())
        m = model(id="iso/check")
        Model._register(m)
        assert len(Model.registry()) == before_models + 1
        assert len(Benchmark.registry()) == before_bench


class TestCircuitBreakerTTL:
    def test_ttl_expiry_recovers_model(self):
        cb = CircuitBreaker(ttl=0.1)
        cb.trip("model-x")
        assert cb.tripped("model-x") is True
        time.sleep(0.15)
        assert cb.tripped("model-x") is False


class TestByCapability:
    def test_filters_and_sorts(self):
        m_cheap = model(
            id="cheap", input_cost=0.1, output_cost=0.2, caps={ModelCapability.VLM}
        )
        m_expensive = model(
            id="expensive", input_cost=5.0, output_cost=10.0, caps={ModelCapability.VLM}
        )
        m_llm = model(id="llm-only", caps={ModelCapability.LLM})
        _register_models(m_expensive, m_cheap, m_llm)

        with patch.object(Model, "is_available", return_value=True):
            results = Model.by_capability(ModelCapability.VLM)
        assert len(results) == 2
        assert results[0].id == "cheap"
        assert results[1].id == "expensive"


class TestIsAvailable:
    def test_known_provider_uses_env_keys_not_litellm(self, monkeypatch):
        """A catalog provider's availability is a pure env-key check.

        Regression: ``is_available`` must NOT touch litellm at all — a litellm call can
        trigger an interactive OpenAI/ChatGPT device-auth flow and hang ``galaius
        providers`` / ``doctor`` (and the server's fallback chain) when no key is set.
        We make litellm itself explode to prove it is never reached.
        """
        Model.load_registry(SAMPLE_JSON)
        gemini = Model.by_id("gemini/gemini-2.0-flash")
        assert gemini is not None

        def _boom(*_args):
            raise RuntimeError("litellm must not be consulted by is_available")

        monkeypatch.setattr("galaius.models._litellm", _boom)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert gemini.is_available() is False
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        assert gemini.is_available() is True

    def test_available_providers_from_env_keys(self, monkeypatch):
        Model.load_registry(SAMPLE_JSON)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        assert Model.available_providers() == ["anthropic"]


class TestBenchmarkRecommend:
    def test_quality_per_dollar_orders_recommendations(self):
        cheap = model(
            id="cheap-vlm",
            input_cost=0.5,
            output_cost=1.5,
            caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM},
        )
        expensive = model(
            id="pricey-vlm",
            input_cost=10.0,
            output_cost=30.0,
            caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM},
        )
        _register_models(expensive, cheap)
        bench = Benchmark.by_id("screenspot_pro")
        assert bench is not None
        bench._measured[cheap.id] = 0.5
        bench._measured[expensive.id] = 0.5

        with patch.object(Model, "is_available", return_value=True):
            recs = bench.recommend(prefer="measured")

        assert [r.model.id for r in recs] == ["cheap-vlm", "pricey-vlm"]
        assert recs[0].rank == 1
        assert isinstance(recs[0], BenchmarkRecommendation)
        assert recs[0].source == "measured"
        assert recs[0].quality_per_dollar > recs[1].quality_per_dollar
        assert recs[0].score == 0.5
        assert recs[0].cost_per_million == 2.0

    def test_min_score_filter(self):
        weak = model(
            id="weak", caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM}
        )
        strong = model(
            id="strong", caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM}
        )
        _register_models(weak, strong)
        bench = Benchmark.by_id("screenspot_pro")
        assert bench is not None
        bench._measured[weak.id] = 0.05
        bench._measured[strong.id] = 0.5

        with patch.object(Model, "is_available", return_value=True):
            recs = bench.recommend(prefer="measured", min_score=0.1)

        assert [r.model.id for r in recs] == ["strong"]

    def test_models_without_score_excluded(self):
        scored = model(
            id="scored", caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM}
        )
        unscored = model(
            id="unscored", caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM}
        )
        _register_models(scored, unscored)
        bench = Benchmark.by_id("screenspot_pro")
        assert bench is not None
        bench._measured[scored.id] = 0.3

        with patch.object(Model, "is_available", return_value=True):
            recs = bench.recommend(prefer="measured")

        assert [r.model.id for r in recs] == ["scored"]

    def test_by_id_unknown_returns_none(self):
        assert Benchmark.by_id("nonexistent") is None

    def test_registry_contains_grounding_benchmarks(self):
        ids = {b.id for b in Benchmark.registry()}
        assert {"screenspot", "screenspot_pro"} <= ids
        b = Benchmark.by_id("screenspot")
        assert b is not None
        assert b.category == "gui_grounding"
        assert b.description and b.url.startswith("https://")  # task explained + linked

    def test_registry_covers_all_three_capability_categories(self):
        """Image / GUI grounding / Video are each represented (the categorized benchmarks UI)."""
        by_cat: dict[str, set[str]] = {}
        for b in Benchmark.registry():
            by_cat.setdefault(b.category, set()).add(b.id)
        assert "mmmu_pro" in by_cat.get("image", set())
        assert "video_mme" in by_cat.get("video", set())
        assert {"screenspot", "screenspot_pro"} <= by_cat.get("gui_grounding", set())
        # every benchmark explains its task and links out
        assert all(b.description and b.url for b in Benchmark.registry())

    def test_benchmarks_hydrated_from_grounding_env(self, monkeypatch):
        sample = json.dumps(
            {
                "providers": {
                    "anthropic": {
                        "envKeys": ["ANTHROPIC_API_KEY"],
                        "models": {
                            "claude-4-sonnet-20250514": {
                                "supports_response_schema": True,
                                "input_cost_per_million": 3.0,
                                "output_cost_per_million": 15.0,
                            }
                        },
                    }
                },
                "recommendations": {"component": ["claude-4-sonnet-20250514"]},
            }
        )
        grounding = json.dumps(
            {
                "claude-4-sonnet-20250514": {
                    "dataset": "rootsautomation/ScreenSpot",
                    "overall_accuracy": 0.42,
                    "text_accuracy": 0.5,
                    "icon_accuracy": 0.3,
                }
            }
        )
        monkeypatch.setenv("GALAIUS_GROUNDING_JSON", grounding)
        Model.load_registry(sample)

        bench = Benchmark.by_id("screenspot")
        assert bench is not None
        m = next(m for m in Model.registry() if m.id == "claude-4-sonnet-20250514")
        assert bench.score_for(m) == 0.42
        other_bench = Benchmark.by_id("screenspot_pro")
        assert other_bench is not None
        assert other_bench.score_for(m) is None


class TestPublishedTable:
    def test_lib_recommendation_model_exact_identity_match(self):
        bench = Benchmark.by_id("screenspot_pro")
        assert bench is not None
        # Published identity is normalized for punctuation, never shortened by substring.
        rec_name = bench.published.lib_recommendation if bench.published else None
        assert rec_name is not None
        assert bench.lib_recommendation_model() is None
        m = model(
            id=f"openai/{rec_name}",
            caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM},
        )
        _register_models(m)
        bench.published.freshness = "current"
        next(e for e in bench.published.entries if e.model_name == rec_name).status = "eligible"
        matched = bench.lib_recommendation_model()
        assert matched is not None
        assert matched.id == m.id

    def test_published_models_in_registry(self):
        bench = Benchmark.by_id("screenspot_pro")
        assert bench is not None
        assert bench.published is not None
        # Pick any entry from the live cache and assert the bridge wires it through.
        entry = bench.published.entries[0]
        m = model(
            id=f"vendor/{entry.model_name}",
            caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM},
        )
        _register_models(m)
        bench.published.freshness = "current"
        entry.status = "eligible"
        pairs = bench.published_models_in_registry()
        assert any(
            model.id == m.id and abs(score - entry.score) < 1e-9
            for model, score in pairs
        )


class TestRecommendBoth:
    def test_recommend_prefer_both_includes_both_sources(self):
        m = model(
            id="openai/ui-tars-1.5",
            input_cost=1.0,
            output_cost=2.0,
            caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM},
        )
        _register_models(m)
        bench = Benchmark.by_id("screenspot_pro")
        assert bench is not None
        assert bench.published is not None
        bench.published.freshness = "current"
        for entry in bench.published.entries:
            entry.status = "eligible"
        bench._measured[m.id] = 0.7

        with patch.object(Model, "is_available", return_value=True):
            recs = bench.recommend(prefer="both")

        sources = {r.source for r in recs if r.model.id == m.id}
        assert sources == {"published", "measured"}

    def test_benchmark_recommendation_source_field(self):
        m = model(
            id="ui-tars-1.5",
            caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM},
        )
        _register_models(m)
        bench = Benchmark.by_id("screenspot_pro")
        assert bench is not None
        assert bench.published is not None
        bench.published.freshness = "current"
        for entry in bench.published.entries:
            entry.status = "eligible"
        with patch.object(Model, "is_available", return_value=True):
            published_recs = bench.recommend(prefer="published")
        assert published_recs
        assert all(r.source == "published" for r in published_recs)
        bench._measured[m.id] = 0.4
        with patch.object(Model, "is_available", return_value=True):
            measured_recs = bench.recommend(prefer="measured")
        assert all(r.source == "measured" for r in measured_recs)


def test_the_served_check_joins_two_spellings_of_one_model():
    """The local daemon calls it `ollama/kimi-k3:cloud`; the Ollama cloud endpoint calls the same
    model `ollama/kimi-k3`. Whichever answered discovery in THIS process is the spelling the
    served set holds, so a pin written in the other one read as "not served" — and `galaius
    providers` printed "key missing" beside a model that answers a live call.

    Every other cross-source join in this codebase goes through `bare_model_name`; this one
    compared raw strings.
    """
    from galaius.models import Model

    saved = dict(Model._served)
    try:
        Model._served = {"ollama": {"ollama/kimi-k3"}}
        assert Model(provider="ollama", id="ollama/kimi-k3", capabilities=set()).is_served()
        assert Model(provider="ollama", id="ollama/kimi-k3:cloud", capabilities=set()).is_served(), (
            "the local daemon's spelling of the very model the cloud endpoint named"
        )
        assert not Model(provider="ollama", id="ollama/never-pulled", capabilities=set()).is_served(), (
            "a model the daemon really does not have is still absent"
        )
    finally:
        Model._served = saved
