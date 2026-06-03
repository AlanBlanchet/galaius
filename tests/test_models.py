import json
import time
from unittest.mock import patch

import pytest

from interact.models import (
    Benchmark,
    BenchmarkRecommendation,
    CircuitBreaker,
    Model,
    ModelCapability,
    ModelChain,
)

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
    """
    Model._reset()
    _clear_measured()
    yield
    Model._reset()
    _clear_measured()


def _register_models(*models: Model) -> None:
    for m in models:
        Model._register(m)


def _make_model(
    id: str = "test/model", caps=None, input_cost=1.0, output_cost=2.0, available=True
):
    return Model(
        id=id,
        provider="test",
        capabilities=caps or {ModelCapability.VLM},
        input_cost_per_million=input_cost,
        output_cost_per_million=output_cost,
    )


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
        m = _make_model(caps={ModelCapability.VLM})
        assert m.can(cap) is expected

    @pytest.mark.parametrize(
        "input_cost,output_cost,expected",
        [
            (1.0, 2.0, 3.0),
            (None, None, 0.0),
            (0.5, None, 0.5),
            (None, 3.0, 3.0),
        ],
    )
    def test_model_cost_score(self, input_cost, output_cost, expected):
        m = _make_model(input_cost=input_cost, output_cost=output_cost)
        assert m.cost_score == expected


class TestModelFromLitellmId:
    def test_known_in_registry(self):
        m = _make_model(id="anthropic/haiku")
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
        with patch("interact.models._litellm") as litellm_fn:
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
            patch("interact.models._litellm") as litellm_fn,
            patch("interact.models.PackageData.models_raw", return_value=None),
            patch("interact.models.PackageData.grounding_raw", return_value=None),
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
        m = _make_model(id="iso/check")
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


class TestModelChain:
    def test_active_skips_tripped(self):
        m1 = _make_model(id="model-a")
        m2 = _make_model(id="model-b")
        chain = ModelChain(role="image", preferences=[m1, m2])
        cb = CircuitBreaker()
        cb.trip("model-a")

        with patch.object(Model, "is_available", return_value=True):
            result = chain.active(breaker=cb)
        assert result is not None
        assert result.id == "model-b"

    def test_active_skips_unavailable(self):
        m1 = _make_model(id="model-a")
        m2 = _make_model(id="model-b")
        chain = ModelChain(role="image", preferences=[m1, m2])

        def availability(self):
            return self.id == "model-b"

        with patch.object(Model, "is_available", availability):
            result = chain.active()
        assert result is not None
        assert result.id == "model-b"

    def test_from_config(self):
        Model.load_registry(SAMPLE_JSON)
        with patch.object(Model, "is_available", return_value=False):
            chain = ModelChain.from_config(
                role="image",
                configured_model="gemini/gemini-2.0-flash",
                recommendations=["claude-3-haiku-20240307"],
            )
        assert chain.preferences[0].id == "gemini/gemini-2.0-flash"
        assert chain.preferences[1].id == "claude-3-haiku-20240307"
        ids = [m.id for m in chain.preferences]
        assert len(ids) == len(set(ids))


class TestByCapability:
    def test_filters_and_sorts(self):
        m_cheap = _make_model(
            id="cheap", input_cost=0.1, output_cost=0.2, caps={ModelCapability.VLM}
        )
        m_expensive = _make_model(
            id="expensive", input_cost=5.0, output_cost=10.0, caps={ModelCapability.VLM}
        )
        m_llm = _make_model(id="llm-only", caps={ModelCapability.LLM})
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
        trigger an interactive OpenAI/ChatGPT device-auth flow and hang ``interact
        providers`` / ``doctor`` (and the server's fallback chain) when no key is set.
        We make litellm itself explode to prove it is never reached.
        """
        Model.load_registry(SAMPLE_JSON)
        gemini = Model.by_id("gemini/gemini-2.0-flash")
        assert gemini is not None

        def _boom(*_args):
            raise RuntimeError("litellm must not be consulted by is_available")

        monkeypatch.setattr("interact.models._litellm", _boom)
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
        cheap = _make_model(
            id="cheap-vlm",
            input_cost=0.5,
            output_cost=1.5,
            caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM},
        )
        expensive = _make_model(
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
        weak = _make_model(
            id="weak", caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM}
        )
        strong = _make_model(
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
        scored = _make_model(
            id="scored", caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM}
        )
        unscored = _make_model(
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
        assert "screenspot" in ids
        assert "screenspot_pro" in ids
        b = Benchmark.by_id("screenspot")
        assert b is not None
        assert "huggingface.co/datasets/rootsautomation" in b.description

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
        monkeypatch.setenv("INTERACT_GROUNDING_JSON", grounding)
        Model.load_registry(sample)

        bench = Benchmark.by_id("screenspot")
        assert bench is not None
        m = next(m for m in Model.registry() if m.id == "claude-4-sonnet-20250514")
        assert bench.score_for(m) == 0.42
        other_bench = Benchmark.by_id("screenspot_pro")
        assert other_bench is not None
        assert other_bench.score_for(m) is None


class TestPublishedTable:
    def test_lib_recommendation_model_substring_match(self):
        bench = Benchmark.by_id("screenspot_pro")
        assert bench is not None
        # Published lib_recommendation comes from the upstream cache; substring-match
        # using whatever model_name happens to top the leaderboard today.
        rec_name = bench.published.lib_recommendation if bench.published else None
        assert rec_name is not None
        token = rec_name.split()[0].lower()
        assert bench.lib_recommendation_model() is None
        m = _make_model(
            id=f"openai/{token}",
            caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM},
        )
        _register_models(m)
        matched = bench.lib_recommendation_model()
        assert matched is not None
        assert matched.id == m.id

    def test_published_models_in_registry(self):
        bench = Benchmark.by_id("screenspot_pro")
        assert bench is not None
        assert bench.published is not None
        # Pick any entry from the live cache and assert the bridge wires it through.
        entry = bench.published.entries[0]
        token = entry.model_name.split()[0].lower()
        m = _make_model(
            id=f"vendor/{token}",
            caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM},
        )
        _register_models(m)
        pairs = bench.published_models_in_registry()
        assert any(
            model.id == m.id and abs(score - entry.score) < 1e-9
            for model, score in pairs
        )


class TestRecommendBoth:
    def test_recommend_prefer_both_includes_both_sources(self):
        m = _make_model(
            id="openai/ui-tars-1.5-7b-vision",
            input_cost=1.0,
            output_cost=2.0,
            caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM},
        )
        _register_models(m)
        bench = Benchmark.by_id("screenspot_pro")
        assert bench is not None
        bench._measured[m.id] = 0.7

        with patch.object(Model, "is_available", return_value=True):
            recs = bench.recommend(prefer="both")

        sources = {r.source for r in recs if r.model.id == m.id}
        assert sources == {"published", "measured"}

    def test_benchmark_recommendation_source_field(self):
        m = _make_model(
            id="ui-tars-1.5-7b",
            caps={ModelCapability.GUI_GROUNDING, ModelCapability.VLM},
        )
        _register_models(m)
        bench = Benchmark.by_id("screenspot_pro")
        assert bench is not None
        with patch.object(Model, "is_available", return_value=True):
            published_recs = bench.recommend(prefer="published")
        assert published_recs
        assert all(r.source == "published" for r in published_recs)
        bench._measured[m.id] = 0.4
        with patch.object(Model, "is_available", return_value=True):
            measured_recs = bench.recommend(prefer="measured")
        assert all(r.source == "measured" for r in measured_recs)
