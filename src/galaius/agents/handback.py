"""What a run's agents bring back to it once they end.

A parent that ends its turn while its agents still work hears nothing when they finish: their ending reaches its
record, never its conversation, and nobody writes the owner the result (owner 2026-10-16: « it said in the UI
that tasks were done... But i never got another report saying "DONE" forthe tasks... »). `hand_back` resumes such a
parent once its LAST pending agent ends, with every report that came in since its turn ended; the run mirror calls it
for each run `EndWatch` sees end (the machine service's loop, `machines.MachineRunner`). `wait` is the same reports for a caller still in its turn, in one call."""

import time
from collections.abc import Sequence
from contextlib import ExitStack

from pydantic import BaseModel, Field

from galaius.agents import agent_queue, messaging
from galaius.agents import registry as reg
from galaius.agents.events import ending_of
from galaius.agents.spawn_line import SpawnLine

#: Each report's share of the hand-back message (`agent_queue.BRIEF_MAX` in all); the full one is `galaius agents events`.
REPORT_MAX = 12_000
#: A run at work that wrote nothing for this long is named silent to the run waiting on it (`Waited.still_at_work`).
SILENT_SECONDS = 600.0
#: The statuses of a parent whose turn ended on its own (a conversation's done turn reads "waiting"); a person's stop or a
#: failure is never resumed behind them.
_ENDED_ITS_TURN = frozenset({"done", "waiting"})


class Report(BaseModel):
    """One run's final words, or that it is still at work."""

    run_id: str
    agent: str | None
    status: str
    pending: bool
    text: str
    #: Seconds since its vendor last wrote, for a run still at work; None for an ended one or one with nothing written.
    silent_for: float | None = None

    @classmethod
    def of(cls, run: reg.AgentRun) -> "Report":
        if _pending(run):
            said = reg.vendor_said_at(run) if run.process_running() else None  # a queued item alone: no turn to be silent
            return cls(run_id=run.run_id, agent=run.agent, status=run.status, pending=True, text="",
                       silent_for=None if said is None else max(0.0, time.time() - said))
        ending = ending_of(events := reg.read_events(run.run_id), run.run_id)
        said = ending.text if ending is not None else next((event.text for event in reversed(events) if event.kind == "text"), "")
        return cls(run_id=run.run_id, agent=run.agent, status=run.status, pending=False, text=(said or "").strip())

    def section(self, limit: int | None = REPORT_MAX) -> str:
        text = self.text if limit is None or len(self.text) <= limit else f"{self.text[:limit]}\n… (clipped; `galaius agents events {self.run_id}`)"
        return f"## {self.agent or 'agent'} {self.run_id[:8]} — {self.status}\n{text or '(it ended without a final message)'}"


def _pending(run: reg.AgentRun) -> bool:
    """Still to end: its process is on a turn (or starting one), or a message waits for it."""
    return (run.status in ("starting", "running", "waiting") and run.process_running()) \
        or any(item.state in {"pending", "running"} for item in agent_queue.items(run.run_id))


def hand_back(run_id: str) -> messaging.Delivery | None:
    """Resume `run_id`'s parent with its agents' reports when `run_id` ended the last of the ones it was waiting on.

    Only a parent whose own turn ended (`_ENDED_ITS_TURN`) before those agents did; one message per ended parent turn
    (its `message_id` names that turn), so every reader of the same ending queues it once."""
    child = reg.get_run(run_id)
    parent = reg.get_run(child.parent_run_id) if child is not None and child.parent_run_id else None
    if parent is None or parent.finished_at is None or parent.status not in _ENDED_ITS_TURN or _pending(parent):
        return None
    agents = [run for stored in reg._stored_records() if stored.parent_run_id == parent.run_id
              and (run := reg.get_run(stored.run_id)) is not None]
    if any(_pending(run) for run in agents):
        return None
    since = [run for run in agents if run.finished_at is not None and run.finished_at >= parent.finished_at]
    if not since:
        return None
    reports = [Report.of(run) for run in sorted(since, key=lambda run: run.finished_at or 0.0)]
    message = (f"Your agents ended after your turn did; their final reports follow. Report the outcome to whoever asked "
               f"you (the result of each ask, from these reports), or continue the work.\n\n"
               + "\n\n".join(report.section() for report in reports))
    return messaging.deliver_message(
        parent.run_id, message, sender=child.run_id, message_id=f"handback-{parent.finished_at:.6f}", only_if_idle=True,
        turn=agent_queue.MessageTurn(limit=agent_queue.BRIEF_MAX),
    )


