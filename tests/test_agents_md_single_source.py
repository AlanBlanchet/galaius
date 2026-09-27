"""AGENTS.md's `target`-routing paragraph must point at its canonical source (the MCP
server instructions / tool docstrings in ``server/core.py``) instead of re-authoring the
routing rule in its own wording. A restated copy drifts silently — the exact bug this
guards: ``core.py``'s ``_instructions()`` text already used different wording for the
same fact before this fix.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_MD = REPO_ROOT / "AGENTS.md"
CORE_PY = REPO_ROOT / "src/interact/server/core.py"


def test_target_routing_rule_points_at_its_source_instead_of_restating_it():
    text = AGENTS_MD.read_text()
    assert "core.py" in text, "AGENTS.md must name core.py as the source of the target-routing rule"
    assert "browser by default, a desktop window title, a screen selector" not in text, (
        "AGENTS.md must not re-author the target-routing rule in its own wording"
    )


def test_referenced_source_file_actually_defines_the_instructions():
    assert CORE_PY.is_file()
    assert "_instructions" in CORE_PY.read_text()
