"""A parent whose turn ended while its agents worked is resumed with their reports once the last one ends
(`galaius.agents.handback`). Owner 2026-10-16: « it said in the UI that tasks were done... But i never got another
report saying "DONE" forthe tasks... »."""

import asyncio
import json
import os
import time
import uuid

import pytest

from galaius.agents import agent_queue, handback, relations
from galaius.agents import registry as reg
from galaius.agents.events import Relation
from galaius.agents.spawn_line import Spawn, SpawnLine
from tests.support import register_run


def _answered(run_id: str, text: str) -> None:
    reg.raw_events_path(run_id).write_text("".join(json.dumps(line) + "\n" for line in (
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "stop_reason": "end_turn", "result": text})))


def _ended(run_id: str, at: float, *, status: str = "done", **rest) -> None:
    register_run(run_id, **rest)
    reg.finish(run_id, exit_code=0 if status == "done" else 1, first=0)
    reg._merge_record_locked(run_id, {"status": status, "finished_at": at})


@pytest.fixture(autouse=True)
def _carried_since_ever(monkeypatch):
    """These tests' runs end at fixed times: every report counts (`handback_since`)."""
    monkeypatch.setattr(relations, "handback_since", lambda **_: 0.0)


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
    pytest.param("done", True, True, id="children-ended-within-its-turn-handed-back-too"),   # never handed to it then
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


