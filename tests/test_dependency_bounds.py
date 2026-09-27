"""A dependency that may take a MAJOR version on its own will eventually take a breaking one.

2026-08-19: `interact mcp` died with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`
for every client — the product's primary surface, gone. Nothing in this repo had changed. The
requirement read `mcp[cli]>=1.27.0` with no upper bound, `uv tool install` resolved it fresh, and
**mcp 2.0.0** arrived having moved that module. The lockfile kept development on 1.27.0, so the
whole test suite stayed green while the installed server could not start at all — the worst shape
of failure: invisible where you look, total where the user runs it.

So a runtime dependency that decides whether the server starts is capped below the next major.
Upgrading is a deliberate act with a migration, never something a fresh install does silently.
"""

import re
import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
DEPS: list[str] = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]

# The ones whose major bump takes the product down rather than merely changing a detail.
LOAD_BEARING = ("mcp", "playwright", "litellm", "pydantic", "fastmcp")


def _requirement(name: str) -> str | None:
    for d in DEPS:
        if re.match(rf"^{re.escape(name)}\b", d.strip()):
            return d
    return None


@pytest.mark.parametrize("name", LOAD_BEARING)
def test_a_load_bearing_dependency_cannot_take_a_major_on_its_own(name):
    req = _requirement(name)
    if req is None:
        pytest.skip(f"{name} is not a direct dependency")
    assert re.search(r"[<~=!]=?\s*\d", req.split(">=")[-1]) or "<" in req, (
        f"{req!r} has no upper bound: a fresh install can take the next MAJOR and break the "
        f"server, exactly as mcp 2.0.0 did on 2026-08-19 (fastmcp moved, `interact mcp` would "
        f"not start, while the lockfile kept the tests green on 1.27.0)"
    )


def test_the_server_entry_point_actually_imports():
    """The cheapest possible smoke test for the failure above: if this import breaks, every MCP
    client sees 'server not connected' and no unit test would otherwise notice."""
    import interact.server  # noqa: F401
