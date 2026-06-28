"""UI critique: turn a captured screen into a STRUCTURED list of what's *wrong* with it.

interact's VLM path lets an agent ask a free-form `query` about a screenshot, but real usage shows
agents hand-rolling the same elaborate "flag every low-contrast / overflow / misaligned ... element"
prompt over and over — and most never ask at all, so they act on pixels they never judged. This
bakes that reviewer expertise into one rubric + a typed result, so `review_ui` gives any agent a
defect list without crafting a prompt, and the findings are machine-readable (severity/category/
location) rather than prose to re-parse.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "major", "minor"]
Category = Literal[
    "contrast",       # text hard/impossible to read against its background
    "overflow",       # content clipped, cut off, or a Flutter "BOTTOM OVERFLOWED" stripe
    "truncation",     # text ellipsised / cut where the full string was expected
    "alignment",      # misaligned, overlapping, or inconsistently spaced elements
    "color",          # wrong/off-theme/broken colors (e.g. a background that should be sand, not purple)
    "broken_state",   # an error / empty / loading / placeholder / "null"/"undefined" state left on screen
    "occlusion",      # a black or blank region hiding content; an element behind another
    "affordance",     # a control too small to tap, or whose purpose/state is unclear
    "consistency",    # diverges from the rest of the UI's language (mismatched styles, stray English, ...)
    "other",
]


class UIFinding(BaseModel):
    severity: Severity = Field(description="critical = unreadable/broken/blocks use; major = clearly noticeable defect; minor = polish")
    category: Category
    ref: str | None = Field(default=None, description="the DETECTED-ELEMENTS ref this finding concerns (e.g. \"e7\"), when it maps to one of the listed elements; null for a whole-background/region issue")
    location: str = Field(description="WHERE on screen, specifically — name the element + its position (e.g. \"the 'X / Y XP' text under 'Camp Level 1', top-left\")")
    issue: str = Field(description="WHAT is wrong, concretely — name colors, the cut-off text, the overlap; never a vague 'looks off'")
    suggestion: str = Field(description="a short, concrete fix")


class UIReview(BaseModel):
    screen: str = Field(description="what screen/page this is (title or best description)")
    looks_ok: bool = Field(description="true ONLY if there are no real defects — an honest clean bill")
    findings: list[UIFinding] = Field(default_factory=list, description="every defect found, most severe first")


_RUBRIC = """You are a meticulous, skeptical UI reviewer. You are shown a screenshot of an app or web \
page. Find what is WRONG with it — do not describe what is right, do not be reassuring. Inspect for:

- CONTRAST / readability: any text hard or impossible to read (low contrast, white-on-light, \
near-invisible). Name the text and the two colors involved.
- OVERFLOW / clipping: content cut off at an edge, a Flutter "BOTTOM OVERFLOWED" yellow-black stripe, \
a scrollbar implying hidden content that should fit.
- TRUNCATION: labels ellipsised ("…") or cut where the full string was clearly intended.
- ALIGNMENT / spacing: misaligned, overlapping, or unevenly spaced elements; a large awkward empty band.
- COLOR / theme: wrong or broken colors, an off-theme background, mismatched accents.
- BROKEN STATE: an error / empty / loading / placeholder state, a broken image, raw "null"/"undefined", \
or missing data left visible.
- OCCLUSION: a black or blank region hiding content; an element drawn behind another.
- AFFORDANCE: a tap target too small, or a control whose purpose or state is unclear.
- CONSISTENCY: anything diverging from the rest of the UI (stray English in a French app, a mismatched \
control style).

