"""A parent whose turn ended while its agents worked is resumed with their reports once the last one ends
(`galaius.agents.handback`). Owner 2026-10-16: « it said in the UI that tasks were done... But i never got another
report saying "DONE" forthe tasks... »."""

import json

import pytest

from galaius.agents import handback
from galaius.agents import registry as reg
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
        assert first is None and sent == []
        return
    assert first is not None and [call["run_id"] for call in sent] == ["lead", "lead"]
    assert sent[0]["message_id"] == sent[1]["message_id"]
    message = sent[0]["message"]
    assert "DONE: endpoint shipped, image abc123." in message and "Verdict: PASS." in message
    assert "eng" in message and "rev" in message
    assert again is not None


def test_a_child_still_at_work_holds_the_hand_back(sent, at_work):
    _ended("lead", 100.0, agent="main", provider_session_id="vendor-lead")
    _ended("eng", 200.0, agent="backend-engineer", parent_run_id="lead")
    at_work("rev", agent="code-reviewer", parent_run_id="lead")

    assert handback.hand_back("eng") is None and sent == []


def test_wait_returns_each_runs_final_report_once_all_ended(at_work):
    _ended("eng", 200.0, agent="backend-engineer")
    _answered("eng", "DONE: endpoint shipped.")
    worker = at_work("rev", agent="code-reviewer")

    pending = handback.wait(["eng", "rev"], timeout=0.2, interval=0.05)
    assert [report.run_id for report in pending if report.pending] == ["rev"]

    worker.kill()
    worker.wait()
    reg._merge_record_locked("rev", {"status": "done", "finished_at": 300.0})
    _answered("rev", "Verdict: PASS.")
    reports = handback.wait(["eng", "rev"], timeout=5, interval=0.05)
    assert [(report.run_id, report.pending, report.text) for report in reports] == [
        ("eng", False, "DONE: endpoint shipped."), ("rev", False, "Verdict: PASS.")]


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
