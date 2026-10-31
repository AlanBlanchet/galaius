"""Where the suite's own files live, computed once: a test reads ``FIXTURES / "x"``, never its own depth."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"
FIXTURES = TESTS / "fixtures"
EXTENSION = REPO / "clients" / "vscode"
