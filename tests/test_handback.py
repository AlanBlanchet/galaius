"""A parent whose turn ended while its agents worked is resumed with their reports once the last one ends
(`galaius.agents.handback`). Owner 2026-10-16: « it said in the UI that tasks were done... But i never got another
report saying "DONE" forthe tasks... »."""

import json
import time
import uuid

import pytest

from galaius.agents import agent_queue, handback, relations
from galaius.agents import registry as reg
from galaius.agents.events import Relation
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
        reg.record_message_event(from_run=sender, to_run=run_id, text=message, **turn.event_fields())  # as `queue_message`
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
    again = handback.hand_back("eng")  # a second ending read (same tick, another reader): both reports already carried

    if not woken:
        assert first == [] and sent == []
        return
    assert first and again == [] and [call["run_id"] for call in sent] == ["lead"]
    message = sent[0]["message"]
    assert "DONE: endpoint shipped, image abc123." in message and "Verdict: PASS." in message
    assert "eng" in message and "rev" in message


@pytest.mark.parametrize("latest, told", [
    pytest.param("coordinator", ["parent", "coordinator"], id="asked-by-another-run-mid-task-both-hear"),
    pytest.param("parent", ["parent", "coordinator"], id="asked-last-by-its-parent-the-coordinators-earlier-ask-still-owed"),
    pytest.param("person", ["parent", "coordinator"], id="asked-last-by-the-person-its-parent-hears"),
    pytest.param("resume", ["parent", "coordinator"], id="the-persons-continue-carries-on-the-coordinators-ask"),
    # 2026-10-17: a run that had only reported to a coordinator was woken by each of its later turns.
    pytest.param("reporter", ["parent", "coordinator"], id="a-one-way-note-carries-on-the-ask-before-it"),
])
def test_a_continued_run_hands_back_to_the_run_that_asked_it_last(sent, latest, told):
    """2026-10-17: a continued instance's reports reached its parent, never the coordinator that had asked it the work.
    Each asker whose ask is still unanswered hears, the parent for the task it spawned the run with included."""
    ids = {name: str(uuid.UUID(int=n)) for n, name in enumerate(("parent", "coordinator", "eng", "reporter"), start=1)} \
        | {"person": reg.PERSON_SENDER, "resume": reg.PERSON_SENDER}
    for name in ("parent", "coordinator", "reporter"):
        _ended(ids[name], 100.0, agent="main")
    _ended(ids["eng"], 200.0, agent="backend-engineer", parent_run_id=ids["parent"])
    for sender in ("parent", "coordinator", latest):
        fyi = sender == "reporter"
        if not fyi:  # a one-way note lists no asker (`MessageTurn.asks`)
            with reg.record_lock(ids["eng"]):
                reg.note_asker_locked(ids["eng"], ids[sender])
        reg.record_message_event(from_run=ids[sender], to_run=ids["eng"], text=reg.RESUME_BRIEF if sender == "resume" else f"ask from {sender}", fyi=fyi)
    _answered(ids["eng"], "DONE: row G live.")

    handback.hand_back(ids["eng"])

    assert sorted(call["run_id"] for call in sent) == sorted(ids[name] for name in told)
    assert all("DONE: row G live." in call["message"] for call in sent)


@pytest.mark.parametrize("next_turn, woken_again", [
    pytest.param("own-agents", False, id="its-own-agents-hand-back-leaves-the-answered-asker-alone"),
    pytest.param("continue", False, id="the-persons-continue-leaves-the-answered-asker-alone"),
    pytest.param("askers-hand-back", False, id="the-askers-own-hand-back-is-no-new-ask"),
    # A reused instance: the task its parent spawned it with is answered, a new ask from another run wakes that run only.
    pytest.param("new-ask", True, id="a-new-ask-from-it-is-answered-again-and-the-answered-parent-left-alone"),
])
def test_an_ask_is_answered_by_one_hand_back(sent, next_turn, woken_again):
    """2026-10-17: a run sent one report (no `--fyi`) became an asker for good. Its target woke it after each later turn:
    a librarian took 4 empty turns, and four runs looped every 3 min (the asker's own agents started each turn of the
    target, and the asker's own hand-back went up to them again)."""
    ids = {name: str(uuid.UUID(int=n)) for n, name in enumerate(("parent", "coordinator", "eng", "helper"), start=1)}
    for name in ("parent", "coordinator"):
        _ended(ids[name], 100.0, agent="main")
    _ended(ids["eng"], 200.0, agent="backend-engineer", parent_run_id=ids["parent"])
    _ended(ids["helper"], 150.0, agent="tester", parent_run_id=ids["eng"])

    def ask(sender: str, text: str, reports: tuple[str, ...] = ()) -> None:
        if sender == "coordinator" and not reports:
            with reg.record_lock(ids["eng"]):
                reg.note_asker_locked(ids["eng"], ids[sender])
        reg.record_message_event(from_run=ids.get(sender, reg.PERSON_SENDER), to_run=ids["eng"], text=text, reports=reports)

    ask("coordinator", "status of row H?")
    _answered(ids["eng"], "Row H live.")
    handback.hand_back(ids["eng"])
    assert sorted(call["run_id"] for call in sent) == sorted([ids["parent"], ids["coordinator"]])

    for name in ("parent", "coordinator"):
        reg._merge_record_locked(ids[name], {"finished_at": 250.0})
    {"own-agents": lambda: ask("helper", "The agents you started or asked ended…"),
     "continue": lambda: ask("person", reg.RESUME_BRIEF),
     "askers-hand-back": lambda: ask("coordinator", "The agents you started or asked ended…", reports=(ids["helper"],)),
     "new-ask": lambda: ask("coordinator", "and row I?")}[next_turn]()
    reg._merge_record_locked(ids["eng"], {"finished_at": 300.0})
    sent.clear()
    handback.hand_back(ids["eng"])

    assert [call["run_id"] for call in sent] == [ids["coordinator"]] * woken_again


