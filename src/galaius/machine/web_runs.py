"""The runs this computer started for the web (`WebRuns`): the only ones, with what they launched, a web request may read,
message or stop."""

import json
import logging
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar
from uuid import UUID

from galaius_core import AgentRefused, AgentRunKind, AgentStartRequest, StopActor, WebFull, WebLimited, WebLoad, listed_role
from pydantic import BaseModel, ConfigDict

from galaius.agents import registry as reg
from galaius.host.atomic_file import replace_held_open
from galaius.host.file_lock import exclusive

logger = logging.getLogger(__name__)

#: A run's statuses while it works (`waiting` for its owner is not working).
WORKING = frozenset({"starting", "running"})
#: A run he follows now: at work, or waiting for his answer.
HELD = WORKING | {"waiting"}


def last_active(tree: Sequence[reg.AgentRun]) -> float:
    """When a conversation's tree last did something: now (`inf`) while one of its runs works or waits for him."""
    return max((math.inf if run.status in HELD else run.finished_at or run.started_at for run in tree), default=0.0)


def listed(forest: Mapping[str, Sequence[reg.AgentRun]], most: int) -> list[reg.AgentRun]:
    """The runs of `forest` (`reg.forest`) a list answer carries, at most `most`, newest first. First the conversations
    (`forest`'s roots), the last active first (one at work or waiting for him before any), then every other run at work
    or waiting, then the newest of the rest: a conversation he uses stays listed whatever it or another launched and
    however many agents work at once, since the web may learn a conversation's program from this list alone (2026-11-02: one
    month old, 1,500 agents since, the web lost its program and refused his subscription switch)."""
    runs = reg.newest_first(forest)
    present = {run.run_id for run in runs}
    active = {root: last_active(tree) for root, tree in forest.items()}
    ranked = (*(root for root in sorted(forest, key=active.__getitem__, reverse=True) if root in present),
              *(run.run_id for run in runs if run.status in HELD), *(run.run_id for run in runs))
    kept = set(list(dict.fromkeys(ranked))[:most])
    return [run for run in runs if run.run_id in kept]


class WebRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    root: str
    path: str = ""
    kind: AgentRunKind = "agent"
    #: A continued copy: the editor conversation it copies (its history opens the copy's transcript).
    source: UUID | None = None
    #: Started inside the machine's OS fence (`galaius.isolation.fence`).
    fenced: bool = False
    #: The start request that launched it (`AgentStartRequest.idempotent_feature`): asked again, it answers this run.
    request_id: UUID | None = None


class WebRuns(BaseModel):
    """The runs this computer started for the web, kept in one 0600 file beside the machine config:
    the only runs (with what they launched) a web request may read, message or stop."""

    model_config = ConfigDict(frozen=True)
    path: Path
    KEEP: ClassVar[int] = 500

    @classmethod
    def in_folder(cls, folder: Path) -> "WebRuns":
        """The file the runner whose machine file is in `folder` keeps them in."""
        return cls(path=folder / "machine-agent-runs.json")

    def read(self) -> tuple[WebRun, ...]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        return tuple(WebRun.model_validate(item) for item in raw)

    def started_by(self, request_id: UUID) -> WebRun | None:
        """The run start request `request_id` launched here, if any."""
        return next((run for run in self.read() if run.request_id == request_id), None)

    def add(self, run: WebRun) -> None:
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        with exclusive(descriptor):
            kept = (*self.read(), run)[-self.KEEP:]
            temporary = self.path.with_suffix(".tmp")
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump([item.model_dump(mode="json") for item in kept], stream)
            replace_held_open(temporary, self.path)

    def forest(self) -> dict[str, list[reg.AgentRun]]:
        """Each of these runs with every run it launched (`reg.forest`), as the launcher's registry has them now."""
        return reg.forest(frozenset(str(item.run_id) for item in self.read()))

    def runs(self) -> list[reg.AgentRun]:
        """These runs and every run they launched, newest first."""
        return reg.newest_first(self.forest())

    def live(self) -> int:
        """The conversations started from the web that work now (one of their runs starting or running): one counts
        ONCE, whatever agents it launched (owner 2026-10-10: two conversations and their five sub-agents refused his
        third); one waiting for its owner, with nothing of it working, does not count; nor a run of a tool of the web's own
        UI (`UI_ROLES`: a question to the blob), which is none of his conversations."""
        return sum(1 for root, runs in reg.forest(frozenset(str(item.run_id) for item in self.read())).items()
                   if any(run.status in WORKING for run in runs) and listed_role(next((run.agent for run in runs if run.run_id == root), None)))

    def load(self, request: WebLimited) -> WebLoad:
        """The conversations started from the web working here against how many may at once, as `request` carries it
        from the web (`WebLimited.web_conversations`, each with whatever agents it launched; the web reads it)."""
        return WebLoad(working=self.live(), limit=request.web_conversations)

    def refuse_full(self, request: WebLimited) -> None:
        """Refuses one more start while as many conversations started from the web as `request` allows work here; never a
        run of a tool of the web's own UI (a question to the blob: no conversation of his, `UI_ROLES`). Nothing here caps how
        many of those start at once but the web's start rate limit: the blob role has no tools and runs read_only."""
        if listed_role(request.role if isinstance(request, AgentStartRequest) else None) and (load := self.load(request)).full:
            raise AgentRefused(WebFull(web=load), f"{load.working} conversations started from the web are already working on this computer (at most {load.limit}); stop one first")

    def working(self) -> bool:
        """One of these runs, or a run it launched, is starting or running now (waiting for its owner is not working)."""
        return any(run.status in WORKING for run in self.runs())

    def stop_live(self) -> tuple[int, int]:
        """Stops every one still working (agents switched off here): (how many stopped, how many
        could not be - each failure logged, never stopping the rest)."""
        stopped = failed = 0
        try:
            runs = self.runs()
        except (OSError, ValueError) as error:
            logger.warning("web runs unreadable; none stopped: %s", error)
            return 0, 1
        for run in runs:
            if run.status not in {"running", "waiting"}:
                continue
            try:
                stopped += bool(reg.stop(run.run_id, by=StopActor(kind="pc_switch")))
            except (OSError, RuntimeError, ValueError) as error:
                failed += 1
                logger.warning("could not stop run %s: %s", run.run_id[:8], error)
        return stopped, failed
