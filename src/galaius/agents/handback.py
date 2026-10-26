"""What a run's agents bring back to it once they end.

A parent that ends its turn while its agents still work hears nothing when they finish: their ending reaches its
record, never its conversation, and nobody writes the owner the result (owner 2026-10-16: « it said in the UI
that tasks were done... But i never got another report saying "DONE" forthe tasks... »). `hand_back` resumes such a
parent (or the run that asked an agent its latest work) once its LAST pending agent ends, with every report that came
in since its turn ended; the run mirror calls it for each run `EndWatch` sees end (the machine service's loop,
`machines.MachineRunner`). `wait` is the same reports for a caller still in its turn, in one call."""

import json
import time
from collections.abc import Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from galaius.agents import agent_queue, messaging, relations
from galaius.agents.events import AgentEvent
from galaius.agents import registry as reg
from galaius.agents.relations import final_words, live, one_read, owed
from galaius.agents.spawn_line import SpawnLine
from galaius.config import Config
from galaius.paths import UserPaths
from galaius.private_files import PRIVATE_FILES

#: Each report's share of the hand-back message (`agent_queue.BRIEF_MAX` in all); the full one is `galaius agents events`.
REPORT_MAX = 12_000
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
    #: The run whose brief it took, refused by its plan's limit before it started (`reg.Relaunch.line`), and why its own
    #: relaunch could not start; "" for neither.
    relaunched: str = ""

    @classmethod
    def of(cls, run: reg.AgentRun) -> "Report":
        """`run`'s report; a run its plan's limit refused before it did anything is still to end until its brief is
        relaunched or its relaunch refused (`registry.relaunch_pending`), never read as a failure meanwhile."""
        relaunched = "\n".join(line for line in (run.relaunched_from.line(run.agent) if run.relaunched_from is not None else "",
                                                  f"its brief could not be relaunched on another model: {run.relaunch_refused}"
                                                  if run.relaunch_refused else "") if line)
        if _pending(run) or reg.relaunch_pending(run):
            said = reg.vendor_said_at(run) if run.process_running() else None  # a queued item alone: no turn to be silent
            return cls(run_id=run.run_id, agent=run.agent, status=run.status, pending=True, text="", relaunched=relaunched,
                       silent_for=None if said is None else max(0.0, time.time() - said))
        return cls(run_id=run.run_id, agent=run.agent, status=run.status, pending=False, text=final_words(run.run_id), relaunched=relaunched)

    @classmethod
    def answering(cls, run: reg.AgentRun, asker: str) -> "Report":
        """`run`'s report to `asker`'s latest ask, never to another run's: what it wrote from the turn that read that ask
        (its queue item, `ask_message`; its start when the ask is its task) up to the turn the next ask from another run
        started (`relations.ask_lines`). 2026-10-25: a reviewer kept for several makers handed one maker's verdict to an earlier
        maker whose ask it had answered days before. Its latest report (`of`) when no later ask came, or this computer
        kept no turn start for the ask."""
        report = cls.of(run)
        if report.pending:
            return report
        read = relations.ask_lines(run)
        message = relations.ask_message(run, asker)
        start = 0 if message is None else next((line for line, said in read if said == message), None)
        if start is None:
            return report
        end = next((line for line, said in read if line > start and said != message), None)
        if end is None:
            return report
        said = relations.report_between(run.run_id, start, end)
        return report if said is None else report.model_copy(update={"text": said})

    def section(self, limit: int | None = REPORT_MAX) -> str:
        text = self.text if limit is None or len(self.text) <= limit else f"{self.text[:limit]}\n… (clipped; `galaius agents events {self.run_id}`)"
        lead = f"{self.relaunched}\n" if self.relaunched else ""
        return f"## {self.agent or 'agent'} {self.run_id[:8]} — {self.status}\n{lead}{text or '(it ended without a final message)'}"