def test_a_child_its_lost_login_cut_wakes_its_parent_only_once_nothing_resumes_it(sent):
    """Owner 2026-10-19 (« 4 of its agents stopped »): a cut that resumes by itself (`registry.self_resuming`) is no
    report; its parent hears the child's report once it resumed and ended, or its cut once nothing will resume it."""
    reg.cut_resume_since(now=0.0)  # a runner here resumes cuts
    _ended("lead", 100.0, agent="main", provider_session_id="vendor-lead")
    reg.raw_events_path("eng").write_text(json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
                                                      "session_id": "s", "result": "Failed to authenticate: OAuth token revoked."}) + "\n")
    watch = handback.EndWatch(live=frozenset({"eng"}))
    _ended("eng", time.time(), status="failed", agent="backend-engineer", parent_run_id="lead")
    assert handback.hand_back("eng") == [] and watch.tick() == [] and sent == []
    # Its resume met the same lost login again: nothing resumes it any more, its launcher hears it on the next look, a
    # watcher started since (the PC's service restarted) too.
    reg._merge_record_locked("eng", {"resumed_at": reg.get_run("eng").finished_at - 10})
    restarted = handback.EndWatch()  # started since (the PC's service restarted)
    assert [delivery.run_id for delivery in restarted.tick()] == ["lead"]
    assert watch.tick() == [], "handed back once per ended turn"


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
    handback.collected(ids["eng"], [handback.Report.of(reg.get_run(ids["helper"]))])   # eng read its helper's report inline

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
    """An ask is answered by one hand-back, so that hand-back carries the final report, after the words of the turn that
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
    reg.finish(worker, exit_code=0, first=0)
    _answered(worker, "DONE: row I live.")

    watch.tick()

    assert [call["run_id"] for call in sent] == [coordinator] and "DONE: row I live." in sent[0]["message"]


def _spawned(run_id: str, agent: str, parent: str) -> None:
    """Start run `run_id` as `Spawn.start` does, its process this one."""
    async def runner(provider, task, *, run_id, **rest):
        register_run(run_id, agent=agent, name=agent, task=task, parent_run_id=parent, pid=os.getpid(),
                     asked_by=rest["asked_by"], inherited_asks=rest["inherited_asks"])

    asyncio.run(Spawn(task=f"{agent} task", cwd=".", agent=agent, parent_run_id=parent).start(run_id, runner))


@pytest.mark.parametrize("handoff", [{"compactions": 1, "compacted_to": 185_000, "context_tokens": 190_000, "context_window": 200_000}])
def test_a_continued_maker_is_resumed_by_its_critics_verdicts_while_its_coordinator_works(sent, at_work, handoff):
    """2026-10-17: a maker continuing a compacted maker, under a coordinator continuing a compacted one, had written to
    the coordinator first: the coordinator read as working for it, its orders as reports, and its turn (and agents)
    held the maker's hand-back for good — three critics' verdicts, one FAIL, never reached it."""
    root, coordinator, own, first, maker, fail, ok = (str(uuid.UUID(int=n << 96)) for n in range(1, 8))
    at_work(root, agent="main")
    reg._merge_record_locked(root, handoff)
    _spawned(coordinator, "main", root)  # each outgrown run hands off by spawning its role
    at_work(first, agent="app-engineer", parent_run_id=root)
    reg._merge_record_locked(first, handoff)
    _spawned(maker, "app-engineer", first)
    for ended, at in ((root, 10.0), (first, 20.0), (maker, 100.0)):
        reg._merge_record_locked(ended, {"status": "done", "finished_at": at, "pid": None})
    reg._merge_record_locked(maker, {"provider_session_id": "vendor-maker"})
    at_work(own, agent="backend-engineer", parent_run_id=coordinator)
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


@pytest.mark.parametrize("read_through, answered", [
    pytest.param("agents-wait", True, id="agents-wait"),
    pytest.param("replied", True, id="send-waiting-on-its-reply"),
    pytest.param("error", False, id="send-whose-item-was-cancelled-stays-owed"),
])
def test_a_report_read_inline_is_answered_and_never_handed_back_again(sent, monkeypatch, read_through, answered):
    """2026-10-17: a reviewer's verdict read through `agents wait` left its reader owed: when another run reused the
    reviewer, its next verdict also woke the reader that had it already. A send that got no reply read nothing."""
    reader, reviewer, other = (str(uuid.UUID(int=n << 96)) for n in (11, 12, 13))
    _ended(reader, 100.0, agent="infra-engineer")
    _ended(reviewer, 200.0, agent="code-reviewer", parent_run_id=reader)
    _answered(reviewer, "Verdict: PASS.")
    if read_through == "agents-wait":
        assert [report.text for report in handback.wait([reviewer], timeout=1, interval=0.05, asker=reader).reports] == ["Verdict: PASS."]
    else:
        from galaius.server import tools_agents

        async def replied(delivery):
            delivery.state = read_through
            return "Verdict: PASS." if read_through == "replied" else "ERROR: queued delivery was cancelled."
        with monkeypatch.context() as sending:
            sending.setenv("GALAIUS_RUN_ID", reader)
            sending.setattr(tools_agents.messaging, "deliver_message", lambda run_id, message, **_: handback.messaging.Delivery(state="queued", text="Queued.", run_id=run_id, queue_id="q"))
            sending.setattr(tools_agents.messaging, "wait_for_reply", replied)
            asyncio.run(tools_agents.agent_send(reviewer, "review row H", wait=True))
    assert (reader not in relations.owed(reg.get_run(reviewer))) is answered

    _ended(other, 250.0, agent="backend-engineer")  # reuses the reviewer for its own round
    with reg.record_lock(reviewer):
        reg.note_asker_locked(reviewer, other)
    reg.record_message_event(from_run=other, to_run=reviewer, text="round 3: review 10cf96fd")
    reg._merge_record_locked(reader, {"finished_at": 240.0})
    reg._merge_record_locked(reviewer, {"finished_at": 300.0})
    handback.hand_back(reviewer)

    assert sorted(call["run_id"] for call in sent) == sorted([other] if answered else [reader, other])


@pytest.fixture
def told(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def deliver(run_id, message, *, sender, message_id, turn):
        calls.append({"run_id": run_id, "message": message, "sender": sender})
        return handback.messaging.Delivery(state="queued", text="Queued.", run_id=run_id, queue_id="q")

    monkeypatch.setattr(handback.messaging, "deliver_message", deliver)
    return calls


def _silent(run_id: str, seconds: float, *, last: dict | None = None) -> None:
    raw = reg.raw_events_path(run_id)
    raw.write_text("".join(json.dumps(line) + "\n" for line in ({"type": "system", "subtype": "init", "session_id": "s"}, *([last] if last else []))))
    os.utime(raw, (time.time() - seconds, time.time() - seconds))


LIMITS = handback.Config(silent_seconds=600, silent_command_seconds=3600)
COMMAND = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}}]}}


@pytest.mark.parametrize("asked_by", ["launcher", "worked for"])
def test_runs_silent_too_long_are_named_once_to_the_run_at_work_that_asked_them(told, at_work, asked_by):
    lead, first, second = (str(uuid.UUID(int=n)) for n in (1, 2, 3))
    at_work(lead, agent="main")
    for run_id in (first, second):
        at_work(run_id, agent="backend-engineer", parent_run_id=lead if asked_by == "launcher" else None)
        if asked_by == "worked for":
            with reg.record_lock(run_id):
                reg.note_asker_locked(run_id, lead)
        _silent(run_id, 700)

    handback.SilenceWatch().tick(LIMITS)
    handback.SilenceWatch().tick(LIMITS)  # its next look, or the watch started again after the PC restarted: told already

    assert [(call["run_id"], call["sender"]) for call in told] == [(lead, reg.SILENCE_SENDER)]  # one message, from the watch
    assert all(run_id in told[0]["message"] and f"galaius agents events {run_id}" in told[0]["message"] for run_id in (first, second))
    assert "backend-engineer" in told[0]["message"] and "11 min" in told[0]["message"]
    _silent(first, 650)  # it wrote again, then went silent again
    handback.SilenceWatch().tick(LIMITS)
    assert len(told) == 2 and first in told[1]["message"] and second not in told[1]["message"]


@pytest.mark.parametrize("case", ["asker ended", "running one command", "running one command, sent a message since", "nobody asked"])
def test_no_run_is_woken_or_told_for_a_silence_that_is_not_one(told, at_work, case):
    lead, silent = str(uuid.UUID(int=1)), str(uuid.UUID(int=2))
    if case == "asker ended":
        _ended(lead, time.time() - 10, agent="main")
    else:
        at_work(lead, agent="main")
    at_work(silent, agent="backend-engineer", parent_run_id=None if case == "nobody asked" else lead)
    _silent(silent, 900, last=COMMAND if case.startswith("running one command") else None)  # 15 min
    if case.endswith("since"):
        reg.record_message_event(from_run=lead, to_run=silent, text="how is it going?")  # 5 min ago, during its command

    handback.SilenceWatch().tick(LIMITS)

    assert told == []


@pytest.mark.parametrize("read_mid_turn", [False, True], ids=["unread-handed-at-its-turn-end", "read-through-its-events-never-again"])
def test_a_verdict_that_ended_while_its_maker_worked_reaches_it_when_its_turn_ends(sent, monkeypatch, read_mid_turn):
    """2026-10-22: a reviewer spawned for e8092369 ended while e8092369 was at work (its hand-back refused then) and its
    verdict never reached it: the maker's own turn end carries it."""
    maker, reviewer, lead = (str(uuid.UUID(int=n)) for n in (41, 42, 43))
    _ended(lead, 100.0, agent="main")
    register_run(maker, agent="infra-engineer", provider_session_id="vendor-maker")
    _ended(reviewer, 150.0, agent="code-reviewer", parent_run_id=lead)
    with reg.record_lock(reviewer):
        reg.note_asker_locked(reviewer, maker)                     # spawned `--for` the maker
    verdict = "Verdict: PASS, one SHOULD row: " + " ".join(["the deploy line's name lives in three places; keep it in one file."] * 4)
    _answered(reviewer, verdict)
    handback.hand_back(reviewer)                                   # the maker is at work: nothing handed to it yet
    assert all(call["run_id"] != maker for call in sent)
    if read_mid_turn:   # the maker reads its reviewer's events (`agent_events`, `galaius agents events`) while it works
        from galaius.server import tools_agents
        monkeypatch.setenv("GALAIUS_RUN_ID", maker)
        assert verdict in asyncio.run(tools_agents.agent_events(reviewer))   # the whole verdict, past the events' cut lines
    reg.finish(maker, exit_code=0, first=0)
    reg._merge_record_locked(maker, {"status": "done", "finished_at": 200.0})
    sent.clear()
    handback.hand_back(maker)                                      # its own turn ends
    if read_mid_turn:
        assert sent == []
        return
    assert [call["run_id"] for call in sent] == [maker] and verdict in sent[0]["message"]


