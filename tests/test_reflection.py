"""A coordinator's answer to the person gets its reflection from the runtime, once, when the answer ended with its agents
back; the reflection's durable rows go to the prompts' owner (`galaius.agents.reflection`; 2026-10-21: the step lived only
as prompt text and was skipped on nearly every turn)."""

import json
import time
import uuid

import pytest

from galaius.agents import reflection
from galaius.agents import registry as reg
from galaius.config.settings import ReflectionCadence
from tests.support import register_run

MAIN = str(uuid.UUID(int=21 << 96))
REFLECTION = str(uuid.UUID(int=22 << 96))


def _ended(run_id: str, *, report: str = "", **rest) -> None:
    register_run(run_id, **rest)
    reg.raw_events_path(run_id).write_text("".join(json.dumps(line) + "\n" for line in (
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "stop_reason": "end_turn", "result": report})))
    reg.finish(run_id, exit_code=0, first=0)
    reg._merge_record_locked(run_id, {"status": "done", "finished_at": time.time()})


@pytest.fixture
def asked(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(reflection.subprocess, "run", lambda argv, **_: calls.append(argv) or reflection.subprocess.CompletedProcess(argv, 0, "", ""))
    return calls


def _spawned(argv: list[str]) -> dict[str, str]:
    tail = argv[argv.index("spawn") + 1:]
    return dict(zip(tail[::2], tail[1::2]))


@pytest.mark.parametrize("sender", [reg.PERSON_SENDER, str(uuid.UUID(int=23 << 96))])
def test_a_coordinators_answer_to_the_person_is_reflected_on_once(asked, sender):
    watch = reflection.ReflectionWatch(started=time.time() - 1)
    register_run(MAIN, agent="main", name="main")
    reg.record_message_event(from_run=sender, to_run=MAIN, text="ajoute le mode sombre")
    _ended(MAIN, agent="main", name="main")
    watch.tick(ReflectionCadence())
    watch.tick(ReflectionCadence())   # the next look: already asked
    if sender != reg.PERSON_SENDER:   # an agent's message is no answer to the person
        assert asked == []
        return
    [argv] = asked
    spawn = _spawned(argv)
    assert (spawn["--agent"], spawn["--parent-run-id"]) == ("reflection", MAIN)
    assert str(reg.events_path(MAIN)) in spawn["--task"] and "DURABLE CHANGE" in spawn["--task"] and "REDO" in spawn["--task"]


def test_a_reflections_durable_rows_go_to_the_librarian(asked):
    watch = reflection.ReflectionWatch(started=time.time() - 1)
    _ended(MAIN, agent="main", name="main")
    _ended(REFLECTION, agent="reflection", name="reflection", parent_run_id=MAIN,
           report="Done right: …\n## DURABLE CHANGE\n- D1 hook: refuse kill by pattern\n## REDO\n- R1 rerun the visual check")
    watch.tick(ReflectionCadence())
    [argv] = asked
    spawn = _spawned(argv)
    assert (spawn["--agent"], spawn["--parent-run-id"]) == ("librarian", MAIN)
    assert "D1 hook: refuse kill by pattern" in spawn["--task"] and "R1" not in spawn["--task"]
