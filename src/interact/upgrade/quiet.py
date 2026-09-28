"""The worker's side of an upgrade: noticing that the pointer moved, and saying when it can go.

Every supervised member supplies one predicate, "do I hold something a swap would lose?", and
calls `step` on its own beat (or runs `watch`). A relayed worker (MCP, console) REPORTS the answer:
its supervisor picks the moment. Any other worker LEAVES: `step` raises `UpgradeReady` once it
holds nothing, and the CLI exits `EXIT_UPGRADE`."""

import asyncio
import os
import time
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from interact.private_files import PRIVATE_FILES
from interact.upgrade.store import RuntimeStore
from interact.upgrade.supervisor import Supervision, WorkerState


class UpgradeReady(Exception):
    """This worker is quiet and another runtime is active: leave so the supervisor starts it."""


class QuietPoint(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    supervision: Supervision
    store: RuntimeStore
    seen: tuple[tuple[int, int] | None, bool] | None = None

    @classmethod
    def current(cls) -> "QuietPoint | None":
        """None when this process is not a supervised worker, or cannot read its supervisor's
        contract (recorded: it then keeps running and upgrades only when restarted)."""
        supervision = Supervision.current()
        if supervision is None and Supervision.variable in os.environ:
            RuntimeStore.default().record("failed", f"a worker (pid {os.getpid()}) cannot read its supervisor's {Supervision.variable}: it upgrades only when restarted")
        return None if supervision is None else cls(supervision=supervision, store=RuntimeStore.default())

    def waiting(self) -> bool:
        """Another runtime than this worker's is active (read again only when the pointer moved)."""
        signature = self.store.signature()
        if self.seen is None or self.seen[0] != signature:
            self.seen = (signature, self.store.active().path != self.supervision.runtime)
        return self.seen[1]

    def step(self, holds: bool) -> None:
        """Once an upgrade waits: report `holds` to the relay, or leave (`UpgradeReady`) when this
        worker holds nothing. A report that cannot be written now (a Windows reader holding the
        file) is simply the next beat's."""
        if not self.waiting():
            return
        if self.supervision.mode == "exit":
            if not holds:
                raise UpgradeReady(f"another runtime is active; leaving {self.supervision.runtime}")
            return
        try:
            PRIVATE_FILES.write_text(self.supervision.state, WorkerState(holds=holds, at=time.time()).model_dump_json())
        except OSError:
            pass

    async def watch(self, holds: Callable[[], bool], every: float = 0.5, alive: Callable[[], bool] = lambda: True) -> None:
        """`step` every `every` seconds on this event loop (so nothing starts between the look and
        the leaving) while `alive()`."""
        while alive():
            self.step(holds())
            await asyncio.sleep(every)