def _pending(run: reg.AgentRun) -> bool:
    """Still to end: its process is on a turn (or starting one), or a message waits for it."""
    return (run.status in ("starting", "running", "waiting") and run.process_running()) \
        or any(item.state in {"pending", "running"} for item in agent_queue.items(run.run_id))


def hand_back(run_id: str) -> list[messaging.Delivery]:
    """Resume each run `run_id` owes its report (`owed`; the run that took over from it, `live`) with the reports of the
    agents it waited on, when `run_id` ended the last of them. 2026-10-17: a continued instance's reports reached its
    parent, never the coordinator that had asked it the work."""
    child = reg.get_run(run_id)
    if child is None:
        return []
    deliveries = [delivery for asker in dict.fromkeys(live(asker) for asker in owed(child)) if (delivery := _resume(asker)) is not None]
    # Its own turn ended too: the reports its agents left while it worked, a hand-back refused then (it was at work), are
    # carried now. 2026-10-22: a reviewer's verdict for its maker ended mid-turn and never reached it.
    if (own := _resume(child.run_id)) is not None:
        deliveries.append(own)
    return deliveries


def _agents_answering(asker_id: str, records: Sequence[reg.AgentRun]) -> list[reg.AgentRun]:
    """The runs among `records` that owe run `asker_id`, or a run it took over from (`live`), their report (`owed`)."""
    aliases = {asker_id} | {stored.run_id for stored in records if stored.successor and live(stored.run_id) == asker_id}
    found = [stored.run_id for stored in records if aliases & stored.askers]
    return [run for run_id in found if (run := reg.get_run(run_id)) is not None and aliases.intersection(owed(run))]


def _resume(asker_id: str) -> messaging.Delivery | None:
    """Resume run `asker_id` with the reports of every agent that owes it one (`owed`) once none is pending: the reports
    alone, its turn opening on where it stands (`relations.card`) and on whose reports they are (`relations.heading`).

    Only a run whose own turn ended (`_ENDED_ITS_TURN`); every report not carried yet (`owed`), whenever its agent
    ended (`relations.handback_since` on): one that ended while it worked was never handed to it. One message per ended turn (its
    `message_id` names that turn), so every reader of the same ending queues it once. An agent still waiting on agents
    of its own holds it: its interim words (« dispatched the tester; waiting ») would answer the ask (`owed`)
    and cost `asker_id` a turn, while its final report comes once they end. So does one a cut stopped that resumes
    by itself (`registry.self_resuming`): its cut is no report, its report comes once it resumed and ended."""
    asker = reg.get_run(asker_id)
    if asker is None or asker.finished_at is None or asker.status not in _ENDED_ITS_TURN or _pending(asker):
        return None
    records = reg._stored_records()
    with one_read():
        agents = _agents_answering(asker_id, records)
        if any(_pending(run) or reg.self_resuming(run) or any(_pending(own) for own in _agents_answering(run.run_id, records)) for run in agents):
            return None
    since = relations.handback_since()
    ended = [run for run in agents if run.finished_at is not None and run.finished_at >= since]
    if not ended:
        return None
    reports = [Report.answering(run, asker_id) for run in sorted(ended, key=lambda run: run.finished_at or 0.0)]
    return messaging.deliver_message(
        asker.run_id, "\n\n".join(report.section() for report in reports), sender=reports[-1].run_id,
        message_id=f"handback-{asker.finished_at:.6f}", only_if_idle=True,
        turn=agent_queue.MessageTurn(reports=tuple(report.run_id for report in reports)),
    )


