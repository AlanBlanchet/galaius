"""A parent whose turn ended while its agents worked is resumed with their reports once the last one ends
(`galaius.agents.handback`). Owner 2026-10-16: « it said in the UI that tasks were done... But i never got another
report saying "DONE" forthe tasks... »."""

import json
import time
import uuid

import pytest

from galaius.agents import agent_queue, handback
from galaius.agents import registry as reg
from galaius.agents.spawn_line import SpawnLine
from tests.support import register_run


def _answered(run_id: str, text: str) -> None:
    reg.raw_events_path(run_id).write_text("".join(json.dumps(line) + "\n" for line in (
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "stop_reason": "end_turn", "result": text})))


def _ended(run_id: str, at: float, *, status: str = "done", **rest) -> None:
    register_run(run_id, **rest)
    reg.finish(run_id, exit_code=0 if status == "done" else 1)
    reg._merge_record_locked(run_id, {"status": status, "finished_at": at})


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def deliver(run_id, message, *, sender, message_id, only_if_idle, turn):
        assert only_if_idle and turn.refusal(message, None) is None
        calls.append({"run_id": run_id, "message": message, "message_id": message_id, "sender": sender})
        return handback.messaging.Delivery(state="queued", text="Queued.", run_id=run_id, queue_id="q")

    monkeypatch.setattr(handback.messaging, "deliver_message", deliver)
    return calls


@pytest.mark.parametrize("parent_status, before, woken", [
    pytest.param("done", False, True, id="ended-turn-resumed-with-both-reports"),
    pytest.param("stopped", False, False, id="stopped-by-a-person-left-alone"),
    pytest.param("done", True, False, id="children-ended-within-its-turn-nothing-to-hand-back"),
])
def test_the_last_child_to_end_resumes_its_ended_parent_with_every_report(sent, parent_status, before, woken):
    _ended("lead", 100.0, status=parent_status, agent="main", provider_session_id="vendor-lead")
    child_end = 50.0 if before else 200.0
    _ended("eng", child_end, agent="backend-engineer", parent_run_id="lead")
    _answered("eng", "DONE: endpoint shipped, image abc123.")
    _ended("rev", child_end + 1, agent="code-reviewer", parent_run_id="lead")
    _answered("rev", "Verdict: PASS.")

    first = handback.hand_back("rev")
    again = handback.hand_back("eng")  # a second ending read (same tick, another reader) queues nothing new

    if not woken:
        assert first == [] and sent == []
        return
    assert first and [call["run_id"] for call in sent] == ["lead", "lead"]
    assert sent[0]["message_id"] == sent[1]["message_id"]
    message = sent[0]["message"]
    assert "DONE: endpoint shipped, image abc123." in message and "Verdict: PASS." in message
    assert "eng" in message and "rev" in message
    assert again


@pytest.mark.parametrize("latest, told", [
    pytest.param("coordinator", ["parent", "coordinator"], id="asked-last-by-another-run-both-hear"),
    pytest.param("parent", ["parent"], id="asked-last-by-its-parent-only-it-hears"),
    pytest.param("person", ["parent"], id="asked-last-by-the-person-only-its-parent-hears"),
    pytest.param("resume", ["parent", "coordinator"], id="the-persons-continue-carries-on-the-coordinators-ask"),
])
def test_a_continued_run_hands_back_to_the_run_that_asked_it_last_as_well_as_its_parent(sent, latest, told):
    """2026-10-17: a continued instance's reports reached its parent, never the coordinator that had asked it the work."""
    ids = {name: str(uuid.UUID(int=n)) for n, name in enumerate(("parent", "coordinator", "eng"), start=1)} | {"person": reg.PERSON_SENDER, "resume": reg.PERSON_SENDER}
    _ended(ids["parent"], 100.0, agent="main")
    _ended(ids["coordinator"], 100.0, agent="main")
    _ended(ids["eng"], 200.0, agent="backend-engineer", parent_run_id=ids["parent"])
    for sender in ("parent", "coordinator", latest):
        with reg.record_lock(ids["eng"]):
            reg.note_asker_locked(ids["eng"], ids[sender])
        reg.record_message_event(from_run=ids[sender], to_run=ids["eng"], text=reg.RESUME_BRIEF if sender == "resume" else f"ask from {sender}")
    _answered(ids["eng"], "DONE: row G live.")

    handback.hand_back(ids["eng"])

    assert sorted(call["run_id"] for call in sent) == sorted(ids[name] for name in told)
    assert all("DONE: row G live." in call["message"] for call in sent)


def test_a_child_still_at_work_holds_the_hand_back(sent, at_work):
    _ended("lead", 100.0, agent="main", provider_session_id="vendor-lead")
    _ended("eng", 200.0, agent="backend-engineer", parent_run_id="lead")
    at_work("rev", agent="code-reviewer", parent_run_id="lead")

    assert handback.hand_back("eng") == [] and sent == []


