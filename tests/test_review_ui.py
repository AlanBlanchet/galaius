"""review_ui — a first-class UI critique. Real usage showed agents hand-rolling the same "flag every
low-contrast / overflow / misaligned element" vision prompt over and over (and most never judging the
UI at all); this turns that into one tool that returns structured, severity-sorted defects."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from interact import server as srv
from interact.critique import (
    UIFinding,
    UIReview,
    build_review_prompt,
    format_grounding,
    format_review,
    parse_review,
)
from interact.state import InteractiveElement
from interact.vision import VLMResult


def _review(**kw) -> UIReview:
    kw.setdefault("screen", "Home")
    kw.setdefault("looks_ok", False)
    return UIReview(**kw)


def test_build_review_prompt_appends_focus_without_dropping_the_rubric():
    base = build_review_prompt()
    assert "contrast" in base.lower() and "overflow" in base.lower()
    focused = build_review_prompt("background should be sand, not purple")
    assert focused.startswith(base)
    assert "sand, not purple" in focused


def test_format_review_sorts_by_severity_and_is_compact():
    review = _review(findings=[
        UIFinding(severity="minor", category="alignment", location="footer", issue="2px off", suggestion="align"),
        UIFinding(severity="critical", category="overflow", location="bottom", issue="BOTTOM OVERFLOWED stripe", suggestion="wrap in scroll"),
        UIFinding(severity="major", category="contrast", location="XP text", issue="grey on cream", suggestion="darken"),
    ])
    out = format_review(review)
    assert out.index("critical") < out.index("major") < out.index("minor")  # severity-sorted
    assert "BOTTOM OVERFLOWED" in out and "3 issue(s)" in out


def test_format_review_reports_a_clean_screen():
    assert "no defects" in format_review(_review(looks_ok=True, findings=[]))


def test_parse_review_handles_a_fallback_banner_and_stray_prose():
    payload = _review(looks_ok=True, findings=[]).model_dump_json()
    assert parse_review(payload).screen == "Home"
    assert parse_review(f"[Fallback: used b after a failed]\n\n{payload}").looks_ok is True
    assert parse_review("Here is the review:\n" + payload + "\nDone.").screen == "Home"
    assert parse_review("no json here at all") is None


@pytest.mark.asyncio
async def test_review_ui_returns_structured_findings(monkeypatch):
    monkeypatch.setattr(srv, "_resolve_target", lambda target, session: (None, MagicMock(), None))
    monkeypatch.setattr(srv, "_capture_target_png", AsyncMock(return_value=b"PNG"))
    captured: dict = {}

    async def fake_vlm(data, context, prompt, *, response_format=None, model_override=None, **kw):
        captured["prompt"], captured["rf"] = prompt, response_format
        review = _review(findings=[
            UIFinding(severity="critical", category="contrast", location="title", issue="white on white", suggestion="darken"),
        ]).model_dump_json()
        return VLMResult(text=review, elapsed=1.1, model="test-model")

    monkeypatch.setattr(srv, "_vlm", fake_vlm)
    out = await srv.review_ui(focus="check the title contrast")
    assert "1 issue(s)" in out and "white on white" in out
    assert "check the title contrast" in captured["prompt"]  # focus reached the rubric
    assert captured["rf"] is UIReview  # asked the VLM for structured output


def test_build_review_prompt_compare_mode_judges_against_the_reference():
    p = build_review_prompt(compare=True)
    assert "REFERENCE" in p and "BUILD" in p and "diverge" in p.lower()


@pytest.mark.asyncio
async def test_review_ui_reference_comparison_passes_both_images(monkeypatch, tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"REFPNG")
    monkeypatch.setattr(srv, "_resolve_target", lambda target, session: (None, MagicMock(), None))
    monkeypatch.setattr(srv, "_capture_target_png", AsyncMock(return_value=b"BUILDPNG"))
    captured: dict = {}

    async def fake_vlm(data, context, prompt, *, response_format=None, model_override=None, extra_images=None, **kw):
        captured.update(data=data, prompt=prompt, extra=extra_images)
        review = _review(findings=[
            UIFinding(severity="critical", category="color", location="accent", issue="reference is teal, build is lime", suggestion="use teal"),
        ]).model_dump_json()
        return VLMResult(text=review, elapsed=1.0, model="m")

    monkeypatch.setattr(srv, "_vlm", fake_vlm)
    out = await srv.review_ui(reference=str(ref))
    assert "reference is teal, build is lime" in out
    assert captured["data"] == b"REFPNG" and captured["extra"] == [b"BUILDPNG"]  # ref first, build second
    assert "REFERENCE" in captured["prompt"]  # the divergence rubric, not the generic one


@pytest.mark.asyncio
async def test_review_ui_missing_reference_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(srv, "_resolve_target", lambda target, session: (None, MagicMock(), None))
    monkeypatch.setattr(srv, "_capture_target_png", AsyncMock(return_value=b"P"))
    out = await srv.review_ui(reference="/no/such/ref.png")
    assert out.startswith("ERROR") and "reference" in out


@pytest.mark.asyncio
async def test_review_ui_degrades_to_raw_text_when_schema_unparsable(monkeypatch):
    monkeypatch.setattr(srv, "_resolve_target", lambda target, session: (None, MagicMock(), None))
    monkeypatch.setattr(srv, "_capture_target_png", AsyncMock(return_value=b"PNG"))
    monkeypatch.setattr(srv, "_vlm", AsyncMock(return_value=VLMResult(
        text="[Vision unavailable — key not configured]", elapsed=0, model="m")))
    out = await srv.review_ui()
    assert "Vision unavailable" in out  # graceful: the raw VLM text, not a crash


# --- element-grounded critique: the strongest hallucination reducer (OmniParser/Set-of-Mark) ---


def _el(ref: str, name: str = "Save") -> InteractiveElement:
    return InteractiveElement(ref=ref, role="button", name=name, x=5, y=5, w=80, h=24, index=1)


def test_format_grounding_lists_ref_role_name_and_position():
    block = format_grounding([_el("e1", "Save"), _el("e2", "Cancel")])
    assert "e1: button \"Save\" @ (5,5 80×24)" in block
    assert "e2: button \"Cancel\"" in block


def test_build_review_prompt_embeds_the_detected_elements_and_asks_for_a_ref():
    grounding = format_grounding([_el("e1")])
    p = build_review_prompt(grounding=grounding)
    assert "DETECTED ELEMENTS" in p and "e1: button" in p
    assert "ref" in p.lower()  # the model is told to anchor each finding to a ref
    # without grounding the block is absent (desktop/file target) — unchanged behaviour
    assert "DETECTED ELEMENTS" not in build_review_prompt()


def test_format_review_flags_a_ref_the_scan_never_detected():
    review = _review(findings=[
        UIFinding(severity="major", category="alignment", ref="e1", location="Save", issue="2px off", suggestion="align"),
        UIFinding(severity="minor", category="contrast", ref="e9", location="ghost", issue="low", suggestion="darken"),
    ])
    out = format_review(review, valid_refs={"e1"})
    assert "[e1]" in out and "e1 ?unverified" not in out  # a real ref shown, not flagged
    assert "e9 ?unverified" in out  # cited-but-undetected ref → flagged as a likely hallucination


@pytest.mark.asyncio
async def test_review_ui_grounds_the_prompt_and_flags_a_hallucinated_ref(monkeypatch):
    monkeypatch.setattr(srv, "_resolve_target", lambda target, session: (None, MagicMock(), None))
    monkeypatch.setattr(srv, "_capture_target_png", AsyncMock(return_value=b"PNG"))
    monkeypatch.setattr(srv, "_scan_elements", AsyncMock(return_value=[_el("e1", "Save")]))
    captured: dict = {}

    async def fake_vlm(data, context, prompt, *, response_format=None, model_override=None, **kw):
        captured["prompt"] = prompt
        review = _review(findings=[
            UIFinding(severity="major", category="alignment", ref="e1", location="Save button", issue="2px off", suggestion="align"),
            UIFinding(severity="minor", category="contrast", ref="e9", location="a control that isn't there", issue="low", suggestion="x"),
        ]).model_dump_json()
        return VLMResult(text=review, elapsed=1.0, model="m")

    monkeypatch.setattr(srv, "_vlm", fake_vlm)
    out = await srv.review_ui()
    assert "DETECTED ELEMENTS" in captured["prompt"] and "e1: button" in captured["prompt"]  # grounded
    assert "[e1]" in out  # real ref surfaced as the finding's anchor
    assert "e9 ?unverified" in out  # the model invented e9 → flagged, not trusted
