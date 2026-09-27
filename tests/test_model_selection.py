"""Which model a role actually resolves to — best/cheapest first, and only what clears its
criterion right now. Every role carries ONE requirement string
(``interact_core.tool_settings.PortableToolSettingsValues``); there is no separate pinned-model
field and no separate fallback-chain field — ``Criteria.qualifying`` already returns every
clearing model in rank order, so the second entry IS the fallback.

A bare capability filter with no ranking term (``cap.vlm``) resolves CHEAPEST-clearing, same as
every other criterion in this product (`interact/criteria.py`); a bare measured benchmark switches
to best-measured-first. Real cases this covers: no provider configured at all, a criterion that
resolves once a key is set, an explicit per-call override beating everything, and a criterion
that clears nothing REFUSING loudly rather than silently substituting a model nobody asked for.
"""

import pytest

from interact.config import Config
from interact.models import Benchmark, Model
from tests.support.models import catalog_of
from tests.support.models import model as _m

_RANK_STRONGEST = "cap.vlm and fx.strength"


@pytest.fixture
def registry():
    """Two reachable-once-keyed VLMs plus a keyless subscription wrapper that must never be
    auto-selected (interact never drives someone's subscription credentials on its own). A
    throwaway measured benchmark (never the live Artificial Analysis board, which these fixture
    ids aren't on) gives a rankable "strength" independent of price."""
    bench = Benchmark(id="strength", name="Fixture Strength", description="d", namespace="fx")
    Benchmark._register(bench)
    bench._measured.update({
        "alpha/weak": 10.0, "alpha/strong": 90.0, "beta/middling": 50.0, "keyless/genius": 99.0,
    })
    try:
        with catalog_of(
            _m("alpha/weak", score=10.0, input_cost=0.1, output_cost=0.1),
            _m("alpha/strong", score=90.0, input_cost=9.0, output_cost=9.0),
            _m("beta/middling", score=50.0, input_cost=1.0, output_cost=1.0),
            _m("keyless/genius", score=99.0, input_cost=0.0, output_cost=0.0),
        ):
            Model._provider_keys = {"alpha": ["ALPHA_KEY"], "beta": ["BETA_KEY"], "keyless": []}
            yield
    finally:
        Benchmark.registry().remove(bench)


def test_no_provider_configured_refuses_loudly(registry, monkeypatch):
    """No key anywhere → resolution fails NAMING the closest miss, never a silent empty id."""
    for key in ("ALPHA_KEY", "BETA_KEY"):
        monkeypatch.delenv(key, raising=False)
    cfg = Config(image_criteria="cap.vlm")
    with pytest.raises(RuntimeError, match="no model available for role 'image'"):
        cfg.resolve_model("image")


def test_a_key_appearing_makes_its_model_resolvable(registry, monkeypatch):
    monkeypatch.setenv("BETA_KEY", "k")
    cfg = Config(image_criteria="cap.vlm")
    assert cfg.resolve_model("image") == "beta/middling"


def test_the_strongest_configured_model_wins_with_a_ranking_criterion(registry, monkeypatch):
    monkeypatch.setenv("ALPHA_KEY", "k")
    monkeypatch.setenv("BETA_KEY", "k")
    cfg = Config(image_criteria=_RANK_STRONGEST)
    assert cfg.resolve_model("image") == "alpha/strong"


def test_a_keyless_subscription_wrapper_is_never_walked_into(registry, monkeypatch):
    """It scores highest and costs nothing, which is exactly why it needs an explicit route
    (a logged-in CLI session), never silent auto-selection."""
    monkeypatch.setenv("ALPHA_KEY", "k")
    cfg = Config(image_criteria=_RANK_STRONGEST)
    assert cfg.resolve_model("image") == "alpha/strong"


def test_an_explicit_per_call_override_beats_everything(registry, monkeypatch):
    monkeypatch.setenv("ALPHA_KEY", "k")
    cfg = Config(image_criteria=_RANK_STRONGEST)
    assert cfg.resolve_model("image", "beta/middling") == "beta/middling"


def test_a_criterion_that_clears_nothing_names_the_closest_miss(registry, monkeypatch):
    monkeypatch.setenv("ALPHA_KEY", "k")
    monkeypatch.setenv("BETA_KEY", "k")
    cfg = Config(image_criteria="fx.strength > 999")
    with pytest.raises(RuntimeError) as e:
        cfg.resolve_model("image")
    assert "999" in str(e.value)


def test_a_blank_criteria_field_falls_back_to_the_bare_capability(registry, monkeypatch):
    """An unset ``image_criteria`` is never "no requirement" — it means "any VLM" (cheapest
    clearing, like every other bare-capability criterion), so a role resolves out of the box
    with no configuration at all."""
    monkeypatch.setenv("ALPHA_KEY", "k")
    cfg = Config()
    assert cfg.criteria_for("image") == "cap.vlm"
    assert cfg.resolve_model("image") == "alpha/weak"  # cheapest of the two reachable alpha models


def test_ranked_models_second_entry_is_the_fallback(registry, monkeypatch):
    """No separate fallback-chain field: the criterion's own ranking already gives the fallback
    order — the second-ranked entry is what the old per-role ``*_fallbacks`` field duplicated."""
    monkeypatch.setenv("ALPHA_KEY", "k")
    monkeypatch.setenv("BETA_KEY", "k")
    cfg = Config(image_criteria=_RANK_STRONGEST)
    ranked = cfg.ranked_models("image")
    assert [m.id for m in ranked[:2]] == ["alpha/strong", "beta/middling"]


def test_circuit_broken_model_is_skipped_in_favour_of_the_next(registry, monkeypatch):
    from interact.models import CircuitBreaker

    monkeypatch.setenv("ALPHA_KEY", "k")
    monkeypatch.setenv("BETA_KEY", "k")
    breaker = CircuitBreaker()
    breaker.trip("alpha/strong")
    cfg = Config(image_criteria=_RANK_STRONGEST)
    assert cfg.resolve_model("image", breaker=breaker) == "beta/middling"