def test_a_reviewer_kept_for_two_makers_hands_each_its_own_verdict(sent, monkeypatch):
    """2026-10-25: a reviewer kept for several makers ended its turn for a second maker while the first maker's ask, which
    a turn had answered before this computer carried reports, was still counted; the first maker was woken with the second
    maker's verdict."""
    first, second, lead, reviewer = (str(uuid.UUID(int=n)) for n in (51, 52, 53, 54))
    _ended(lead, 100.0, agent="main")
    _ended(first, 100.0, agent="backend-engineer")
    register_run(second, agent="app-engineer")                          # still at work when the second verdict lands
    _ended(reviewer, 300.0, agent="code-reviewer", parent_run_id=lead)
    for maker in (first, second):
        with reg.record_lock(reviewer):
            reg.note_asker_locked(reviewer, maker)
    asks = [reg.record_message_event(from_run=maker, to_run=reviewer, text=f"review {maker[-2:]}") for maker in (first, second)]
    turns = [({"type": "system", "subtype": "init", "session_id": "s"},
              {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "stop_reason": "end_turn", "result": said})
             for said in ("Verdict for the first: PASS.", "Verdict for the second: FAIL, one row.")]
    for index, turn in enumerate(turns):                                # the first verdict ends before reports were carried
        with reg.raw_events_path(reviewer).open("a") as stream:
            stream.write("".join(json.dumps(line) + "\n" for line in turn))
        reg.read_events(reviewer)
        if index == 0:
            carried_from = time.time()
            monkeypatch.setattr(relations, "handback_since", lambda **_: carried_from)
    for message, (maker, line) in zip(asks, ((first, 0), (second, 2))):
        item = agent_queue.enqueue(reviewer, message_id=message, sender=maker)
        with reg.record_lock(reviewer):
            agent_queue._replace_item_locked(reviewer, item.id, state="replied", started_at=200.0 + line, finished_at=201.0 + line, raw_index=line)
    run = reg.get_run(reviewer)
    assert handback.Report.answering(run, first).text == "Verdict for the first: PASS."
    assert handback.Report.answering(run, second).text == "Verdict for the second: FAIL, one row."
    assert relations.owed(run) == (second,)
    handback.hand_back(reviewer)                                        # its turn for the second ends: the first is idle
    assert not [call for call in sent if call["run_id"] == first]