Report every real defect as a finding with a precise location, the concrete problem, a severity, and a \
short fix. Be specific and grounded in what is actually visible — never invent a defect to fill the list. \
Judge INTRINSIC, observable defects only — not subjective taste ("is it modern / premium / on-trend"), \
which invites confident-but-wrong verdicts. Do not assume a brand, palette, or design intent you cannot \
see on screen; if matching a specific design is what matters, a reference image is supplied — say so \
rather than guessing the intent. If the screen is genuinely clean, set looks_ok=true and return no findings."""

# When a reference image is supplied: judge the BUILD against the REFERENCE, not a generic ideal — the
# real-usage failure mode was the VLM "PASS"-ing a build it judged in isolation (lime accent vs the
# reference's teal, missing nav) because it never saw the two together.
_COMPARE_RUBRIC = """You are comparing two screenshots: IMAGE 1 is the REFERENCE (the target/design to \
match) and IMAGE 2 is the BUILD (what was produced). Report every way the BUILD DIVERGES from the \
REFERENCE — wrong colors/accent/theme (name both colors), missing or extra sections, different layout or \
spacing, a different font weight, missing nav/header/footer, content present in one but not the other. \
Each divergence is a finding: location = where, issue = "reference has X, build has Y", with a severity \
(critical = wrong theme/accent or a missing major region; major = a clear visible mismatch; minor = small \
drift) and a short fix. Judge ONLY against the reference shown — do not invent an ideal. List intrinsic \
build defects (unreadable text, overflow, broken state) too. If the build faithfully matches, looks_ok=true."""


# Grounding the critique to interact's already-detected element list is the single biggest
# hallucination reducer for UI judgement — bigger than swapping the model. A pure-vision model invents
# elements / wrong coordinates until it's handed the parsed element list and told to reference it, at
# which point grounding accuracy jumps sharply (OmniParser arXiv:2408.00203, Set-of-Mark
# arXiv:2310.11441). So when interact has a reliable element list (the browser DOM-ref scan) we hand it
# over and require each finding to cite a `ref` — turning "looks misaligned" into a checkable claim and
# letting a finding that cites a non-existent ref be flagged as a likely hallucination.
_GROUNDING_NOTE = (
    "\n\nDETECTED ELEMENTS — interact located these on the screen, as `ref: role \"name\" @ (x,y w×h)`. "
    "For EVERY finding, set its `ref` to the element it concerns when that element is one of these (use "
    "null only for a whole-background / region issue). Do NOT report a defect about an element that is "
    "neither visible in the screenshot nor in this list — if you cannot tie it to a ref or a clear "
    "on-screen region, leave it out:\n"
)


def format_grounding(elements: list) -> str:
    """A compact list of interact's DETECTED elements (ref, role, name, position) for the model to
    ANCHOR each finding to — the strongest single hallucination reducer for UI critique (give it the
    real element list and require it to reference it, rather than inventing element descriptions).
    Empty list → ``""`` (no grounding block, e.g. a desktop/file target with no reliable scan)."""
    lines = []
    for el in elements:
        ref = getattr(el, "ref", None) or f"#{el.index}"
        name = f' "{el.name}"' if el.name else ""
        lines.append(f"  {ref}: {el.role}{name} @ ({el.x},{el.y} {el.w}×{el.h})")
    return "\n".join(lines)


def build_review_prompt(
    focus: str | None = None, *, compare: bool = False, grounding: str | None = None
) -> str:
    """The critique rubric, optionally narrowed by the agent's own `focus` (e.g. 'the background should
    be warm sand, not purple', 'check the bottom nav is not black'). With ``compare=True`` it's the
    reference-vs-build divergence rubric — the fix for isolation-judged false PASSes. ``grounding`` is
    interact's detected-element list (from ``format_grounding``); when present the model is told to
    anchor each finding to a ref, the strongest hallucination reducer."""
    out = _COMPARE_RUBRIC if compare else _RUBRIC
    if grounding:
        out += _GROUNDING_NOTE + grounding
    if focus:
        out += f"\n\nPay particular attention to: {focus}"
    return out


_SEV_RANK = {"critical": 0, "major": 1, "minor": 2}


def _ref_anchor(ref: str | None, valid_refs: set[str] | None) -> str:
    """The ` [ref]` tag for a finding/check, marking `?unverified` when the model cited a ref that
    interact never detected — a likely hallucinated element surfaced for the agent rather than trusted."""
    if not ref:
        return ""
    unknown = valid_refs is not None and ref not in valid_refs
    return f" [{ref}{' ?unverified' if unknown else ''}]"


def format_review(review: UIReview, valid_refs: set[str] | None = None) -> str:
    """Render a UIReview as a compact, severity-sorted report for the calling agent. When ``valid_refs``
    is given (interact's detected refs), a finding citing a ref outside that set is marked
    ``?unverified`` — a caught hallucination instead of a trusted defect."""
    if review.looks_ok and not review.findings:
        return f"UI review of {review.screen}: no defects found (clean)."
    findings = sorted(review.findings, key=lambda f: _SEV_RANK.get(f.severity, 3))
    lines = [f"UI review of {review.screen}: {len(findings)} issue(s) found."]
    for f in findings:
        lines.append(f"- [{f.severity}/{f.category}]{_ref_anchor(f.ref, valid_refs)} {f.location}: {f.issue} → {f.suggestion}")
    return "\n".join(lines)


def _parse_json_model(text: str, cls: type[BaseModel]):
    """Validate ``text`` as JSON for ``cls``, tolerating a leading fallback banner / stray prose
    around the object (first ``{`` … last ``}``). Returns the model or None."""
    try:
        return cls.model_validate_json(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return cls.model_validate_json(text[start : end + 1])
        except ValueError:
            return None
    return None


def parse_review(text: str) -> UIReview | None:
    """Parse a VLM response (JSON per the UIReview schema) into a UIReview. Robust to a leading
    fallback banner or stray prose around the JSON object; returns None if no object parses."""
    return _parse_json_model(text, UIReview)


# ── verify_ui: requirement-anchored ACCEPTANCE (the complement to review_ui's discovery) ──────────
# Real usage showed freeform critique is satisfiable by a vague "looks good" that never tests the
# literal form-defect ("is this icon dark or COLORED?"). verify_ui judges each of the user's exact
# requirements PASS/FAIL on the rendered pixels, naming the element + observed value as evidence.
ReqVerdict = Literal["pass", "fail", "unclear"]


class RequirementCheck(BaseModel):
    requirement: str = Field(description="the requirement being judged, echoed verbatim")
    verdict: ReqVerdict = Field(description="pass = met to the letter; fail = not met OR not found; unclear = element genuinely occluded/ambiguous")
    ref: str | None = Field(default=None, description="the DETECTED-ELEMENTS ref this check judged (e.g. \"e7\"), when it maps to one of the listed elements; null otherwise")
    element: str = Field(description="the EXACT element judged + its position (e.g. \"the coin pill, top-right\")")
    evidence: str = Field(description="the OBSERVED value that decided it (e.g. \"shows an orange flame icon, not a gold coin\")")


class VerifyReport(BaseModel):
    screen: str = Field(description="what screen/page this is")
    all_pass: bool = Field(description="true ONLY if every requirement is PASS")
    checks: list[RequirementCheck] = Field(default_factory=list, description="one per requirement, in order")


_VERIFY_RUBRIC = """You are a STRICT acceptance reviewer. You are shown a screenshot and a numbered \
list of REQUIREMENTS the screen must meet. Judge EACH requirement PASS or FAIL on the rendered pixels — \
exactly as written, to the letter. Rules:

- Judge the LITERAL words. "a GOLD coin, not a flame" is FAIL if you see an orange flame, even though \
an icon IS present — presence is not enough, the FORM must match (color, shape, count, position, state, \
text). This presence-but-wrong-form case is the one freeform critique misses; catch it.
- Name the EXACT element you judged and the OBSERVED value as evidence ("the coin pill, top-right: an \
orange flame icon").
- A requirement you cannot find or confirm on screen is FAIL. Use "unclear" ONLY when the element is \
genuinely occluded/ambiguous — never as a soft pass.
- Do NOT pass a requirement because the screen looks good overall; each requirement stands alone, and \
do not be reassuring.
- Judge only what is visible. Set all_pass=true ONLY when every requirement is PASS."""

_VERIFY_COMPARE_RUBRIC = """You are a STRICT acceptance reviewer. IMAGE 1 is the REFERENCE (the target \
to match); IMAGE 2 is the BUILD. Judge EACH numbered REQUIREMENT below PASS or FAIL on the BUILD, using \
the REFERENCE as the source of truth for intent. Same rules: judge the literal words; presence is not \
enough (the form/color/count must match); name the element + observed value as evidence; not-found or \
not-matching is FAIL; "unclear" only for genuine occlusion. all_pass=true only if every requirement \
passes on the build."""


def build_verify_prompt(
    requirements: list[str], focus: str | None = None, *, compare: bool = False,
    grounding: str | None = None,
) -> str:
    """The acceptance rubric with the user's requirements embedded as a numbered checklist. With
    ``compare=True`` it judges the build against a supplied reference image. ``grounding`` is interact's
    detected-element list; when present each check is told to anchor to a ref (hallucination reducer)."""
    base = _VERIFY_COMPARE_RUBRIC if compare else _VERIFY_RUBRIC
    reqs = "\n".join(f"{i}. {r}" for i, r in enumerate(requirements, 1))
    out = f"{base}\n\nREQUIREMENTS:\n{reqs}"
    if grounding:
        out += _GROUNDING_NOTE + grounding
    if focus:
        out += f"\n\nPay particular attention to: {focus}"
    return out


_VERDICT_MARK = {"pass": "PASS", "fail": "FAIL", "unclear": "UNCLEAR"}


def format_verify(report: VerifyReport, valid_refs: set[str] | None = None) -> str:
    """Render a VerifyReport as a compact PASS/FAIL checklist for the calling agent. When ``valid_refs``
    is given, a check citing a ref outside interact's detected set is marked ``?unverified``."""
    n_pass = sum(1 for c in report.checks if c.verdict == "pass")
    head = f"verify_ui — {report.screen}: {n_pass}/{len(report.checks)} PASS"
    if report.all_pass and report.checks:
        head += "  ✓ all pass"
    lines = [head]
    for c in report.checks:
        lines.append(f"- [{_VERDICT_MARK.get(c.verdict, c.verdict)}]{_ref_anchor(c.ref, valid_refs)} {c.requirement} — {c.element}: {c.evidence}")
    return "\n".join(lines)


def parse_verify(text: str) -> VerifyReport | None:
    """Parse a VLM response (JSON per VerifyReport) — robust to a fallback banner / stray prose."""
    return _parse_json_model(text, VerifyReport)
