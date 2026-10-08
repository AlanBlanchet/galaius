"""One agent child started before its task, so the next start skips the CLI's own startup.

Claude Code spends ~2.6 s starting (settings, hooks, agent definitions, MCP servers) before the model
reads a word; a child started ahead does that while nobody waits and answers ~2.7 s after its task
arrives instead of ~5.5 s (measured on a 12-core PC, 2026-10-08). The slot holds at most one child,
for the launch the last start made: the next start with the SAME launch (`ChildLaunch.key`: command,
environment, folder, the files the CLI read at its start, the date) claims it; any other start ends it.
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
    """A started child waiting for its task: the run id it was started as, under launch `key`."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    key: str
    run_id: str
    process: asyncio.subprocess.Process
    born: float

    @property
    def alive(self) -> bool:
        return self.process.returncode is None

    def end(self) -> None:
        """Stop it and drop the files it wrote: it never became a run. Its stdin closing ends it
        (no task will come); POSIX also signals its tree, at once (Windows' `taskkill` would block
        the runner's loop)."""
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.alive and os.name == "posix":
            end_process_tree(self.process.pid)
        for path in (reg.raw_events_path(self.run_id), reg.stderr_path(self.run_id)):
            path.unlink(missing_ok=True)


class WarmStart(BaseModel):
    """At most one child started ahead, owned by one long-lived launcher (the PC's runner)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: Seconds a child waits for its task before it is replaced: what it took at its start that no
    #: file tells (its start hooks' output, the account's connectors, the vendor's own state) is at
    #: most this old.
    max_age: float = 300.0
    #: Seconds between a start and its successor's start, so the two CLIs never share the CPU while
    #: the first one is starting up.
    delay: float = 15.0
    #: A child ending on its own sooner than this after its start is not started again (a CLI that
    #: cannot start here would otherwise be restarted forever).
    shortest_life: float = 60.0
    #: How often a held child is checked for its age and for having ended on its own.
    check_every: float = 5.0

    _held: WarmChild | None = PrivateAttr(default=None)
    _keeping: asyncio.Task | None = PrivateAttr(default=None)

    @property
    def holding(self) -> str | None:
        """The run id of the child waiting now, if any."""
        return self._held.run_id if self._held is not None else None

    def claim(self, key: str) -> WarmChild | None:
        """The held child when it was started for `key` and still waits; any other child is ended."""
        held, self._held = self._held, None
        if held is None:
            return None
        if held.key == key and held.alive and time.monotonic() - held.born < self.max_age:
            return held
        held.end()
        return None

    def prepare(self, key: Callable[[], str], start: Callable[[str], Awaitable[asyncio.subprocess.Process]]) -> None:
        """After `delay`, hold a child `start(run_id)` starts for the next start with the launch
        `key()` names (read just before each child starts: what it reads then is what the key says),
        and keep one held (replaced at `max_age`) until a start claims it or a later `prepare` runs."""
        if self._keeping is not None:
            self._keeping.cancel()
        self._keeping = asyncio.create_task(self._keep(key, start))

    def close(self) -> None:
        if self._keeping is not None:
            self._keeping.cancel()
        if self._held is not None:
            self._held.end()
            self._held = None

    async def _keep(self, key: Callable[[], str], start: Callable[[str], Awaitable[asyncio.subprocess.Process]]) -> None:
        await asyncio.sleep(self.delay)
        while True:
            if self._held is not None:
                self._held.end()
            run_id = str(uuid.uuid4())
            try:
                launch = key()
                child = WarmChild(key=launch, run_id=run_id, process=await start(run_id), born=time.monotonic())
            except (OSError, ValueError, RuntimeError) as error:
                log.warning("no agent started ahead: %s", error)
                self._held = None
                return
            self._held = child
            while self._held is child and child.alive and time.monotonic() - child.born < self.max_age:
                await asyncio.sleep(self.check_every)
            if self._held is not child:
                return  # claimed, or replaced by a later `prepare`
            if not child.alive and time.monotonic() - child.born < self.shortest_life:
                log.warning("the agent started ahead ended on its own after %.0f s; none is kept", time.monotonic() - child.born)
                with suppress(OSError):
                    child.end()
                self._held = None
                return
