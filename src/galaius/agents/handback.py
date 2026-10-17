"""What a run's agents bring back to it once they end.

A parent that ends its turn while its agents still work hears nothing when they finish: their ending reaches its
record, never its conversation, and nobody writes the owner the result (owner 2026-10-16: « it said in the UI
that tasks were done... But i never got another report saying "DONE" forthe tasks... »). `hand_back` resumes such a
parent (or the run that asked an agent its latest work) once its LAST pending agent ends, with every report that came
in since its turn ended; the run mirror calls it for each run `EndWatch` sees end (the machine service's loop,
`machines.MachineRunner`). `wait` is the same reports for a caller still in its turn, in one call."""

import time
from collections.abc import Sequence
from contextlib import ExitStack

from pydantic import BaseModel, Field

from galaius.agents import agent_queue, messaging
from galaius.agents import registry as reg
from galaius.agents.events import AgentEvent, ending_of
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


def owed(run: reg.AgentRun) -> tuple[str, ...]:
    """The runs its ended turn reports to: each run it answers (`askers`) whose latest ask no hand-back has carried its
    report on yet (`_reported`). Its parent asked its task when it started, and asks again with each message it or the
    person sends; a run of its `asked_by` asks with each message it sends (or a spawn sent for it, `for_run`). An
    automatic resume, the person's « Continuer », any hand-back (`reports`) or a one-way note (`fyi`) is no ask. An ask
    with no time stays owed. 2026-10-17: a report sent to a third run made it an asker for good, each later turn of the
    reporter woke it with an empty report (one coordinator: 9 such runs; a librarian: 4 empty turns; four runs looping
    every 3 min); a reused instance's every turn also woke the run that had spawned it for an earlier job."""
    asked: dict[str, float | None] = {run.parent_run_id: run.started_at or None} if run.parent_run_id else {}
    for event in reg.read_messages(run.run_id):
        if event.to_run != run.run_id or event.fyi or event.reports:
            continue
        said_by = event.for_run or event.from_run
        if said_by == reg.PERSON_SENDER and event.text != reg.RESUME_BRIEF and run.parent_run_id:
            said_by = run.parent_run_id  # the person's words to it: its parent hears the outcome
        if said_by in run.askers:
            asked[said_by] = event.at
    return tuple(asker for asker, at in asked.items() if at is None or not _reported(run.run_id, to=asker, since=at))


def _reported(run_id: str, *, to: str, since: float) -> bool:
    """A hand-back to run `to` carried `run_id`'s report (`_resume`) at or after `since`."""
    return any(event.to_run == to and run_id in event.reports and (event.at or 0.0) >= since for event in reg.read_messages(to))


def hand_back(run_id: str) -> list[messaging.Delivery]:
    """Resume each run `run_id` owes its report (`owed`) with the reports of the agents it waited on, when `run_id` ended
    the last of them. 2026-10-17: a continued instance's reports reached its parent, never the coordinator that had
    asked it the work."""
    child = reg.get_run(run_id)
    if child is None:
        return []
    return [delivery for asker in owed(child) if (delivery := _resume(asker, child)) is not None]


def _agents_answering(asker_id: str, records: Sequence[reg.AgentRun]) -> list[reg.AgentRun]:
    """The runs among `records` that owe run `asker_id` their report (`owed`)."""
    return [run for stored in records if asker_id in stored.askers
            and (run := reg.get_run(stored.run_id)) is not None and asker_id in owed(run)]


def _resume(asker_id: str, child: reg.AgentRun) -> messaging.Delivery | None:
    """Resume run `asker_id` with the reports of every agent that owes it one (`owed`) once none is pending.

    Only a run whose own turn ended (`_ENDED_ITS_TURN`) before those agents did; one message per ended turn (its
    `message_id` names that turn), so every reader of the same ending queues it once. An agent still waiting on agents
    of its own holds it: its interim words (« dispatched the tester; waiting ») would answer the ask (`owed`)
    and cost `asker_id` a turn, while its final report comes once they end."""
    asker = reg.get_run(asker_id)
    if asker is None or asker.finished_at is None or asker.status not in _ENDED_ITS_TURN or _pending(asker):
        return None
    records = reg._stored_records()
    agents = _agents_answering(asker_id, records)
    if any(_pending(run) or any(_pending(own) for own in _agents_answering(run.run_id, records)) for run in agents):
        return None
    since = [run for run in agents if run.finished_at is not None and run.finished_at >= asker.finished_at]
    if not since:
        return None
    reports = [Report.of(run) for run in sorted(since, key=lambda run: run.finished_at or 0.0)]
    message = (f"The agents you started or asked ended after your turn did; their final reports follow. Report the outcome "
               f"to whoever asked you (the result of each ask, from these reports), or continue the work.\n\n"
               + "\n\n".join(report.section() for report in reports))
    return messaging.deliver_message(
        asker.run_id, message, sender=child.run_id, message_id=f"handback-{asker.finished_at:.6f}", only_if_idle=True,
        turn=agent_queue.MessageTurn(limit=agent_queue.BRIEF_MAX, reports=tuple(report.run_id for report in reports)),
    )


class EndWatch(BaseModel):
    """The runs that ended between two looks: left the live set, or recorded an end since the last look (a run that
    started and ended between looks, or one settled late with its observed end)."""

    live: frozenset[str] = Field(default_factory=lambda: frozenset(run.run_id for run in reg.running_runs()))
    looked: float = Field(default_factory=time.time)

    def ended(self) -> list[str]:
        now, live = time.time(), frozenset(run.run_id for run in reg.running_runs())
        recorded = {run.run_id for run in reg._stored_records()
                    if run.askers and run.finished_at is not None and self.looked <= run.finished_at <= now}
        ended = sorted((self.live - live) | (recorded - live))
        self.live, self.looked = live, now
        return ended

    def tick(self) -> list[messaging.Delivery]:
        """`hand_back` for every run that ended since the last look; the deliveries it queued."""
        return [delivery for run_id in self.ended() for delivery in hand_back(run_id)]


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
                aside.enter_context(line.aside(asker, waits_on=[report.run_id for report in reports if report.pending]))
                stepped_aside = True
            time.sleep(interval)
