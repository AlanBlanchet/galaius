"""Every `galaius.*` module named in text resolves: code inside strings (`python -c`, subprocess
scripts), `-m` arguments, shell / PowerShell scripts and docs. Import checks never read these, so a
module move breaks them silently."""

import importlib.util
import re
import subprocess

import pytest

from tests.support.paths import REPO

SUFFIXES = (".py", ".sh", ".ps1", ".md", ".toml", ".yml", ".yaml", ".ts", ".json")
# Names a test writes on purpose as gone (a stale hook the code must clean up), with the test.
GONE = {"galaius.session_stop_hook": "tests/agents/turns/test_session_stops.py: a stale hook entry to remove"}
NAMED = re.compile(
    r"\bfrom\s+(galaius(?:\.\w+)*)\s+import\s+\(?\s*(\w+)"  # from galaius.<mod> import <name> / import (<name>,
    r"|(?:-m\s+|[\"']-m[\"']\s*,\s*[\"']|\bimport\s+)(galaius(?:\.\w+)+)"  # -m galaius.<mod>, ["-m", "galaius.<mod>"], import galaius.<mod>
    r"|(?:__import__|import_module)\(\s*[\"'](galaius(?:\.\w+)*)[\"']"  # __import__("galaius.<mod>"), import_module("galaius.<mod>")
)


def _named_modules() -> dict[str, set[str]]:
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, check=True, capture_output=True, text=True).stdout.split("\0")
    found: dict[str, set[str]] = {}
    for path in tracked:
        if not path.endswith(SUFFIXES) or not (REPO / path).is_file():
            continue
        for match in NAMED.finditer((REPO / path).read_text(errors="replace")):
            package, name, module, dynamic = match.groups()
            found.setdefault(module or dynamic or f"{package}:{name}", set()).add(path)
    return found


def _resolves(ref: str) -> bool:
    module, _, name = ref.partition(":")
    if importlib.util.find_spec(module) is None:
        return False
    if not name or hasattr(importlib.import_module(module), name):
        return True
    # `from pkg import name` where name is a submodule not yet imported
    return hasattr(importlib.import_module(module), "__path__") and importlib.util.find_spec(f"{module}.{name}") is not None


def test_every_named_galaius_module_resolves() -> None:
    broken = {}
    for ref, paths in _named_modules().items():
        if ref in GONE:
            continue
        try:
            ok = _resolves(ref)
        except ImportError:
            ok = False
        if not ok:
            broken[ref] = sorted(paths)
    assert not broken, "module names that no longer resolve:\n" + "\n".join(f"{ref}  ({', '.join(paths)})" for ref, paths in sorted(broken.items()))


if __name__ == "__main__":
    pytest.main([__file__])
