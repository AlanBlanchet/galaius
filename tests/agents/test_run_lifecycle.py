"""The agent-run machinery never loses or strands work when one of its own code paths breaks or the PC reboots: the repair
channel never depends on the broken path. One row per way it happened (2026-11-21 / 11-22):

1. a resume / send / hand-back raising a code error (`'Model' object has no attribute 'context_length'`) kept four critic
   reports from their makers for a day, ~200 tracebacks an hour per run;
2. a reboot lost six queued spawns, no run record left;
3. a poll starting a subprocess at every tick (`claude auth status` for 25 logins every few seconds)."""

import ast
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from galaius.agents import agent_queue, messaging, registry as reg, run as run_module, spawn_line
from galaius.agents.catalog import relations
from galaius.agents.limits import fallback
from galaius.agents.spawn_line import REVIVALS_MAX, Spawn, SpawnLine
from galaius.agents.turns import handback
from tests.agents.limits.test_resume_swap import _stopped_run
from tests.support import register_run

BUG = "'Model' object has no attribute 'context_length'"


def _a_resume_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the 2026-11-22 bug did: the window of the run's model is read from a field its registry row has not."""
    def broken(model, candidates):
        raise AttributeError(BUG)

    monkeypatch.setattr(run_module, "_context_of", broken)
    monkeypatch.setattr(messaging, "policy_for_continuation", lambda run, provider, environment=None: broken(None, None))


