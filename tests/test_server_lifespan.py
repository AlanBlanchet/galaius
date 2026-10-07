"""Nothing the MCP server does in the background is allowed to sit on the request loop.

The loop that answers `initialize` is the loop a client times out on: registry maintenance ran
there, a pass over live runs' raw transcripts took 22 s of a 30 s connect budget, and a session
that never connected still listed galaius's tools to every agent it spawned (#224).
"""

import asyncio
import threading

import pytest

from galaius.agents import run as run_module
from galaius.server import core, sandbox


@pytest.fixture
def quiet_lifespan(monkeypatch):
    """Everything the lifespan starts BESIDE the mirror, silenced — no network, no signals."""
    monkeypatch.setattr("galaius.live_sources.refresh_in_background", lambda: None)
    monkeypatch.setattr(sandbox, "install_teardown_handlers", lambda: None)

    async def idle(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(sandbox, "_idle_session_reaper", idle)


async def test_registry_mirror_never_runs_on_the_request_loop(quiet_lifespan, monkeypatch):
    seen: dict[str, object] = {}
    started = threading.Event()

    async def probe(alive, *, interval: float = 1.0):
        seen["thread"] = threading.get_ident()
        seen["loop"] = asyncio.get_running_loop()
        started.set()
        while alive():
            await asyncio.sleep(0.01)
        seen["stopped"] = True

    monkeypatch.setattr(run_module, "_mirror_running_runs", probe)
    async with core._lifespan(core.mcp):
        assert started.wait(5), "the run mirror never started"
        assert seen["thread"] != threading.get_ident()
        assert seen["loop"] is not asyncio.get_running_loop()
    assert seen.get("stopped"), "leaving the lifespan must stop the mirror"


async def test_a_blocking_mirror_pass_leaves_the_request_loop_answering(quiet_lifespan, monkeypatch):
    """The real pass blocks on file reads and JSON parsing; the request loop must not wait."""
    import time

    running = threading.Event()

    async def hog(alive, *, interval: float = 1.0):
        running.set()
        while alive():
            time.sleep(0.05)  # a registry pass, synchronous, as it really is
            await asyncio.sleep(0)

    monkeypatch.setattr(run_module, "_mirror_running_runs", hog)
    async with core._lifespan(core.mcp):
        assert running.wait(5)
        deadline = time.monotonic() + 1.0
        turns = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(0)
            turns += 1
        assert turns > 1000, f"request loop starved by background maintenance ({turns} turns/s)"