_INIT = {"type": "system", "subtype": "init", "session_id": "s"}


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}, "session_id": "s"}


def _result(text: str) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "stop_reason": "end_turn", "result": text}


_HOOK = {"type": "user", "isSynthetic": True, "session_id": "s",
         "message": {"role": "user", "content": [{"type": "text", "text": "Stop hook feedback:\nDELIVERY GATE: state the live commit."}]}}
_CLOSE = "Both fixes are live in build 27f54b4.\n\n**DONE**"


@pytest.mark.parametrize("lines, expected", [
    pytest.param([_INIT, _assistant("Deploying."), _assistant(_CLOSE), _result(_CLOSE)], _CLOSE, id="no-hook-unchanged"),
    pytest.param([_INIT, _assistant(_CLOSE), _HOOK, _assistant("Live build: 27f54b4."), _result("Live build: 27f54b4.")],
                 f"{_CLOSE}\n\nLive build: 27f54b4.", id="stop-hook-reply-after-the-close"),
    pytest.param([_INIT, _assistant(_CLOSE), _result(_CLOSE), _INIT, _assistant("Nothing changed since."), _result("Nothing changed since.")],
                 f"{_CLOSE}\n\nNothing changed since.", id="no-message-wake-after-the-close"),
])
def test_a_hand_back_keeps_the_close_a_hook_reply_or_a_wake_follows(lines, expected):
    """2026-10-28, backend-engineer 7cf011fd: its librarian was handed only its reply to the delivery-gate Stop hook
    (« Live build: … »), its close « Both fixes are live » dropped; a no-op wake replaced an answer the same way."""
    _ended("maker", 100.0, agent="backend-engineer")
    reg.raw_events_path("maker").write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert handback.Report.of(reg.get_run("maker")).text == expected


def test_a_clipped_hand_back_drops_its_oldest_parts_first():
    report = handback.Report(run_id="maker", agent="backend-engineer", status="done", pending=False,
                             text="a" * 90 + "\n\n" + "b" * 30, parts=("a" * 90, "b" * 30))
    assert report.section(limit=80) == f"## backend-engineer maker — done\n… (clipped; `galaius agents events maker`)\n\n{'b' * 30}"