class EndWatch(BaseModel):
    """The runs that ended between two looks: left the live set, or recorded an end since the last look (a run that
    started and ended between looks, or one settled late with its observed end)."""

    live: frozenset[str] = Field(default_factory=lambda: frozenset(run.run_id for run in reg.running_runs()))
    looked: float = Field(default_factory=time.time)
    #: Ended runs a cut stopped that resume by themselves (`registry.self_resuming`): their launcher was not woken with the
    #: cut (`_resume`). Each ends again here once that no longer holds and nothing resumed it (cut again by the same cause
    #: right after its resume). Read from the store at start, every run a cut ended (`registry.recently_cut`): one that
    #: stopped coming back while this watcher was down ends on its first look (a report already carried is never carried twice).
    held: frozenset[str] = Field(default_factory=lambda: frozenset(run.run_id for run in reg.recently_cut() if run.askers))

    def model_post_init(self, _context) -> None:
        relations.handback_since()   # from its first look on, a report its asker was at work for is carried (`_resume`)

    def ended(self) -> list[str]:
        now, live = time.time(), frozenset(run.run_id for run in reg.running_runs())
        recorded = {run.run_id for run in reg._stored_records()
                    if run.askers and run.finished_at is not None and self.looked <= run.finished_at <= now}
        fresh = (self.live - live) | (recorded - live)
        cut = frozenset(run_id for run_id in (self.held | fresh) - live if (run := reg.get_run(run_id)) is not None and reg.self_resuming(run, now=now))
        ended = sorted(fresh | (self.held - live - cut))
        self.live, self.looked, self.held = live, now, cut
        return ended

    def tick(self) -> list[messaging.Delivery]:
        """`hand_back` for every run that ended since the last look; the deliveries it queued."""
        return [delivery for run_id in self.ended() for delivery in hand_back(run_id)]


class SilenceWatch(BaseModel):
    """Each run on a turn whose vendor wrote nothing for `Config.silent_seconds` (`silent_command_seconds` while its last
    word is a command it started, a build or a test suite), named once to the run that asked it — its launcher, else the
    first run it works for — while that run is at work: one message per asking run per look, every silent run it asked in
    it, from the watch (`reg.SILENCE_SENDER`), never from the silent run. An asking run not at work is never woken for it
    (its own turn reads « silent N min », `Waited.still_at_work`); it is told once it works again. A silence is the run and
    its last write: told once (`told`, kept beside the runs, so a restarted watch never tells it again); a new write starts
    a new one. A run nobody asked (the person's own) has no run to tell here."""

    @staticmethod
    def told_path() -> Path:
        return UserPaths.agents() / "silences-told.json"

    def tick(self, config: Config) -> list[messaging.Delivery]:
        now, told = time.time(), self._told()
        silent: dict[str, list[tuple[str, str]]] = {}
        standing: set[str] = set()
        for run in reg.running_runs():
            said, asker = reg.vendor_said_at(run), run.parent_run_id or next(iter(run.asked_by), None)
            if run.status != "running" or said is None or now - said < config.silent_seconds or asker in (None, run.run_id):
                continue
            silence = f"{run.run_id}-{said:.0f}"
            standing.add(silence)
            if silence in told or (asking := reg.get_run(asker)) is None or not asking.working():
                continue
            # Its own last word, never a message another run sent it meanwhile (one sent during a long command hid it).
            last = next((event for event in reversed(reg.events_from(run.run_id, max(0, reg.raw_line_count(run.run_id) - 50))) if event.kind != "message"), None)
            if last is not None and last.kind == "tool" and now - said < config.silent_command_seconds:
                continue  # it runs one command it started: a long build is not silence
            silent.setdefault(asker, []).append((silence, f"- {run.run_id} ({run.agent or 'no role'}): nothing written for {int((now - said) // 60)} min "
                                                         f"while at work; last event {last.kind if last else 'none'} at {datetime.fromtimestamp(said, UTC):%H:%M} UTC. "
                                                         f"Look: galaius agents events {run.run_id}"))
        deliveries = []
        for asker, found in silent.items():
            delivery = messaging.deliver_message(asker, "Silent at work, among the runs you asked:\n" + "\n".join(line for _, line in found),
                                                 sender=reg.SILENCE_SENDER, message_id=f"silent-{asker}-{now:.0f}", turn=agent_queue.MessageTurn(fyi=True))
            deliveries.append(delivery)
            if delivery.state != "error":
                told |= {silence for silence, _ in found}
        PRIVATE_FILES.write_text(self.told_path(), json.dumps(sorted(told & standing)))  # an ended or spoken-again silence is forgotten
        return deliveries

    def _told(self) -> set[str]:
        try:
            return set(json.loads(PRIVATE_FILES.read_text(self.told_path())))
        except (OSError, ValueError):
            return set()


