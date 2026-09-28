"""The worker's side of an upgrade: noticing that the pointer moved, and saying when it can go.

A relayed worker (MCP) REPORTS whether it holds something a swap would lose; its supervisor picks
the moment. Any other worker LEAVES by itself: it raises `UpgradeReady` at a point where it holds
nothing (no command running, no key pressed lately) and the CLI exits `EXIT_UPGRADE`."""

import time

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
        """None when this process is not a supervised worker."""
        supervision = Supervision.current()
        return None if supervision is None else cls(supervision=supervision, store=RuntimeStore.default())

    def waiting(self) -> bool:
        """Another runtime than this worker's is active (read again only when the pointer moved)."""
        signature = self.store.signature()
        if self.seen is None or self.seen[0] != signature:
            self.seen = (signature, self.store.active().path != self.supervision.runtime)
        return self.seen[1]

    def report(self, holds: bool) -> None:
        """Relay mode: tell the supervisor, once an upgrade waits, whether a swap would lose state."""
        if self.supervision.state is not None and self.waiting():
            PRIVATE_FILES.write_text(self.supervision.state, WorkerState(holds=holds, at=time.time()).model_dump_json())

    def leave_if_quiet(self, quiet: bool) -> None:
        """Exit mode: raise `UpgradeReady` when an upgrade waits and this worker holds nothing."""
        if self.supervision.mode == "exit" and quiet and self.waiting():
            raise UpgradeReady(f"another runtime is active; leaving {self.supervision.runtime}")