def test_a_run_waiting_on_its_own_agents_hands_back_its_final_report_once(sent, at_work):
    """An ask is answered by one hand-back, so that hand-back carries the final report, never the words of a turn that
    ended to wait on its own agents."""
    ids = {name: str(uuid.UUID(int=n)) for n, name in enumerate(("parent", "coordinator", "eng", "helper"), start=1)}
    for name in ("parent", "coordinator"):
        _ended(ids[name], 100.0, agent="main")
    _ended(ids["eng"], 200.0, agent="backend-engineer", parent_run_id=ids["parent"])
    with reg.record_lock(ids["eng"]):
        reg.note_asker_locked(ids["eng"], ids["coordinator"])
    reg.record_message_event(from_run=ids["coordinator"], to_run=ids["eng"], text="ship row H")
    helper = at_work(ids["helper"], agent="tester", parent_run_id=ids["eng"])
    _answered(ids["eng"], "Dispatched the tester; waiting.")

    assert handback.hand_back(ids["eng"]) == [] and sent == []

    helper.kill()
    helper.wait()
    reg._merge_record_locked(ids["helper"], {"status": "done", "finished_at": 260.0})
    reg.record_message_event(from_run=ids["helper"], to_run=ids["eng"], text="The agents you started or asked ended…",
                             reports=(ids["helper"],))
    with reg.raw_events_path(ids["eng"]).open("a") as raw:  # its next turn's answer, appended as the vendor writes it
        raw.write(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": "s",
                              "stop_reason": "end_turn", "result": "Row H live."}) + "\n")
    reg._merge_record_locked(ids["eng"], {"finished_at": 300.0})
    handback.hand_back(ids["eng"])

    assert sorted(call["run_id"] for call in sent) == sorted([ids["parent"], ids["coordinator"]])
    assert all("Row H live." in call["message"] for call in sent)


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


@pytest.mark.parametrize("handoff", [
    pytest.param({"compactions": 2}, id="compacted-runs-handing-off-unrecorded"),
    pytest.param({"context_tokens": 190_000, "context_window": 200_000}, id="full-contexts-handing-off-unrecorded"),
])
def test_a_continued_maker_is_resumed_by_its_critics_verdicts_while_its_coordinator_works(sent, at_work, handoff):
    """2026-10-17: a maker continuing a compacted maker, under a coordinator continuing a compacted one, had written to
    the coordinator first: the coordinator read as working for it, its orders as reports, and its turn (and agents)
    held the maker's hand-back for good — three critics' verdicts, one FAIL, never reached it."""
    root, coordinator, own, first, maker, fail, ok = (str(uuid.UUID(int=n << 96)) for n in range(1, 8))
    _ended(root, 10.0, agent="main")
    at_work(coordinator, agent="main", parent_run_id=root)
    at_work(own, agent="backend-engineer", parent_run_id=coordinator)
    _ended(first, 20.0, agent="app-engineer", parent_run_id=root)
    for outgrown in (root, first):
        reg._merge_record_locked(outgrown, handoff)
    _ended(maker, 100.0, agent="app-engineer", parent_run_id=first, provider_session_id="vendor-maker")
    with reg.record_lock(coordinator):
        reg.note_asker_locked(coordinator, maker)
    reg.record_message_event(from_run=maker, to_run=coordinator, text="Row A landed; critics dispatched.")
    for critic, verdict, at in ((fail, "Verdict: FAIL — the footer overlaps the list.", 200.0), (ok, "Verdict: PASS.", 201.0)):
        _ended(critic, at, agent="visual-critic", parent_run_id=maker)
        _answered(critic, verdict)

    handback.hand_back(ok)

    assert [call["run_id"] for call in sent] == [maker]
    assert "FAIL — the footer overlaps the list." in sent[0]["message"] and "Verdict: PASS." in sent[0]["message"]
    maker_run, coordinator_run = reg.get_run(maker), reg.get_run(coordinator)
    assert relations.boss(maker_run) == coordinator
    assert relations.relation(coordinator_run, maker_run, "fix the footer") is Relation.ASKER
    assert relations.relation(maker_run, coordinator_run, "Row A live.") is Relation.REPORT
