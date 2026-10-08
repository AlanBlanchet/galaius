"""The children a launcher starts ahead: one per launch for the launches started most recently, each
handed over only while fresh. Real child processes, no CLI: each waits on its stdin as Claude does."""

import asyncio
import sys

import pytest

from galaius.agents.warm import WarmStart
from galaius.processes import process_group_options


async def _waiting(run_id: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(sys.executable, "-c", "import sys; sys.stdin.read()", stdin=asyncio.subprocess.PIPE,
                                                **process_group_options())  # its own tree, as `ChildLaunch.start` gives it


async def _held(warm: WarmStart, count: int) -> None:
    for _ in range(200):
        if len(warm.holding) == count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{len(warm.holding)} children held, {count} expected")


@pytest.mark.asyncio
async def test_two_alternating_launches_both_find_their_child_and_a_third_drops_the_oldest():
    """Alan alternates read-only and write starts (22 of his 49 consecutive web starts changed it)."""
    warm = WarmStart(capacity=2, delay=0, check_every=0.02)
    try:
        warm.prepare("read_only", lambda: "fresh", _waiting)
        warm.prepare("workspace_write", lambda: "fresh", _waiting)
        await _held(warm, 2)
        first, second = warm.claim("read_only", "fresh"), warm.claim("workspace_write", "fresh")
        assert first is not None and second is not None and first.alive and second.alive

        warm.prepare("read_only", lambda: "fresh", _waiting)
        warm.prepare("workspace_write", lambda: "fresh", _waiting)
        await _held(warm, 2)
        dropped = warm.holding[0]
        warm.prepare("another folder", lambda: "fresh", _waiting)
        await _held(warm, 2)
        assert dropped not in warm.holding and warm.claim("read_only", "fresh") is None, "the least recent launch lost its child"
        assert warm.claim("workspace_write", "fresh") is not None
        for child in (first, second):
            child.end()
    finally:
        warm.close()


@pytest.mark.asyncio
async def test_a_child_no_longer_fresh_is_ended_never_handed_a_task():
    warm = WarmStart(capacity=2, delay=0, check_every=0.02)
    try:
        warm.prepare("main", lambda: "before the edit", _waiting)
        await _held(warm, 1)
        assert warm.claim("main", "after the edit") is None and warm.holding == ()
    finally:
        warm.close()
