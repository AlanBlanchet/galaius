"""verify_ui — requirement-anchored ACCEPTANCE (the complement to review_ui's open-ended discovery).
Real usage showed freeform critique is satisfiable by a vague "looks good" that never tests the literal
form-defect; verify_ui judges each requirement PASS/FAIL on its named element. All VLM calls mocked."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from interact import server as srv
from interact.critique import (
    RequirementCheck,
    VerifyReport,
    build_verify_prompt,
    format_verify,
    parse_verify,
)
from interact.vision import VLMResult


def _report(**kw) -> VerifyReport:
    kw.setdefault("screen", "Home")
    kw.setdefault("all_pass", False)
    return VerifyReport(**kw)


def _check(req, verdict, element="el", evidence="ev") -> RequirementCheck:
    return RequirementCheck(requirement=req, verdict=verdict, element=element, evidence=evidence)


def test_build_verify_prompt_numbers_requirements_and_keeps_the_rubric():
    p = build_verify_prompt(["coin is gold not a flame", "nav has 4 tabs"])
    assert "PASS or FAIL" in p and "to the letter" in p
    assert "1. coin is gold not a flame" in p and "2. nav has 4 tabs" in p


def test_build_verify_prompt_compare_mode_judges_the_build_against_reference():
    p = build_verify_prompt(["accent is teal"], compare=True)
    assert "REFERENCE" in p and "BUILD" in p and "1. accent is teal" in p


def test_format_verify_counts_passes_and_marks_each():
    rep = _report(checks=[
        _check("coin gold", "fail", "coin pill", "orange flame, not gold"),
        _check("4 tabs", "pass", "bottom nav", "4 tabs present"),
        _check("FAB clear", "unclear", "FAB", "occluded"),
    ])
    out = format_verify(rep)
    assert "1/3 PASS" in out
    assert "[FAIL] coin gold" in out and "orange flame" in out
    assert "[PASS] 4 tabs" in out and "[UNCLEAR] FAB clear" in out


def test_format_verify_flags_all_pass():
    rep = _report(all_pass=True, checks=[_check("x", "pass")])
    assert "✓ all pass" in format_verify(rep)


def test_build_verify_prompt_embeds_detected_elements_for_grounding():
    from interact.critique import format_grounding
    from interact.state import InteractiveElement

    grounding = format_grounding([InteractiveElement(ref="e3", role="link", name="Home", x=0, y=0, w=40, h=12, index=1)])
    p = build_verify_prompt(["nav has Home"], grounding=grounding)
    assert "DETECTED ELEMENTS" in p and "e3: link" in p
    assert "DETECTED ELEMENTS" not in build_verify_prompt(["nav has Home"])  # absent without grounding


def test_format_verify_flags_a_check_citing_an_undetected_ref():
    rep = _report(checks=[
        RequirementCheck(requirement="Home tab present", verdict="pass", ref="e3", element="nav", evidence="present"),
        RequirementCheck(requirement="Cart present", verdict="fail", ref="e99", element="ghost", evidence="not found"),
    ])
    out = format_verify(rep, valid_refs={"e3"})
    assert "[e3]" in out and "e3 ?unverified" not in out
    assert "e99 ?unverified" in out  # a cited ref the scan never produced → flagged


def test_parse_verify_handles_a_fallback_banner_and_no_json():
    payload = _report(all_pass=True, checks=[_check("x", "pass")]).model_dump_json()
    assert parse_verify(payload).all_pass is True
    assert parse_verify(f"[Fallback: used b after a]\n\n{payload}").screen == "Home"
    assert parse_verify("no json here") is None


@pytest.mark.asyncio
async def test_verify_ui_returns_per_requirement_verdicts(monkeypatch):
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda target, session: (None, MagicMock(), None))
    monkeypatch.setattr(srv.capture, "_capture_target_png", AsyncMock(return_value=b"PNG"))
    captured: dict = {}

    async def fake_vlm(data, context, prompt, *, response_format=None, model_override=None, **kw):
        captured["prompt"], captured["rf"] = prompt, response_format
        rep = _report(checks=[_check("coin is gold", "fail", "coin pill", "orange flame")]).model_dump_json()
        return VLMResult(text=rep, elapsed=1.0, model="m")

    monkeypatch.setattr(srv.vlm, "_vlm", fake_vlm)
    out = await srv.verify_ui(requirements=["coin is gold, not a flame"])
    assert "0/1 PASS" in out and "[FAIL] coin is gold" in out and "orange flame" in out
    assert "1. coin is gold, not a flame" in captured["prompt"]  # requirement reached the rubric
    assert captured["rf"] is VerifyReport


@pytest.mark.asyncio
async def test_verify_ui_reference_comparison_passes_both_images(monkeypatch, tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"REFPNG")
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda target, session: (None, MagicMock(), None))
    monkeypatch.setattr(srv.capture, "_capture_target_png", AsyncMock(return_value=b"BUILDPNG"))
    captured: dict = {}

    async def fake_vlm(data, context, prompt, *, response_format=None, extra_images=None, **kw):
        captured.update(data=data, extra=extra_images, prompt=prompt)
        return VLMResult(text=_report(all_pass=True, checks=[_check("accent teal", "pass")]).model_dump_json(),
                         elapsed=1.0, model="m")

    monkeypatch.setattr(srv.vlm, "_vlm", fake_vlm)
    out = await srv.verify_ui(requirements=["accent is teal"], reference=str(ref))
    assert "1/1 PASS" in out
    assert captured["data"] == b"REFPNG" and captured["extra"] == [b"BUILDPNG"]  # ref first, build second
    assert "REFERENCE" in captured["prompt"]


@pytest.mark.asyncio
async def test_verify_ui_requires_at_least_one_requirement():
    out = await srv.verify_ui(requirements=[])
    assert out.startswith("ERROR") and "at least one requirement" in out


@pytest.mark.asyncio
async def test_verify_ui_on_a_file_target_does_not_capture(monkeypatch, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"FILEPNG")
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: pytest.fail("must not capture a file: target"))
    captured: dict = {}

    async def fake_vlm(data, context, prompt, **kw):
        captured["data"], captured["context"] = data, context
        return VLMResult(text=_report(all_pass=True, checks=[_check("x", "pass")]).model_dump_json(),
                         elapsed=0.5, model="m")

    monkeypatch.setattr(srv.vlm, "_vlm", fake_vlm)
    out = await srv.verify_ui(requirements=["x present"], target=f"file:{img}")
    assert "1/1 PASS" in out and captured["data"] == b"FILEPNG" and "Image file:" in captured["context"]


@pytest.mark.asyncio
async def test_verify_ui_degrades_to_raw_text_when_schema_unparsable(monkeypatch):
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda target, session: (None, MagicMock(), None))
    monkeypatch.setattr(srv.capture, "_capture_target_png", AsyncMock(return_value=b"PNG"))
    monkeypatch.setattr(srv.vlm, "_vlm", AsyncMock(return_value=VLMResult(
        text="[Vision unavailable — key not configured]", elapsed=0, model="m")))
    out = await srv.verify_ui(requirements=["x"])
    assert "Vision unavailable" in out