class EndWatch(BaseModel):
    """The runs that ended between two looks: left the live set, or recorded an end since the last look (a run that
    started and ended between looks, or one settled late with its observed end)."""

    live: frozenset[str] = Field(default_factory=lambda: frozenset(run.run_id for run in reg.running_runs()))
    looked: float = Field(default_factory=time.time)

    def ended(self) -> list[str]:
        now, live = time.time(), frozenset(run.run_id for run in reg.running_runs())
        recorded = {run.run_id for run in reg._stored_records()
                    if run.parent_run_id and run.finished_at is not None and self.looked <= run.finished_at <= now}
        ended = sorted((self.live - live) | (recorded - live))
        self.live, self.looked = live, now
        return ended

    def tick(self) -> list[messaging.Delivery]:
        """`hand_back` for every run that ended since the last look; the deliveries it queued."""
        return [delivery for run_id in self.ended() if (delivery := hand_back(run_id)) is not None]


class Waited(BaseModel):
    """What `wait` saw: each run's report, and the message that cut the wait short (None: all ended, or the timeout)."""

    reports: list[Report]
    message: agent_queue.QueueItem | None = None

    def still_at_work(self) -> list[str]:
        """Each pending run's id, with how long it has written nothing once that passes `SILENT_SECONDS`."""
        return [report.run_id + (f" (silent {report.silent_for / 60:.0f} min)" if (report.silent_for or 0) >= SILENT_SECONDS else "")
                for report in self.reports if report.pending]


def wait(run_ids: Sequence[str], *, timeout: float, interval: float = 2.0, asker: str | None = None) -> Waited:
    """Each run's report once none of them is pending, or at `timeout` with the pending ones marked. A short id is resolved;
    a run still waiting in this computer's line (`SpawnLine`) is pending; an id naming no run is refused (`LookupError`).
    A message `asker` (the run waiting, `GALAIUS_RUN_ID`) reads at its next step (`agent_queue.next_step_read`) ends the
    wait at once, never after a 10-minute wait (2026-10-16: the owner's words reached the coordinator 9.4 min late); one
    it reads only once its turn ends leaves the wait running. While one is pending, `asker`'s place on this computer is
    free for the line; it counts again as soon as the wait returns (`SpawnLine.aside`)."""
    line = SpawnLine.here()
    waiting = reg.get_run(asker) if asker is not None else None
    deadline = time.monotonic() + timeout
    stepped_aside = False
    with ExitStack() as aside:
        while True:
            reports: list[Report] = []
            for asked in run_ids:
                if (run := reg.get_run(reg.resolve_run_id(asked) or asked)) is not None:
                    reports.append(Report.of(run))
                elif line.describe(asked) is not None:
                    reports.append(Report(run_id=asked, agent=None, status="queued", pending=True, text=""))
                else:
                    raise LookupError(f"no agent run {asked!r}")
            if not any(report.pending for report in reports) or time.monotonic() >= deadline:
                return Waited(reports=reports)
            if waiting is not None and (message := agent_queue.next_step_read(waiting)) is not None:
                return Waited(reports=reports, message=message)
            if not stepped_aside:
                aside.enter_context(line.aside(asker))
                stepped_aside = True
            time.sleep(interval)
