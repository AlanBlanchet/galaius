"""The reflection a coordinator's answer to the person gets, asked by the runtime, never left to the coordinator's memory
(`Config.agent_reflection`; 2026-10-21: the step lived only as prompt text and was skipped on nearly every turn).

Once a root run of a coordinator role has ended the turns that answered the person, its agents back too, an instance of
the reflection role in its conversation is asked about those turns (`galaius agents spawn`, which continues the instance
already there). Its report reaches the coordinator like any agent's (`handback`): the rows it owes this turn are open
there on its next wake. The rows naming a durable change (a prompt, a hook, code) go to the prompts' owner role in the
same conversation, the reflection role reading only. Each ask and each forward is written down before it is made
(`#reflection-cadence.state`): a watcher started again never asks twice, and never asks about turns that ended before it.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from galaius.agents import handback
from galaius.agents import registry as reg
from galaius.agents.relations import final_words
from galaius.config.settings import ReflectionCadence

log = logging.getLogger(__name__)

#: Where a reflection's report names its durable changes, and where that list ends.
DURABLE = re.compile(r"^\W*DURABLE CHANGES?\b.*$", re.M | re.I)
ENDS = re.compile(r"^\W*(?:REDO|NO CHANGE|NOT DURABLE)\b", re.M | re.I)
#: How long one ask may take to be handed over (`galaius agents spawn` returns once its run is in line).
ASK_SECONDS = 120


class Marks(BaseModel):
    """What the watcher already did: per coordinator run, the time up to which its turns were reflected on; per
    reflection run, the end of the turn whose durable rows were forwarded."""

    model_config = ConfigDict(extra="ignore")

    reflected: dict[str, float] = Field(default_factory=dict)
    forwarded: dict[str, float] = Field(default_factory=dict)

    @classmethod
    def read(cls, raw: bytes | None) -> Marks:
        """The marks as written; an unreadable file reads as none (the watcher's start bounds what it asks about)."""
        try:
            return cls.model_validate_json(raw) if raw else cls()
        except ValidationError:
            return cls()


class Ask(BaseModel):
    """One `galaius agents spawn` of `role` under coordinator run `parent` with `task`."""

    model_config = ConfigDict(frozen=True)

    role: str
    parent: str
    cwd: str
    task: str

    def argv(self) -> list[str]:
        return reg.participant_argv("galaius", "agents", "spawn", "--agent", self.role, "--name", self.role,
                                    "--parent-run-id", self.parent, "--cwd", self.cwd, "--task", self.task)


class ReflectionWatch(BaseModel):
    """The coordinators' answers and the reflections' reports that ended between two looks (`tick`)."""

    started: float = Field(default_factory=time.time)

    def tick(self, cadence: ReflectionCadence) -> list[Ask]:
        """Ask a reflection about every coordinator's answer to the person that ended, and forward every reflection's
        durable rows; the asks made (each failure logged, never retried: its mark is written first)."""
        if not cadence.enabled:
            return []
        records = reg._stored_records()
        with reg.shared_state("reflection-cadence") as (raw, write, _keep):
            marks = Marks.read(raw)
            asks = [*self._answers(records, cadence, marks), *self._durable(records, cadence, marks)]
            if asks:
                write(marks.model_dump_json().encode())
        for ask in asks:
            done = subprocess.run(ask.argv(), cwd=ask.cwd if ask.cwd else ".", capture_output=True, text=True, timeout=ASK_SECONDS,
                                  env=reg.participant_environment(os.environ), check=False)
            if done.returncode != 0:
                log.warning("could not ask %s for %s: %s", ask.role, ask.parent[:8], (done.stderr or done.stdout).strip()[-400:])
        return asks

    def _answers(self, records: Sequence[reg.AgentRun], cadence: ReflectionCadence, marks: Marks) -> list[Ask]:
        """One reflection ask per root coordinator run whose turns answering the person ended since its mark, all its
        agents back; its mark moves to that end."""
        asks = []
        for run in records:
            since = marks.reflected.get(run.run_id, self.started)
            if (run.agent not in cadence.coordinators or run.parent_run_id is not None or run.finished_at is None
                    or run.finished_at <= since or not _settled(run, records)):
                continue
            asked = [event.at for event in reg.read_messages(run.run_id)
                     if event.from_run == reg.PERSON_SENDER and event.to_run == run.run_id and since < event.at <= run.finished_at]
            marks.reflected[run.run_id] = run.finished_at
            if asked:
                asks.append(Ask(role=cadence.role, parent=run.run_id, cwd=run.cwd or ".", task=reflection_brief(run, min(asked), cadence)))
        return asks

    def _durable(self, records: Sequence[reg.AgentRun], cadence: ReflectionCadence, marks: Marks) -> list[Ask]:
        """One ask of the prompts' owner per reflection run under a coordinator whose turn ended since its mark with
        durable rows in its report; its mark moves to that end."""
        asks = []
        for run in records:
            coordinator = reg.get_run(run.parent_run_id) if run.agent == cadence.role and run.parent_run_id else None
            if (coordinator is None or coordinator.agent not in cadence.coordinators or run.finished_at is None
                    or run.finished_at <= marks.forwarded.get(run.run_id, self.started) or handback._pending(run)):
                continue
            marks.forwarded[run.run_id] = run.finished_at
            if rows := durable_rows(final_words(run.run_id)):
                asks.append(Ask(role=cadence.librarian, parent=coordinator.run_id, cwd=coordinator.cwd or ".",
                                task=f"Durable changes reflection {run.run_id[:8]} found in coordinator {coordinator.run_id[:8]}'s answer "
                                     f"to the person. Make each one, generic, never fitted to the one case:\n\n{rows}"))
        return asks


def _settled(run: reg.AgentRun, records: Sequence[reg.AgentRun]) -> bool:
    """Whether `run` ended its turn with nothing waiting for it and every agent it waits on back (`handback`)."""
    return (run.status in handback._ENDED_ITS_TURN and not handback._pending(run)
            and not any(handback._pending(agent) for agent in handback._agents_answering(run.run_id, records)))


def durable_rows(report: str) -> str:
    """The rows a reflection report lists under its durable changes, up to its next list ("" when it names none)."""
    start = DURABLE.search(report)
    if start is None:
        return ""
    rest = report[start.end():]
    end = ENDS.search(rest)
    return (rest[:end.start()] if end else rest).strip()


def reflection_brief(run: reg.AgentRun, asked_at: float, cadence: ReflectionCadence) -> str:
    """What the reflection is asked about `run`'s turns since the person's message at `asked_at`: where they are in its
    transcript, and the shape its report takes."""
    first, last = transcript_span(run.run_id, asked_at)
    return (f"Coordinator {run.name or run.agent} {run.run_id} ended its answer to the person (his message at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(asked_at))}). That answer is lines {first}-{last} of its "
            f"transcript {reg.events_path(run.run_id)} (one JSON event per line). Reflect on it as your role says: done right, "
            "done wrong, could upgrade. End your report with `DURABLE CHANGE` rows (each one change to a prompt, a hook or "
            "code that stops the class of mistake, never fitted to one case) or `NO CHANGE`, then `REDO` rows: what this "
            f"answer still owes the person. The {cadence.librarian} gets your DURABLE CHANGE rows; the coordinator gets your report.")


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
