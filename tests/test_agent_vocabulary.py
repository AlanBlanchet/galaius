import pytest

from galaius.agents.profiles import model_source_for, overlay_for
from galaius.agents.policy import Policy, PolicyError
from galaius.agents.vocabulary import (
    ApprovalIntent,
    ModelSource,
    ThinkingLevel,
    TouchScope,
    UnsupportedIntent,
    vocabulary_for,
)


@pytest.mark.parametrize("harness", ["codex", "claude", "ollama", "cursor"])
@pytest.mark.parametrize("scope", list(TouchScope))
def test_every_touch_intent_has_a_safe_translation_or_named_refusal(harness, scope):
    translation = vocabulary_for(harness).touch[scope]
    if translation.native is None:
        assert scope.value in translation.detail or "file" in translation.detail.lower()


@pytest.mark.parametrize("harness", ["codex", "claude", "ollama", "cursor"])
@pytest.mark.parametrize("level", list(ThinkingLevel))
def test_every_thinking_intent_is_explicit(harness, level):
    translation = vocabulary_for(harness).thinking[level]
    if translation.native is None:
        assert "effort" in translation.detail.lower() or "harness" in translation.detail.lower()


def test_unsafe_full_access_never_becomes_unsandboxed_codex():
    translation = vocabulary_for("codex").translate("touch", TouchScope.FULL_ACCESS)
    assert translation.native == "workspace-write"
    assert translation.exact is False


def test_ollama_refuses_agent_permissions_in_its_own_vocabulary():
    with pytest.raises(UnsupportedIntent, match="touch intent"):
        vocabulary_for("ollama").translate("touch", TouchScope.WORKSPACE_WRITE)
    with pytest.raises(UnsupportedIntent, match="approval intent"):
        vocabulary_for("ollama").translate("approval", ApprovalIntent.ASK)


def test_model_source_maps_local_and_cloud_without_vendor_flag_guessing():
    assert model_source_for("ollama/gemma4", {}) is ModelSource.OLLAMA_LOCAL
    assert model_source_for("ollama/gemma4:cloud", {}) is ModelSource.OLLAMA_CLOUD
    assert model_source_for("hf/model", {"HF_BASE_URL": "https://router.huggingface.co/v1"}) is ModelSource.OPERATOR_ENDPOINT
    assert model_source_for("cursor/model", {}) is ModelSource.CURSOR_CLOUD


def test_ollama_cloud_overlay_uses_anthropic_bearer_token_only_for_cloud():
    env = {"OLLAMA_API_BASE": "https://ollama.com", "OLLAMA_API_KEY": "k42"}
    cloud = overlay_for("ollama/gemma4", env)
    local = overlay_for("ollama/gemma4", {"OLLAMA_API_BASE": "http://localhost:11434", "OLLAMA_API_KEY": "k42"})
    assert cloud["ANTHROPIC_AUTH_TOKEN"] == "k42"
    assert "ANTHROPIC_AUTH_TOKEN" not in local


def test_reasoning_can_be_selected_by_resolved_model_after_role_policy():
    policy = Policy(
        agents={"builder": "codex/gpt-5.6-luna"},
        reasoning={"builder": "low"},
        reasoning_models={"codex/gpt-5.6-luna": "high"},
    )
    policy.validate()
    assert policy.reasoning_for("builder", "codex/gpt-5.6-luna") == "high"
    assert policy.reasoning_for("builder", "gpt-5.6-luna") == "low"
    assert policy.reasoning_for("builder", "codex/other") == "low"


def test_reasoning_model_policy_rejects_unknown_effort():
    with pytest.raises(PolicyError, match="model .*reasoning"):
        Policy(reasoning_models={"cursor/model": "unbounded"}).validate()
