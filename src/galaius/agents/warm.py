"""Agent children started before their task, so the next start skips the CLI's own startup.

Claude Code spends ~2.6 s starting (settings, hooks, agent definitions, MCP servers) before the model
reads a word; a child started ahead does that while nobody waits and answers ~2.7 s after its task
arrives instead of ~5.5 s (measured on a 12-core PC, 2026-10-08). One child waits per launch for the
`capacity` launches started most recently (a launch: `ChildLaunch.slot`, the command, environment and
folder). A start claims its launch's child only while that child is fresh (`ChildLaunch.key`: the
files the CLI read at its start unchanged, the same date); a stale child is ended, never handed a task.
"""

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress

from pydantic import BaseModel, ConfigDict, PrivateAttr

from galaius.agents import registry as reg
from galaius.processes import end_process_tree

log = logging.getLogger(__name__)


class WarmChild(BaseModel):
    """A started child waiting for its task: the run id it was started as, fresh while `key` holds."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    key: str
    run_id: str
    process: asyncio.subprocess.Process
    born: float
    #: Whether its files are its own (a run it would have started) rather than an existing run's.
    owns_files: bool = True

    @property
    def alive(self) -> bool:
        return self.process.returncode is None

    def end(self) -> None:
        """Stop it (`end_waiting`) and drop the files it wrote when they are its own: it never became a run."""
        end_waiting(self.process)
        for path in (reg.raw_events_path(self.run_id), reg.stderr_path(self.run_id)) if self.owns_files else ():
            path.unlink(missing_ok=True)


def end_waiting(process: asyncio.subprocess.Process) -> None:
    """Stop a child waiting for its task. Its stdin closing ends it (no task will come); POSIX also
    signals its tree, at once (Windows' `taskkill` would block the runner's loop)."""
    if process.stdin is not None:
        process.stdin.close()
    if process.returncode is None and os.name == "posix":
        end_process_tree(process.pid)


class WarmStart(BaseModel):
    """The children started ahead by one long-lived launcher (the PC's runner), one per launch."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: How many launches keep a child, the most recently started ones: one person alternating
    #: between two permissions (or folders) finds both warm. An idle child holds ~0.5 GB.
    capacity: int = 2
    #: Seconds a child waits for its task before it is replaced: what it took at its start that no
    #: file tells (its start hooks' output, the account's connectors, the vendor's own state) is at
    #: most this old.
    max_age: float = 300.0
    #: Seconds between a start and its successor's start, so the two CLIs do not share the CPU while
    #: the first one is starting up.
    delay: float = 5.0
    #: A child ending on its own sooner than this after its start is not started again (a CLI that
    #: cannot start here would otherwise be restarted forever).
    shortest_life: float = 60.0
    #: How often a held child is checked for its age and for having ended on its own.
    check_every: float = 5.0
    #: Seconds after `prepare` a launch stops keeping a child; None: until a start claims it.
    hold: float | None = None

    #: Per launch (`slot`), least recently prepared first: the task keeping its child, the child waiting.
    _keepers: dict[str, asyncio.Task] = PrivateAttr(default_factory=dict)
    _held: dict[str, WarmChild] = PrivateAttr(default_factory=dict)

    @property
    def holding(self) -> tuple[str, ...]:
        """The run ids of the children waiting now."""
        return tuple(child.run_id for child in self._held.values())

    def claim(self, slot: str, key: str) -> WarmChild | None:
        """Launch `slot`'s child when it still waits and is fresh (`key`); a stale one is ended."""
        child = self._held.pop(slot, None)
        if child is None:
            return None
        if child.key == key and child.alive and time.monotonic() - child.born < self.max_age:
            return child
        child.end()
        return None

    def prepare(self, slot: str, key: Callable[[], str], start: Callable[[str], Awaitable[asyncio.subprocess.Process]], *,
                run_id: str | None = None, still: Callable[[], bool] | None = None) -> None:
        """After `delay`, keep a child `start(run_id)` starts for launch `slot` (fresh while `key()`
        holds, read just before each child starts: what it reads then is what the key says),
        replaced at `max_age`, until a start claims it, `hold` passes or `still()` (cheap, asked at
        each check) says no. Beyond `capacity` launches, the least recently prepared one loses its
        child. `run_id`: the existing run every child serves (its next turn); a new run id per child
        when None."""
        if (keeper := self._keepers.pop(slot, None)) is not None:
            keeper.cancel()
        self._keepers[slot] = asyncio.create_task(self._keep(slot, key, start, run_id, still or (lambda: True)))
        while len(self._keepers) > self.capacity:
            self.forget(next(iter(self._keepers)))

    def forget(self, slot: str) -> None:
        """Keep no child for launch `slot` any more; the one waiting is ended."""
        if (keeper := self._keepers.pop(slot, None)) is not None:
            keeper.cancel()
        if (child := self._held.pop(slot, None)) is not None:
            child.end()

    def close(self) -> None:
        for slot in list(self._keepers):
            self.forget(slot)
        for child in self._held.values():
            child.end()
        self._held.clear()

    async def _keep(self, slot: str, key: Callable[[], str], start: Callable[[str], Awaitable[asyncio.subprocess.Process]], serves: str | None,
                    still: Callable[[], bool]) -> None:
        until = None if self.hold is None else time.monotonic() + self.hold
        await asyncio.sleep(self.delay)
        while (until is None or time.monotonic() < until) and still():
            if (former := self._held.pop(slot, None)) is not None:
                former.end()
            run_id = serves or str(uuid.uuid4())
            try:
                fresh = key()
                child = WarmChild(key=fresh, run_id=run_id, process=await start(run_id), born=time.monotonic(), owns_files=serves is None)
            except (OSError, ValueError, RuntimeError) as error:
                log.warning("no agent started ahead: %s", error)
                return
            self._held[slot] = child
            while (self._held.get(slot) is child and child.alive and time.monotonic() - child.born < self.max_age
                   and (until is None or time.monotonic() < until) and still()):
                await asyncio.sleep(self.check_every)
            if self._held.get(slot) is not child:
                return  # claimed, or dropped
            if not child.alive and time.monotonic() - child.born < self.shortest_life:
                log.warning("the agent started ahead ended on its own after %.0f s; none is kept", time.monotonic() - child.born)
                with suppress(OSError):
                    child.end()
                del self._held[slot]
                return
        if (last := self._held.pop(slot, None)) is not None:
            last.end()
        if self._keepers.get(slot) is asyncio.current_task():
            del self._keepers[slot]