def _fresh_runs(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """The runs the fallback starts: `run_agent` is replaced by a runner that records the run (no CLI is started)."""
    started: list[dict] = []

    async def runner(provider, task, *, run_id, **rest):
        started.append({"run_id": run_id, "task": task, **rest})
        reg.register(run_id=run_id, pid=999998, provider="claude", name=rest.get("name") or "worker", task=task, agent=rest.get("agent"),
                     parent_run_id=rest.get("parent_run_id"))
        return SimpleNamespace(run_id=run_id)

    monkeypatch.setattr(run_module, "run_agent", runner)
    return started


def _reported(run_id: str, text: str) -> None:
    reg.raw_events_path(run_id).write_text("".join(json.dumps(line) + "\n" for line in (
        {"type": "system", "subtype": "init", "session_id": "vendor"},
        {"type": "result", "subtype": "success", "is_error": False, "session_id": "vendor", "stop_reason": "end_turn", "result": text})))


def test_a_send_to_an_ended_run_whose_resume_raises_a_code_error_reaches_a_fresh_run_carrying_its_last_report(monkeypatch, tmp_path):
    _stopped_run(monkeypatch, tmp_path)
    _reported("r1", "VERDICT: PASS on the perf review.")
    _a_resume_raising(monkeypatch)
    started = _fresh_runs(monkeypatch)

    delivery = messaging.deliver_message("r1", "does the fix pass?", sender="operator")

    assert delivery.state == "queued" and delivery.relaunched_as == started[0]["run_id"], delivery
    task = started[0]["task"]
    assert "does the fix pass?" in task and "VERDICT: PASS on the perf review." in task and BUG in task
    fresh = reg.get_run(started[0]["run_id"])
    assert fresh.relaunched_from.run_id == "r1" and fresh.relaunched_from.cause == "fault" and "AttributeError" in fresh.relaunched_from.said
    assert reg.get_run("r1").replaced_by.run_id == fresh.run_id
    assert relations.live("r1") == fresh.run_id, "what is sent to the ended run later reaches its successor"
    assert "could not be resumed" in fresh.relaunched_from.line("tester")


def test_an_error_that_is_no_code_fault_still_reaches_its_sender_and_starts_no_run(monkeypatch, tmp_path):
    _stopped_run(monkeypatch, tmp_path)
    started = _fresh_runs(monkeypatch)
    monkeypatch.setattr(messaging, "policy_for_continuation", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        messaging.deliver_message("r1", "go on", sender="operator")
    assert started == []


def test_a_resume_the_dispatcher_cannot_start_for_a_code_reason_goes_to_a_fresh_run_at_once_never_tried_again(monkeypatch, tmp_path):
    _stopped_run(monkeypatch, tmp_path)
    _reported("r1", "Shipped.")
    started = _fresh_runs(monkeypatch)
    monkeypatch.setattr(agent_queue.QueuedTurn, "launch", lambda self, run, prompt, *, raw_index: (_ for _ in ()).throw(AttributeError(BUG)))
    messaging.deliver_message("r1", "go on", sender="operator")

    agent_queue.dispatch("r1")

    [item] = agent_queue.items("r1")
    assert item.state == "replied" and item.attempts == 0, "no pause, no second try of a bug"
    assert len(started) == 1 and "go on" in started[0]["task"] and "Shipped." in started[0]["task"]
    assert reg.get_run("r1").status != "failed", "the run is not failed by what its own code broke"


def test_no_fresh_run_possible_the_failure_still_reads_on_the_run_and_its_sender(monkeypatch, tmp_path):
    _stopped_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fallback, "_successor", lambda run, task, relaunches: None)
    monkeypatch.setattr(agent_queue.QueuedTurn, "launch", lambda self, run, prompt, *, raw_index: (_ for _ in ()).throw(AttributeError(BUG)))
    messaging.deliver_message("r1", "go on", sender="operator")
    agent_queue.dispatch("r1")
    [item] = agent_queue.items("r1")
    assert item.state == "failed" and BUG in item.error


def test_a_hand_back_that_raises_every_time_is_tried_with_growing_pauses_and_reported_once(monkeypatch, caplog):
    calls: list[str] = []

    def broken(run_id):
        calls.append(run_id)
        raise ValueError("unreadable")

    monkeypatch.setattr(handback, "hand_back", broken)
    monkeypatch.setattr(SpawnLine, "revive", lambda self: [])
    monkeypatch.setattr(SpawnLine, "report_lost", lambda self: [])
    watch = handback.EndWatch()
    watch.held = frozenset({"eng"})   # a run that ended since the last look
    with caplog.at_level("ERROR"):
        for _ in range(6):   # six looks within the first pause
            watch.tick()
        assert calls == ["eng"], "one try, then its pause"
        pauses = []
        for _ in range(handback.HAND_BACK_TRIES + 2):
            if "eng" in watch.failed:
                pauses.append(watch.failed["eng"].tries)
                watch.failed["eng"].next_at = 0.0   # its pause is over
            watch.tick()
    assert pauses[:3] == [1, 2, 3] and "eng" not in watch.failed, "given up after its tries"
    assert len(calls) == handback.HAND_BACK_TRIES
    assert sum("failed; tried again" in record.getMessage() for record in caplog.records) == 1, "its traceback once"
    assert sum("given up" in record.getMessage() for record in caplog.records) == 1
    delays = [handback.HandBackFailure.after(handback.HandBackFailure(tries=n, next_at=0.0), 0.0).next_at for n in range(1, 12)]
    assert delays == sorted(delays) and delays[-1] == handback.HAND_BACK_LAST_PAUSE


@pytest.fixture
def line_held_by_this_test(monkeypatch) -> list[str]:
    """Every waiter the line starts (`_hold`) recorded, none started."""
    held: list[str] = []
    monkeypatch.setattr(SpawnLine, "_hold", lambda self, run_id, spawn, asker: held.append(run_id))
    return held


def _spawn() -> Spawn:
    return Spawn(task="review the fix", cwd=".", agent="code-reviewer", parent_run_id=None, session_id="s1")


def _busy_line() -> SpawnLine:
    busy = SpawnLine(limit=1, waiting_flag_minutes=5, waiting_gap_seconds=60)
    register_run("busy", pid=os.getpid())
    return busy


def test_a_queued_spawn_whose_holder_is_gone_keeps_its_place_and_is_held_again(line_held_by_this_test):
    """A reboot: no process of the line is left (`holder_started` reads another process), the run has no record. The spawn is neither failed
    nor told lost: it waits in its place and `revive` starts its waiter again."""
    line = _busy_line()
    first, second = line.enter(_spawn()), line.enter(_spawn())
    with line._places() as places:
        places[:] = [place.model_copy(update={"holder_started": -1}) for place in places]   # every holder gone
    line_held_by_this_test.clear()

    seen = line._places_seen()
    assert [(place.run_id, place.state, place.lost) for place in seen] == [(first.run_id, "waiting", False), (second.run_id, "waiting", False)]
    assert line.report_lost() == [], "nobody is told of a spawn that still waits"
    assert line.revive() == [first.run_id, second.run_id] and line_held_by_this_test == [first.run_id, second.run_id]
    with line._places() as places:
        assert [place.revivals for place in places] == [1, 1]


def test_a_revived_spawn_starts_when_a_place_frees_in_the_order_it_was_asked(monkeypatch):
    started: list[str] = []

    async def runner(provider, task, *, run_id, **rest):
        started.append(run_id)

    monkeypatch.setattr(spawn_line, "run_agent", runner)
    monkeypatch.setattr(SpawnLine, "_hold", lambda self, run_id, spawn, asker: None)
    line = _busy_line()
    first, second = line.enter(_spawn()), line.enter(_spawn())
    with line._places() as places:
        places[:] = [place.model_copy(update={"holder_started": -1}) for place in places]
    reg.save_run(reg.get_run("busy").model_copy(update={"status": "done", "finished_at": time.time()}))
    reg.finish("busy", exit_code=0, first=0)

    assert line._turn(second.run_id) == "wait", "the earlier spawn goes first"
    turn = line._turn(first.run_id)
    assert turn != "wait" and turn != "gone" and turn.run_id == first.run_id


def test_a_spawn_whose_start_keeps_killing_its_holder_is_told_lost_after_its_revivals(line_held_by_this_test, monkeypatch):
    told: list[str] = []
    monkeypatch.setattr(spawn_line.messaging, "deliver_message", lambda run_id, message, **rest: (
        told.append(run_id), spawn_line.messaging.Delivery(state="queued", text="q", run_id=run_id))[1])
    register_run("lead", pid=os.getpid())
    line = _busy_line()
    asked = line.enter(Spawn(task="review the fix", cwd=".", agent="code-reviewer", parent_run_id="lead", session_id="s1"))
    for _ in range(REVIVALS_MAX):
        with line._places() as places:
            places[:] = [place.model_copy(update={"holder_started": -1}) for place in places]   # held again, gone again
        line.revive()
        assert line.report_lost() == []
    with line._places() as places:
        places[:] = [place.model_copy(update={"holder_started": -1}) for place in places]
    assert line.describe(asked.run_id).startswith("not started: the process holding its place in line went away")
    line.report_lost()
    assert told == ["lead"]


def test_a_waiter_that_cannot_start_leaves_its_spawn_lost_and_its_asker_told(monkeypatch):
    def cannot(self, run_id, spawn, asker):
        self._settle(run_id, "its waiter could not start: no such folder")
        raise OSError("no such folder")

    monkeypatch.setattr(SpawnLine, "_hold", lambda self, run_id, spawn, asker: None)
    line = _busy_line()
    asked = line.enter(_spawn())
    with line._places() as places:
        places[:] = [place.model_copy(update={"holder_started": -1}) for place in places]
    monkeypatch.setattr(SpawnLine, "_hold", cannot)
    assert line.revive() == []
    with line._places() as places:
        assert places[0].run_id == asked.run_id and places[0].state == "failed" and places[0].lost


def test_a_queued_spawn_survives_its_waiters_death_with_a_real_waiter_process(tmp_path):
    """The real waiter (`spawn_wait`) killed as a reboot kills it: the spawn is held by a new one."""
    import signal

    from galaius.host.processes import process_exited

    line = _busy_line()
    asked = line.enter(Spawn(task="x", cwd=str(tmp_path), agent="code-reviewer", session_id="s1"))
    first = next(place.holder for place in line._places_seen() if place.run_id == asked.run_id)
    assert first != os.getpid()
    try:
        os.kill(first, signal.SIGKILL)
        deadline = time.time() + 10
        while time.time() < deadline and not process_exited(first):
            time.sleep(0.05)
        assert line.report_lost() == [] and [place.state for place in line._places_seen()] == ["waiting"]
        assert line.revive() == [asked.run_id]
        second = next(place.holder for place in line._places_seen() if place.run_id == asked.run_id)
        assert second not in (first, os.getpid()) and not process_exited(second)
    finally:
        for place in line._places_seen():
            if place.holder_started is not None and place.holder != os.getpid():
                try:
                    os.kill(place.holder, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        with line._places() as places:
            places.clear()


#: The call sites that start a process inside a loop that repeats on a clock, each with the bound that holds it (row 3: `claude auth status`
#: for 25 logins every few seconds, 2026-11-21). A new site starts here or is refused by the next test.
def test_every_subprocess_started_inside_a_polling_loop_carries_a_rate_bound():
    root = Path(spawn_line.__file__).resolve().parents[1]
    unbounded = []
    for source in sorted(root.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for loop in (node for node in ast.walk(tree) if isinstance(node, ast.While)):
            if not (isinstance(loop.test, ast.Constant) and loop.test.value is True):
                continue
            calls = [node for node in ast.walk(loop) if isinstance(node, ast.Call) and _starts_process(node)]
            sleeps = [node for node in ast.walk(loop) if isinstance(node, ast.Call) and _is_sleep(node)]
            if calls and sleeps and not _bounded(source, loop):
                unbounded.append(f"{source.relative_to(root)}:{loop.lineno}")
    assert unbounded == [], f"a `while True` that sleeps between ticks and starts a process each tick, with no `# rate-bound:` line: {unbounded}"


def _starts_process(call: ast.Call) -> bool:
    function = call.func
    name = function.attr if isinstance(function, ast.Attribute) else function.id if isinstance(function, ast.Name) else ""
    owner = function.value.id if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name) else ""
    return (owner == "subprocess" and name in {"run", "Popen", "check_output", "check_call", "call"}) or name in {"start_participant"}


def _is_sleep(call: ast.Call) -> bool:
    function = call.func
    return isinstance(function, ast.Attribute) and function.attr == "sleep"


def _bounded(source: Path, loop: ast.While) -> bool:
    lines = source.read_text(encoding="utf-8").splitlines()[loop.lineno - 1:loop.end_lineno]
    return any("rate-bound:" in line for line in lines)


def test_a_login_status_command_runs_once_per_reuse_window_however_often_it_is_asked(monkeypatch):
    """The poll that started `claude auth status` for 25 logins every few seconds: a signed-in login is not asked again within
    `LOGIN_REUSE_SECONDS` (the web page polls every few seconds)."""
    import asyncio

    from galaius.agents.providers import PROVIDERS, LoginStatus

    provider, started = PROVIDERS["claude"], []

    async def status(env, *, timeout=10, named=True):
        started.append(env)
        return LoginStatus(signed_in=True, account="a", login="0123456789abcdef")

    monkeypatch.setattr(provider, "login_status", status)

    async def poll():
        return [await provider.authenticated({"CLAUDE_CONFIG_DIR": f"/logins/{n % 25}"}) for n in range(200)]

    assert all(asyncio.run(poll())) and len(started) == 25, "once per login, not once per poll"


# --- 3. a maker closing DONE on a slice that passed its critic and sits on no remote ---------------------------------------------

def _slice(tmp_path: Path, *, pushed: bool, verdict: str | None, critic_after_commit: bool = True) -> str:
    """A maker `maker` working in a clone, one commit made during its run; its child critic ended with `verdict` as its report
    (None: no critic ran); the commit pushed to the remote or not."""
    from tests.support import commit_all, init_repo, run_git

    remote = init_repo(tmp_path / "remote.git", bare=True)
    work = init_repo(tmp_path / "work")
    run_git(work, "remote", "add", "origin", str(remote))
    (work / "a.txt").write_text("a")
    commit_all(work, "slice")
    if pushed:
        run_git(work, "push", "-q", "origin", "main")
    made = float(run_git(work, "log", "-1", "--format=%ct").stdout.strip())
    register_run("maker", agent="backend-engineer", cwd=str(work), pid=None)
    reg._merge_record_locked("maker", {"started_at": made - 100})
    if verdict is not None:
        register_run("critic", agent="perf-critic", parent_run_id="maker", pid=None)
        _reported("critic", verdict)
        reg._merge_record_locked("critic", {"status": "done", "finished_at": made + 5 if critic_after_commit else made - 5})
    return str(work)


DONE_CLOSE = "DONE: fix merged and live.\n"


@pytest.mark.parametrize(("pushed", "verdict", "after", "refused"), [
    (False, "PASS: the perf review passes.", True, True),    # the 11-22 strand: passed, committed, on no remote
    (False, "ACCEPTED.", True, True),
    (True, "PASS: ok.", True, False),                        # delivered
    (False, "FAIL: p95 regressed.", True, False),            # not passed
    (False, None, True, False),                              # no critic ran
    (False, "PASS: ok.", False, False),                      # it judged an earlier commit
])
def test_a_done_close_on_a_slice_that_passed_its_critic_and_is_on_no_remote_is_refused(tmp_path, pushed, verdict, after, refused):
    from galaius.agents.turns import running_claim

    _slice(tmp_path, pushed=pushed, verdict=verdict, critic_after_commit=after)
    said = running_claim.refusal("maker", DONE_CLOSE)
    assert (said is not None) is refused, said
    if refused:
        assert "on no remote" in said and "critic" in said and "PARTIAL" in said


def test_the_stop_hook_blocks_that_close_and_stays_silent_on_a_delivered_one(tmp_path, monkeypatch, capsys):
    import io
    import sys

    from galaius import close_hook

    work = _slice(tmp_path, pushed=False, verdict="PASS: ok.")

    def stop(cwd: str) -> str:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"last_assistant_message": DONE_CLOSE, "cwd": cwd})))
        monkeypatch.setattr(sys, "argv", ["close_hook", "maker", str(tmp_path / "records")])
        (tmp_path / "records").mkdir(exist_ok=True)
        close_hook.main()
        return capsys.readouterr().out

    assert json.loads(stop(work))["decision"] == "block"
    from tests.support import run_git
    run_git(Path(work), "push", "-q", "origin", "main")
    assert stop(work) == ""
    assert stop(str(tmp_path)) == "", "a folder that is no repository costs no read of the records"
