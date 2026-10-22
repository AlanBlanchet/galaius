"""The reviews a coordinator's answer to the person gets, asked by the runtime, never left to the coordinator's memory
(`Config.agent_turn_reviews`; 2026-10-21: reflection and the frustration analysis lived only as prompt text and were
skipped on nearly every turn).

Once a root run of a coordinator role has ended the turns that answered the person (his messages, or his words that
started it), its agents back too, each review whose signal his message carries (`TurnReview.signal`; none: every
answer) asks an instance of its role in the conversation about those turns (`galaius agents spawn`, which continues the
instance already there). The ask is answered at once on the coordinator's side, so the report wakes no one: the rows it
says this answer still owes are on the coordinator's card on the first turn it starts after (`relations.card`). The
rows naming a durable change (a prompt, a hook, code) go to the prompts' owner role in the same conversation. Each ask
and each forward is written down before it is made (`turn_review_marks`): a watcher started again never asks twice, and
never asks about turns that ended before it.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from galaius.agents import handback, relations, turn_review_marks
from galaius.agents import registry as reg
from galaius.agents.relations import final_words, report_section
from galaius.agents.turn_review_marks import Marks
from galaius.caller import RUN_VARIABLES
from galaius.config.settings import TurnReview, TurnReviews

log = logging.getLogger(__name__)

#: What the coordinator reads where the review's report would have come back (`Ask.answered`).
ANSWERED = ("{role} {run} was asked about your answer to the person by the runtime: its open rows reach your next turn "
            "(never a report waking you).")
#: How long one ask may take to be handed over (`galaius agents spawn` returns once its run is in line).
ASK_SECONDS = 120


class Ask(BaseModel):
    """One `galaius agents spawn` of `role` under coordinator run `parent` with `task`, made on the coordinator's behalf
    (the instance of its conversation continued when one is there), its answer never handed back to wake it."""

    model_config = ConfigDict(frozen=True)

    role: str
    parent: str
    cwd: str
    task: str

    def argv(self) -> list[str]:
        return reg.participant_argv("galaius", "agents", "spawn", "--agent", self.role, "--name", self.role,
                                    "--parent-run-id", self.parent, "--cwd", self.cwd, "--task", self.task)

    def make(self) -> str | None:
        """Ask it; the run that took it (the spawn's last line), None when the spawn refused (logged)."""
        environment = {**os.environ, **dict.fromkeys(RUN_VARIABLES, self.parent)}
        done = subprocess.run(self.argv(), cwd=self.cwd, capture_output=True, text=True, timeout=ASK_SECONDS,
                              env=reg.participant_environment(environment), check=False)
        said = (done.stdout or "").strip().splitlines()
        if done.returncode != 0 or not said or reg.get_run(said[-1]) is None:
            log.warning("could not ask %s for %s: %s", self.role, self.parent[:8], (done.stderr or done.stdout).strip()[-400:])
            return None
        self.answered(said[-1])
        return said[-1]

    def answered(self, run_id: str) -> None:
        """Its ask answered at once on the coordinator's side (`relations._reported`): the run's report is read where the
        coordinator stands on its next turn (`relations.card`), so ending it hands nothing back and wakes nobody."""
        reg.record_message_event(from_run=run_id, to_run=self.parent, text=ANSWERED.format(role=self.role, run=run_id[:8]),
                                 reports=(run_id,), fyi=True)


class Said(BaseModel):
    """One thing the person said to a coordinator run: a message, or the words that started it."""

    model_config = ConfigDict(frozen=True)

    at: float
    text: str


class TurnReviewWatch(BaseModel):
    """The coordinators' answers and the reviews' reports that ended between two looks (`tick`)."""

    started: float = Field(default_factory=time.time)

    def tick(self, reviews: TurnReviews) -> list[Ask]:
        """Ask each review owed by every coordinator's answer to the person that ended, and forward every review's
        durable rows; the asks made (each failure logged, never retried: its mark is written first)."""
        if not reviews.enabled:
            return []
        records = reg._stored_records()
        marks = turn_review_marks.read()
        before = marks.model_copy(deep=True)
        asks = [*self._answers(records, reviews, marks), *self._durable(records, reviews, marks)]
        if marks != before:
            turn_review_marks.update(lambda stored: stored.with_(marks))   # merged with a look made meanwhile
        for ask in asks:
            if (run_id := ask.make()) is not None and reviews.of(ask.role) is not None:
                turn_review_marks.update(lambda stored, run_id=run_id: stored.with_(Marks(asked={run_id: time.time()})))
        return asks

    def _answers(self, records: Sequence[reg.AgentRun], reviews: TurnReviews, marks: Marks) -> list[Ask]:
        """Per root coordinator run settled since its mark (agents back), one ask of each review whose signal what the
        person said in those turns carries; each mark moves to that end."""
        asks = []
        for run in records:
            if run.agent not in reviews.coordinators or run.parent_run_id is not None or run.finished_at is None:
                continue
            owed = [review for review in reviews.reviews if marks.reviewed.get(Marks.key(review.role, run.run_id), self.started) < run.finished_at]
            if not owed or not _settled(run, records):
                continue
            everything = _said(run, since=0.0)
            for review in owed:
                key = Marks.key(review.role, run.run_id)
                since = marks.reviewed.get(key, self.started)
                marks.reviewed[key] = run.finished_at
                earlier = [said.text for said in everything if said.at <= since]
                called = [(words, signal) for words in everything if words.at > since
                          and (signal := _calls(review, words.text, [*earlier, *(said.text for said in everything if since < said.at < words.at)])) is not None]
                if called:
                    words, signal = called[0]
                    asks.append(Ask(role=review.role, parent=run.run_id, cwd=run.cwd or ".", task=brief(run, words.at, review, signal, reviews)))
        return asks

    def _durable(self, records: Sequence[reg.AgentRun], reviews: TurnReviews, marks: Marks) -> list[Ask]:
        """One ask of the prompts' owner per review run the runtime asked whose turn ended since its mark with durable
        rows in its report; its mark moves to that end."""
        asks = []
        for run in records:
            coordinator = reg.get_run(run.parent_run_id) if reviews.of(run.agent) is not None and run.parent_run_id else None
            if (coordinator is None or run.run_id not in marks.asked or run.finished_at is None
                    or run.finished_at <= marks.forwarded.get(run.run_id, self.started) or handback._pending(run)):
                continue
            marks.forwarded[run.run_id] = run.finished_at
            if rows := report_section(final_words(run.run_id), "DURABLE CHANGE"):
                asks.append(Ask(role=reviews.librarian, parent=coordinator.run_id, cwd=coordinator.cwd or ".",
                                task=f"Durable changes {run.agent} {run.run_id[:8]} found in coordinator {coordinator.run_id[:8]}'s answer "
                                     f"to the person. Make each one, generic, never fitted to the one case:\n\n{rows}"))
        return asks


def _said(run: reg.AgentRun, *, since: float) -> list[Said]:
    """What the person said to `run`, and to each run whose work it continues (`relations.lineage`), after `since` and up
    to the end of its last turn, oldest first: his messages, and the words that started a run when they did (a root run
    no other run asked: its task, never a message)."""
    said = []
    for link in relations.lineage(run):
        said += [Said(at=event.at, text=event.text) for event in reg.read_messages(link.run_id)
                 if event.from_run == reg.PERSON_SENDER and event.to_run == link.run_id and since < event.at <= (run.finished_at or 0.0)]
        if _launched_by_person(link) and since < (link.started_at or 0.0) <= (run.finished_at or 0.0):
            said.append(Said(at=link.started_at or 0.0, text=link.task))
    return sorted(said, key=lambda words: words.at)


def _calls(review: TurnReview, text: str, earlier: Sequence[str]) -> str | None:
    """What in `text` calls for `review` ("" when it asks after every answer), `earlier` being what the person said before
    it in the conversation; None when nothing does."""
    return "" if review.signal is None else review.signal.matched(text, earlier)


def _settled(run: reg.AgentRun, records: Sequence[reg.AgentRun]) -> bool:
    """Whether `run` ended its turn with nothing waiting for it and every agent it waits on back (`handback`)."""
    return (run.status in handback._ENDED_ITS_TURN and not handback._pending(run)
            and not any(handback._pending(agent) for agent in handback._agents_answering(run.run_id, records)))


def _launched_by_person(run: reg.AgentRun) -> bool:
    """Whether the person's words started `run`: a root run no other run asked or handed its asks to."""
    return run.parent_run_id is None and not run.asked_by and not run.inherited_asks


def brief(run: reg.AgentRun, asked_at: float, review: TurnReview, signal: str, reviews: TurnReviews) -> str:
    """What `review` is asked about `run`'s turns since the person's message at `asked_at`: where they are in its
    transcript, its question, and the shape its report takes."""
    first, last = transcript_span(run.run_id, asked_at)
    return (f"Coordinator {run.name or run.agent} {run.run_id} ended its answer to the person (his message at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(asked_at))}). That answer is lines {first}-{last} of its "
            f"transcript {reg.events_path(run.run_id)} (one JSON event per line). {review.ask.format(signal=signal)} "
            "End your report with `DURABLE CHANGE` rows (each one change to a prompt, the profile, a hook or code that stops "
            "the class of mistake, never fitted to one case) or `NO CHANGE`, then `REDO` rows: what this answer still owes "
            f"the person. The {reviews.librarian} gets your DURABLE CHANGE rows; the coordinator reads your REDO rows on its next turn.")


def transcript_span(run_id: str, since: float) -> tuple[int, int]:
    """The 1-based lines of `run_id`'s transcript from its first event at or after `since` to its last."""
    first, count = 0, 0
    try:
        with reg.events_path(run_id).open(encoding="utf-8", errors="replace") as stream:
            for count, line in enumerate(stream, 1):
                if not first and (at := _at(line)) is not None and at >= since:
                    first = count
    except OSError:
        return 0, 0
    return first or count, count


def _at(line: str) -> float | None:
    """The `at` of one transcript line, read without parsing the whole event."""
    found = re.search(r'"at":\s*([0-9.]+)', line)
    return float(found.group(1)) if found else None
