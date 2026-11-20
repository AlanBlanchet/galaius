"""What each tool's model resolves to, and why."""

from galaius.models.registry import Model
from galaius.runtime import config


def print_resolved_models(indent: str = "  ") -> None:
    """Print which model each VLM TASK resolves to with current keys — answers "what runs when I
    do X, and why that default?". Organised BY TASK (what a user actually picks) with the config
    knob (the role) named in brackets — different jobs can have different defaults without a
    separate model setting per task (else all resolve to the same best VLM). Real axes: MODALITY
    (image/component/video/audio) and STAKES — review_ui/verify_ui layer a quality tier on the
    image model, the sovereign tier's cheapest clearing model at low/medium, frontier at
    high/critical. Shared by status / providers / doctor / version so every surface tells one
    story. Every role is a CRITERION now — this always shows what it RESOLVES TO, never the bare
    rule text alone, and says plainly when nothing resolves (a blank here is the defect the owner
    has complained about before)."""

    Model.load_registry()

    def _resolved(role: str) -> tuple[str, str]:
        """(display string, bare model id) for a role given the current keys."""
        rule = config.criteria_for(role)
        try:
            mid = config.resolve_model(role)
        except RuntimeError as err:
            return f"— (criterion: {rule})  ⚠ {err}", ""
        return f"{mid} (criterion: {rule})", mid

    def _line(task: str, role: str) -> None:
        print(f"{indent}{task:<33}→ {_resolved(role)[0]}  [{role}]")
        # What it stepped over. "Are we using the best model?" is unanswerable from the chosen id
        # alone — reads the same whether the walk found the strongest available or a stale default
        # returned with no walk at all. Naming the stronger models it couldn't reach, and why,
        # makes the answer checkable and shows which key to add.
        explanation = config.explain_model(role)
        if explanation:
            print(f"{indent}{'':<33}  ↑ {explanation}")

    _line("screenshot(query) · describe", "image")
    _line("get_interactive_elements", "component")
    _line("transcribe", "audio")
    _line("record · video understanding", "video")

    # review_ui / verify_ui pick by STAKES, not role: sovereign tier's cheapest clearing model for
    # a quick look (low/medium), frontier image model for final sign-off (high/critical).
    frontier = _resolved("image")[1] or "—"
    sovereign = config.resolve_quality_model("low")
    if sovereign:
        critique = f"{sovereign} (low/medium tier) · {frontier} (high/critical)"
    else:
        critique = f"{frontier} (all tiers — sovereign criteria {config.tier_sovereign_criteria or '(default)'!r} clears nothing, so low/medium fall back to frontier)"
    print(f"{indent}{'review_ui · verify_ui (critique)':<33}→ {critique}")
    print(f"{indent}{'measure_ui (contrast)':<33}→ deterministic — no model")
