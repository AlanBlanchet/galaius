"""A stop he made sticks (owner 2026-11-04: « I also stopped, but when i came back it actually continues working... And a
subagent was still working... »): whatever would start the run again by itself waits, until his own message (his
« Continuer » included) releases it (`registry.AgentRun.held_at`)."""

import time

import pytest
from galaius_core import StopActor

from galaius.agents import agent_queue, messaging
from galaius.agents import registry as reg
from galaius.agents.turns import handback
from tests.support.agents import setup_queue

HIM = StopActor(kind="person", name="Alan")


def _agent(run_id: str, parent: str) -> None:
    reg.register(run_id=run_id, pid=999998, provider="queue-fake", name=run_id, task="t", agent="tester",
                 parent_run_id=parent, provider_session_id=f"vendor-{run_id}")


def _ended_turn() -> None:
    """The coordinator's turn ended while its agents work: it reads done (the press finds it ended)."""
    reg.finish("r1", exit_code=0, first=0)


def _process_gone() -> None:
    """Its process went away mid-turn (the PC restarted): a cut this PC resumes by itself."""
    reg._merge_record_locked("r1", {"status": "interrupted", "finished_at": time.time()})
    assert reg.cut_cause(reg.get_run("r1")) == "process_gone"


def _report_from(sender: str) -> None:
    assert messaging.deliver_message("r1", f"report of {sender}", sender=sender).state != "error"


@pytest.mark.parametrize("before, wake", [
    pytest.param(_ended_turn, lambda: (_agent("c1", "r1"), _report_from("c1")), id="a-child-report-arrives"),
    pytest.param(_process_gone, lambda: reg.claim_resume("r1", now=time.time() + 60), id="b-process-gone-auto-resume"),
    pytest.param(lambda: (reg._merge_record_locked("r1", {"parent_run_id": "lead"}), _ended_turn()),
                 lambda: (_agent("g1", "r1"), _report_from("g1")), id="c-grandchild-reports-to-a-stopped-middle-run"),
])
def test_a_stopped_run_stays_stopped_until_he_writes(monkeypatch, before, wake):
    provider = setup_queue(monkeypatch)
    monkeypatch.setattr(reg, "_cut_since", lambda: 0.0)   # this PC resumes cuts
    before()

    assert reg.stop("r1", by=HIM) is True
    assert reg.get_run("r1").status == "stopped"   # « Arrêté » on every row, its turn ended before his press or not
    wake()
    agent_queue.dispatch("r1")

    run = reg.get_run("r1")
    assert provider.messages == [] and run.held_at is not None and run.status == "stopped"
    assert reg.cut_resume(run) is None
    assert not handback._pending(run)   # a held run's queue never holds its own launcher's hand-back
    waited = [item.state for item in agent_queue.items("r1")]

    assert messaging.deliver_message("r1", "continue", sender=reg.PERSON_SENDER).state != "error"
    agent_queue.dispatch("r1")

    # What waited is read first, then his words; the run is no longer held.
    assert len(provider.messages) == len(waited) + 1 and "continue" in provider.messages[-1]
    assert reg.get_run("r1").held_at is None


@pytest.mark.parametrize("by, held", [
    pytest.param(HIM, True, id="his-stop-holds"),
    pytest.param(StopActor(kind="terminal"), True, id="his-terminal-holds"),
    pytest.param(StopActor(kind="agent", name="main"), False, id="an-agent-ending-its-finished-agent-never-holds"),
    pytest.param(None, False, id="its-own-cap-is-a-cut"),
])
def test_who_stops_decides_whether_the_stop_holds(monkeypatch, by, held):
    setup_queue(monkeypatch)
    _ended_turn()
    reg.stop("r1", by=by)
    assert (reg.get_run("r1").held_at is not None) is held


def test_work_from_the_run_it_works_for_releases_it_once_that_run_runs_again(monkeypatch):
    """His « Continuer » on the coordinator: its next message to the agent the same press stopped starts that agent."""
    provider = setup_queue(monkeypatch)
    reg.register(run_id="lead", pid=999997, provider="queue-fake", name="lead", task="t", agent="main", provider_session_id="vendor-lead")
    reg._merge_record_locked("r1", {"parent_run_id": "lead"})
    _ended_turn()
    for run_id in ("lead", "r1"):
        reg.stop(run_id, by=HIM)

    _report_from("lead")   # the coordinator still held: its message waits
    agent_queue.dispatch("r1")
    assert provider.messages == []

    reg._merge_record_locked("lead", {"held_at": None})   # he wrote to it
    _report_from("lead")
    agent_queue.dispatch("r1")
    assert len(provider.messages) == 2 and reg.get_run("r1").held_at is None
