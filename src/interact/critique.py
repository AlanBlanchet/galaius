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


def build_review_prompt(focus: str | None = None, *, compare: bool = False) -> str:
    """The critique rubric, optionally narrowed by the agent's own `focus` (e.g. 'the background should
    be warm sand, not purple', 'check the bottom nav is not black'). With ``compare=True`` it's the
    reference-vs-build divergence rubric — the fix for isolation-judged false PASSes."""
    base = _COMPARE_RUBRIC if compare else _RUBRIC
    return f"{base}\n\nPay particular attention to: {focus}" if focus else base


_SEV_RANK = {"critical": 0, "major": 1, "minor": 2}


def format_review(review: UIReview) -> str:
    """Render a UIReview as a compact, severity-sorted report for the calling agent."""
    if review.looks_ok and not review.findings:
        return f"UI review of {review.screen}: no defects found (clean)."
    findings = sorted(review.findings, key=lambda f: _SEV_RANK.get(f.severity, 3))
    lines = [f"UI review of {review.screen}: {len(findings)} issue(s) found."]
    for f in findings:
        lines.append(f"- [{f.severity}/{f.category}] {f.location}: {f.issue} → {f.suggestion}")
    return "\n".join(lines)


def parse_review(text: str) -> UIReview | None:
    """Parse a VLM response (JSON per the UIReview schema) into a UIReview. Robust to a leading
    fallback banner or stray prose around the JSON object; returns None if no object parses."""
    try:
        return UIReview.model_validate_json(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return UIReview.model_validate_json(text[start : end + 1])
        except ValueError:
            return None
    return None
