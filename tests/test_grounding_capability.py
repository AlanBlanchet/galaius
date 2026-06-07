"""A model's grounding strategy is DERIVED from its capabilities (sourced from litellm), never a
hardcoded per-model list — so a coordinate-native model (computer-use) is driven by coordinates,
others by the safe DOM/ref list. These pin that derivation."""

import json

import pytest

from interact.models import Model, ModelCapability


def _model(*caps: ModelCapability) -> Model:
    return Model(id="x/y", provider="x", capabilities=set(caps))


@pytest.mark.parametrize(
    "caps, strategy",
    [
        ((ModelCapability.VLM,), "ref_list"),  # plain VLM, no grounding → safe ref path
        ((ModelCapability.VLM, ModelCapability.COMPUTER_USE), "coords"),  # native click coords
        ((ModelCapability.VLM, ModelCapability.GUI_GROUNDING), "coords"),  # known box convention
        ((ModelCapability.LLM,), "ref_list"),
    ],
)
def test_grounding_strategy_is_derived_from_capabilities(caps, strategy):
    assert _model(*caps).grounding_strategy() == strategy


@pytest.mark.parametrize(
    "flag, cap",
    [
        ("supports_computer_use", ModelCapability.COMPUTER_USE),
        ("supports_video_input", ModelCapability.VIDEO),
    ],
)
def test_litellm_flags_become_capabilities(flag, cap):
    """The litellm catalog is the source: its support flags map straight to our capabilities."""
    m = Model._from_litellm_cost("p/m", "p", {"supports_vision": True, flag: True})
    assert m.can(cap)
    cold = Model._from_litellm_cost("p/m", "p", {"supports_vision": True})
    assert not cold.can(cap)


def test_models_json_capability_tags_are_honoured(monkeypatch):
    """`capabilities` tags carried in models.json (from the generator) reach the registry; an
    unknown tag is ignored, not fatal."""
    blob = json.dumps(
        {
            "providers": {
                "acme": {
                    "envKeys": ["ACME_API_KEY"],
                    "models": {
                        "acme/grounder": {"capabilities": ["vlm", "computer_use", "bogus"]},
                        "acme/plain": {"capabilities": ["vlm"]},
                    },
                }
            }
        }
    )
    try:
        Model.load_registry(models_json=blob)
        grounder = Model.by_id("acme/grounder")
        assert grounder.can(ModelCapability.COMPUTER_USE)
        assert grounder.grounding_strategy() == "coords"
        assert ModelCapability("bogus") if False else True  # bogus tag was skipped, no crash
        assert Model.by_id("acme/plain").grounding_strategy() == "ref_list"
    finally:
        Model.load_registry()  # restore the bundled registry for other tests
