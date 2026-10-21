"""A coordinator's answer to the person gets its reflection from the runtime, once, when the answer ended with its agents
back, waking no one; what the answer still owes is on the coordinator's next turn, the durable rows go to the prompts'
owner (`galaius.agents.reflection`; 2026-10-21: the step lived only as prompt text and was skipped on nearly every turn)."""

import json
import time
import uuid

import pytest

from galaius.agents import handback, reflection, relations
from galaius.agents import registry as reg
from galaius.config.settings import ReflectionCadence
from tests.support import register_run

MAIN = str(uuid.UUID(int=21 << 96))
REFLECTION = str(uuid.UUID(int=22 << 96))
REPORT = "Done right: …\n## DURABLE CHANGE\n- D1 hook: refuse kill by pattern\n## REDO\n- R1 rerun the visual check"


def _ended(run_id: str, *, report: str = "", **rest) -> None:
    if reg.get_run(run_id) is None:
        register_run(run_id, **rest)
    reg.raw_events_path(run_id).write_text("".join(json.dumps(line) + "\n" for line in (
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "stop_reason": "end_turn", "result": report})))
    reg.finish(run_id, exit_code=0, first=0)
    reg._merge_record_locked(run_id, {"status": "done", "finished_at": time.time()})


@pytest.fixture
def asked(monkeypatch) -> list[dict[str, str]]:
    """Each spawn the watcher asks, its reflection taken by run REFLECTION (started under MAIN)."""
    calls: list[dict[str, str]] = []

    def spawn(argv, **_):
        tail = argv[argv.index("spawn") + 1:]
        calls.append(dict(zip(tail[::2], tail[1::2])))
        if reg.get_run(REFLECTION) is None:
            register_run(REFLECTION, agent="reflection", name="reflection", parent_run_id=MAIN)
        return reflection.subprocess.CompletedProcess(argv, 0, f"{REFLECTION}\n", "")

    monkeypatch.setattr(reflection.subprocess, "run", spawn)
    return calls


@pytest.mark.parametrize("opened", ["his-message", "his-words-started-it", "an-agents-message"])
def test_a_coordinators_answer_to_the_person_is_reflected_on_once_waking_no_one(asked, opened):
    watch = reflection.ReflectionWatch(started=time.time() - 1)
    register_run(MAIN, agent="main", name="main", asked_by=(str(uuid.uuid4()),) if opened != "his-words-started-it" else ())
    if opened != "his-words-started-it":
        reg.record_message_event(from_run=reg.PERSON_SENDER if opened == "his-message" else str(uuid.uuid4()), to_run=MAIN, text="ajoute le mode sombre")
    _ended(MAIN)
    watch.tick(ReflectionCadence())
    watch.tick(ReflectionCadence())   # the next look: already asked
    if opened == "an-agents-message":   # no answer to the person
        assert asked == []
        return
    [spawn] = asked
    assert (spawn["--agent"], spawn["--parent-run-id"]) == ("reflection", MAIN)
    assert str(reg.events_path(MAIN)) in spawn["--task"] and "DURABLE CHANGE" in spawn["--task"] and "REDO" in spawn["--task"]
    _ended(REFLECTION, report=REPORT)
    assert MAIN not in relations.owed(reg.get_run(REFLECTION)) and handback.hand_back(REFLECTION) == []   # wakes no one


def test_the_open_rows_reach_the_coordinators_next_turn_and_the_durable_ones_the_librarian(asked):
    watch = reflection.ReflectionWatch(started=time.time() - 1)
    register_run(MAIN, agent="main", name="main", asked_by=(str(uuid.uuid4()),))   # no answer to the person of its own
    _ended(MAIN)
    _ended(REFLECTION, report=REPORT, agent="reflection", name="reflection", parent_run_id=MAIN)
    watch.tick(ReflectionCadence())
    [spawn] = asked
    assert (spawn["--agent"], spawn["--parent-run-id"]) == ("librarian", MAIN)
    assert "D1 hook: refuse kill by pattern" in spawn["--task"] and "R1" not in spawn["--task"]
    card = relations.card(reg.get_run(MAIN))   # its next turn: drawn before that turn ends
    assert "R1 rerun the visual check" in card and "D1" not in card
    _ended(MAIN)   # that turn ended: the rows are not shown again
    assert "R1 rerun" not in relations.card(reg.get_run(MAIN))