def test_wait_returns_each_runs_final_report_once_all_ended(at_work):
    _ended("eng", 200.0, agent="backend-engineer")
    _answered("eng", "DONE: endpoint shipped.")
    worker = at_work("rev", agent="code-reviewer")

    pending = handback.wait(["eng", "rev"], timeout=0.2, interval=0.05).reports
    assert [report.run_id for report in pending if report.pending] == ["rev"]

    worker.kill()
    worker.wait()
    reg._merge_record_locked("rev", {"status": "done", "finished_at": 300.0})
    _answered("rev", "Verdict: PASS.")
    reports = handback.wait(["eng", "rev"], timeout=5, interval=0.05).reports
    assert [(report.run_id, report.pending, report.text) for report in reports] == [
        ("eng", False, "DONE: endpoint shipped."), ("rev", False, "Verdict: PASS.")]


def test_a_message_for_the_waiting_run_cuts_its_wait_short(at_work, monkeypatch):
    """2026-10-16: the owner wrote to the coordinator while it sat in `galaius agents wait --timeout 580`; his words
    reached it 9.4 min later, when the wait timed out (« I find Claude to be pretty slow today »). A message queued for
    the run that waits ends the wait at once, so it reaches that run at its next step."""
    register_run("lead", agent="main")
    at_work("rev", agent="code-reviewer", parent_run_id="lead")
    monkeypatch.setattr(reg, "vendor_said_at", lambda run: time.time() - 900)

    quiet = handback.wait(["rev"], timeout=0.2, interval=0.05, asker="lead")
    assert quiet.message is None and [report.silent_for // 60 for report in quiet.reports] == [15]

    agent_queue.enqueue("lead", message_id="c1", sender="operator", turn=agent_queue.CompactionTurn())
    behind = handback.wait(["rev"], timeout=0.3, interval=0.05, asker="lead")  # read only once its turn ends: no cut
    assert behind.message is None and [report.pending for report in behind.reports] == [True]
    with reg.record_lock("lead"):
        agent_queue.cancel_pending_locked("lead")

    item = agent_queue.enqueue("lead", message_id="m1", sender="operator")
    started = time.monotonic()
    waited = handback.wait(["rev"], timeout=30, interval=0.05, asker="lead")
    assert time.monotonic() - started < 5
    assert waited.message is not None and waited.message.id == item.id and [report.pending for report in waited.reports] == [True]


def test_the_waiting_run_gives_its_place_back_and_a_message_still_cuts_its_wait_at_once(at_work, monkeypatch):
    """2026-10-17: every place on the computer held, half by runs sitting in `galaius agents wait` on a run that is
    not their child, which queued behind them. Woken on a full line, it counts again at once: never a wait for a place."""
    monkeypatch.setattr(SpawnLine, "here", classmethod(lambda cls: cls(limit=1, waiting_flag_minutes=5, waiting_gap_seconds=60)))
    at_work("lead", agent="main")
    at_work("deployer", agent="deployer")
    looks: list[frozenset[str]] = []

    def look(seconds: float) -> None:
        looks.append(SpawnLine.here().set_aside())
        agent_queue.enqueue("lead", message_id="m1", sender="operator")

    monkeypatch.setattr(handback.time, "sleep", look)
    started = time.monotonic()
    waited = handback.wait(["deployer"], timeout=30, interval=0.05, asker="lead")
    assert time.monotonic() - started < 5 and waited.message is not None and looks == [frozenset({"lead"})]
    with SpawnLine.here()._places() as places:
        assert SpawnLine.here().load(places).places == 2 and SpawnLine.here().set_aside() == frozenset()


@pytest.fixture
def at_work():
    import subprocess
    import sys

    children: list[subprocess.Popen] = []

    def start(run_id: str, **rest) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        children.append(child)
        register_run(run_id=run_id, pid=child.pid, **rest)
        return child

    yield start
    for child in children:
        child.kill()
        child.wait()


def test_the_watch_sees_a_run_leave_its_live_set_and_one_that_lived_between_two_looks(at_work):
    worker = at_work("long", agent="backend-engineer", parent_run_id="lead")
    watch = handback.EndWatch()
    _ended("brief", watch.looked + 0.001, agent="code-reviewer", parent_run_id="lead")  # started and ended between looks
    _ended("old", watch.looked - 60, agent="tester", parent_run_id="lead")  # ended before the watch began
    worker.kill()
    worker.wait()

    assert watch.ended() == ["brief", "long"]
    assert watch.ended() == []


def test_the_watch_hands_back_a_run_with_no_parent_to_the_run_it_works_for(sent):
    """A run started by the person, then asked work by another run, reports to that run once it ends."""
    coordinator, worker = str(uuid.UUID(int=1)), str(uuid.UUID(int=2))
    watch = handback.EndWatch()
    _ended(coordinator, watch.looked - 10, agent="main")
    register_run(worker, agent="backend-engineer")
    with reg.record_lock(worker):
        reg.note_asker_locked(worker, coordinator)
    reg.record_message_event(from_run=coordinator, to_run=worker, text="row I")
    reg.finish(worker, exit_code=0)
    _answered(worker, "DONE: row I live.")

    watch.tick()

    assert [call["run_id"] for call in sent] == [coordinator] and "DONE: row I live." in sent[0]["message"]
