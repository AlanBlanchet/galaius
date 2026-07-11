"""Quality tiers: the review_ui/verify_ui `quality` literal picks the model by STAKES, not by name —
"low"/"medium" use a cheap sovereign self-host model (GLM-4.5V), "high"/"critical" the best frontier,
and "critical" strips findings/PASSes resting on an element interact never detected. No model spend."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from interact import server as srv
from interact.config import _DEFAULT_SOVEREIGN_MODEL, Config
from interact.vision.critique import RequirementCheck, UIFinding, UIReview, VerifyReport
from interact.state import InteractiveElement
from interact.vision import VLMResult


def _avail(value: bool):
    """Patch Model.is_available (a plain instance method — clean to monkeypatch, unlike a classmethod)
    so a tier's sovereign-model availability is deterministic without real API keys."""
    return lambda self: value


# --- resolve_quality_model: tier → model preference, graceful when the sovereign isn't available ---


@pytest.mark.parametrize("tier, available, expected", [
    ("low", True, _DEFAULT_SOVEREIGN_MODEL),
    ("medium", True, _DEFAULT_SOVEREIGN_MODEL),
    ("low", False, ""),       # sovereign key missing → "" → fall back to normal resolution, no hard fail
    ("high", True, ""),       # frontier tiers use the normal best-available model
    ("critical", True, ""),
])
def test_resolve_quality_model(monkeypatch, tier, available, expected):
    monkeypatch.setattr("interact.models.Model.is_available", _avail(available))
    assert Config().resolve_quality_model(tier) == expected


def test_resolve_quality_model_honors_a_configured_sovereign(monkeypatch):
    monkeypatch.setattr("interact.models.Model.is_available", _avail(True))
    assert Config(tier_sovereign_model="ollama/glm-local").resolve_quality_model("low") == "ollama/glm-local"


def test_resolve_quality_model_prefers_the_first_available_sovereign(monkeypatch):
    # A z.ai-key user (no Novita key) must get the z.ai GLM id from low/medium — NOT "" that falls
    # through to a frontier model. The tier tries each sovereign candidate; first available wins.
    monkeypatch.setattr("interact.models.Model.is_available", lambda self: self.id == "zai/glm-4.5v")
    assert Config().resolve_quality_model("low") == "zai/glm-4.5v"
    assert Config().resolve_quality_model("medium") == "zai/glm-4.5v"


def test_resolve_quality_model_falls_to_novita_when_thats_the_reachable_glm(monkeypatch):
    # A Novita-key user (no z.ai key) still lights up GLM via the other sovereign candidate.
    monkeypatch.setattr("interact.models.Model.is_available", lambda self: self.id == "novita/zai-org/glm-4.5v")
    assert Config().resolve_quality_model("low") == "novita/zai-org/glm-4.5v"


# --- _quality_plan: explicit model wins, unknown tier errors, None passes through ---


def test_quality_plan_none_passes_model_through():
    assert srv._quality_plan(None, "gpt-x") == ("gpt-x", False, None)
    assert srv._quality_plan(None, None) == (None, False, None)


def test_quality_plan_rejects_an_unknown_tier():
    eff, strict, err = srv._quality_plan("ultra", None)
    assert eff is None and strict is False and err.startswith("ERROR") and "low" in err


def test_quality_plan_explicit_model_beats_the_tier(monkeypatch):
    monkeypatch.setattr("interact.models.Model.is_available", _avail(True))  # sovereign available
    assert srv._quality_plan("low", "pinned")[0] == "pinned"            # explicit model wins
    assert srv._quality_plan("low", None)[0] == _DEFAULT_SOVEREIGN_MODEL  # else the tier's model
    assert srv._quality_plan("critical", None)[1] is True               # only critical is strict
    assert srv._quality_plan("high", None)[1] is False


# --- the literal wired into the tools ---


def _el(ref: str) -> InteractiveElement:
    return InteractiveElement(ref=ref, role="button", name="Go", x=1, y=1, w=9, h=9, index=1)


def _stub_browser_capture(monkeypatch, elements):
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda target, session: (None, MagicMock(), None))
    monkeypatch.setattr(srv.capture, "_capture_target_png", AsyncMock(return_value=b"PNG"))
    monkeypatch.setattr(srv.capture, "_scan_elements", AsyncMock(return_value=elements))


@pytest.mark.asyncio
async def test_review_ui_quality_low_picks_the_sovereign_model(monkeypatch):
    _stub_browser_capture(monkeypatch, [])
    monkeypatch.setattr("interact.models.Model.is_available", _avail(True))  # sovereign reachable
    captured: dict = {}

    async def fake_vlm(data, context, prompt, *, response_format=None, model_override=None, **kw):
        captured["model_override"] = model_override
        return VLMResult(text=UIReview(screen="X", looks_ok=True, findings=[]).model_dump_json(), elapsed=1, model="m")

    monkeypatch.setattr(srv.vlm, "_vlm", fake_vlm)
    await srv.review_ui(quality="low")
    assert captured["model_override"] == _DEFAULT_SOVEREIGN_MODEL  # the tier chose the model for the agent


@pytest.mark.asyncio
async def test_review_ui_rejects_a_bad_quality_value(monkeypatch):
    _stub_browser_capture(monkeypatch, [])
    out = await srv.review_ui(quality="ultra")
    assert out.startswith("ERROR") and "low" in out


@pytest.mark.asyncio
async def test_review_ui_critical_drops_a_finding_with_a_phantom_ref(monkeypatch):
    # critical → resolve_quality_model returns "" (not low/medium), so no availability patch needed.
    _stub_browser_capture(monkeypatch, [_el("e1")])

    async def fake_vlm(data, context, prompt, *, response_format=None, model_override=None, **kw):
        review = UIReview(screen="X", looks_ok=False, findings=[
            UIFinding(severity="major", category="alignment", ref="e1", location="Go", issue="off", suggestion="fix"),
            UIFinding(severity="minor", category="contrast", ref="e9", location="ghost", issue="x", suggestion="y"),
        ]).model_dump_json()
        return VLMResult(text=review, elapsed=1, model="m")

    monkeypatch.setattr(srv.vlm, "_vlm", fake_vlm)
    out = await srv.review_ui(quality="critical")
    assert "1 issue(s)" in out and "Go" in out  # the real-ref finding survives
    assert "ghost" not in out  # the phantom-ref finding was dropped (strict critical)


@pytest.mark.asyncio
async def test_verify_ui_critical_downgrades_a_pass_on_a_phantom_ref(monkeypatch):
    _stub_browser_capture(monkeypatch, [_el("e1")])

    async def fake_vlm(data, context, prompt, *, response_format=None, model_override=None, **kw):
        rep = VerifyReport(screen="X", all_pass=True, checks=[
            RequirementCheck(requirement="r", verdict="pass", ref="e9", element="ghost", evidence="seen"),
        ]).model_dump_json()
        return VLMResult(text=rep, elapsed=1, model="m")

    monkeypatch.setattr(srv.vlm, "_vlm", fake_vlm)
    out = await srv.verify_ui(["r"], quality="critical")
    assert "UNCLEAR" in out and "0/1 PASS" in out  # a PASS resting on a phantom ref can't stand
    assert "✓ all pass" not in out  # all_pass recomputed after the downgrade