def collected(asker: str | None, reports: Sequence[Report]) -> None:
    """Record each ended report in `reports` as carried to run `asker`, the run that read it inline (`agents wait`, a send
    waiting on its reply), as a hand-back records the reports it carries: the ask it answered is answered (`owed`), its
    next turn hands back nothing to `asker` (2026-10-17: a reviewer whose verdict was read through `agents wait` woke
    that reader again when it ended its next round). Once per ended turn, however often it is read."""
    if asker is None or reg.get_run(asker) is None:
        return
    for report in reports:
        if report.pending or (run := reg.get_run(report.run_id)) is None or run.finished_at is None:
            continue
        message_id = f"collected-{run.run_id}-{run.finished_at:.6f}"
        if reg.message_for(asker, message_id) is None:
            reg.record_message_event(from_run=run.run_id, to_run=asker, text=report.section(), event_id=message_id,
                                     reports=(run.run_id,), fyi=True)


def read_inline(run_id: str, shown: Sequence[AgentEvent]) -> str:
    """What the run asking from this process (`messaging.asking_run`) reads after `shown`, run `run_id`'s events (each cut
    short), when they hold its ending and it owes that run its report: the whole report, as a hand-back carries it, then
    counted as read (`collected`), never handed back to it again. "" otherwise."""
    run = reg.get_run(run_id)
    asker = messaging.asking_run()
    if (run is None or asker is None or run.finished_at is None or _pending(run)
            or not any(event.kind in ("done", "error") for event in shown) or live(asker) not in {live(other) for other in owed(run)}):
        return ""
    report = Report.of(run)
    collected(asker, [report])
    return report.section(limit=None)


class Waited(BaseModel):
    """What `wait` saw: each run's report, and the message that cut the wait short (None: all ended, or the timeout)."""

    reports: list[Report]
    message: agent_queue.QueueItem | None = None

    def still_at_work(self) -> list[str]:
        """Each pending run's id, with how long it has written nothing once that passes `Config.silent_seconds`."""
        silent = Config().silent_seconds
        return [report.run_id + (f" (silent {report.silent_for / 60:.0f} min)" if (report.silent_for or 0) >= silent else "")
                for report in self.reports if report.pending]


def wait(run_ids: Sequence[str], *, timeout: float, interval: float = 2.0, asker: str | None = None) -> Waited:
    """Each run's report once none of them is pending, or at `timeout` with the pending ones marked; for a run whose brief
    was relaunched on another model (`registry.relaunch_owed`), the run that took it (`live`). A short id is resolved;
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
                if (run := reg.get_run(reg.resolve_run_id(asked) or asked)) is not None and run.relaunched_at is not None \
                        and (taken := live(run.run_id)) != run.run_id \
                        and ((started := reg.get_run(taken)) is not None or line.describe(taken) is not None):
                    # Its brief relaunched on another model: that run's report, or it waits in line.
                    reports.append(Report.of(started) if started is not None else
                                   Report(run_id=taken, agent=run.agent, status="queued", pending=True, text=""))
                elif run is not None:
                    reports.append(Report.of(run))
                elif line.describe(asked) is not None:
                    reports.append(Report(run_id=asked, agent=None, status="queued", pending=True, text=""))
                else:
                    raise LookupError(f"no agent run {asked!r}")
            if not any(report.pending for report in reports) or time.monotonic() >= deadline:
                collected(asker, reports)
                return Waited(reports=reports)
            if waiting is not None and (message := agent_queue.next_step_read(waiting)) is not None:
                collected(asker, reports)
                return Waited(reports=reports, message=message)
            if not stepped_aside:
                aside.enter_context(line.aside(asker, waits_on=[report.run_id for report in reports if report.pending]))
                stepped_aside = True
            time.sleep(interval)
