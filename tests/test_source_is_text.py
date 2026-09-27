"""A NUL byte inside a template literal is legal TypeScript, survives the compiler, passes every
test — and makes the whole source file BINARY. `file` reports "data", grep skips it silently
(returning "no match" for a symbol that is plainly there), and any tool that sniffs content stops
seeing it. I shipped exactly that: a Markdown renderer used a NUL as a placeholder while lifting
fenced code blocks out of the text, and the only symptom was a grep that kept insisting a function
did not exist.

Cheap to check, and the failure it prevents is one that looks like a completely different bug.
"""

from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP = {".git", "node_modules", "out", ".venv", "__pycache__", "dist", ".mypy_cache", ".pytest_cache"}
SOURCE = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".css", ".yml", ".yaml", ".sh", ".toml"}


@lru_cache(maxsize=1)
def _source_files() -> tuple[Path, ...]:
    """Walked once. Pruning the skip directories during the walk rather than filtering after it
    matters: rglob descends into .venv and node_modules first and then throws the results away,
    which turned this from a fifth of a second into most of a minute."""
    found: list[Path] = []
    stack = [ROOT]
    while stack:
        for entry in stack.pop().iterdir():
            if entry.is_dir():
                if entry.name not in SKIP:
                    stack.append(entry)
            elif entry.suffix in SOURCE:
                found.append(entry)
    return tuple(found)


def test_there_are_source_files_to_check():
    """A guard whose corpus silently became empty would pass forever."""
    assert len(_source_files()) > 100


@pytest.mark.parametrize("suffix", sorted(SOURCE))
def test_no_source_file_contains_a_nul_byte(suffix):
    offenders = [
        str(p.relative_to(ROOT))
        for p in _source_files()
        if p.suffix == suffix and b"\x00" in p.read_bytes()
    ]
    assert offenders == [], f"NUL byte makes these binary to grep and to `file`: {offenders}"
