"""A turn this computer may not start now waits, said why, and starts once that clears with no click
(`galaius.agents.admission`, `SpawnLine`). 2026-10-13..20: 71 usage-limit cuts in bursts of ~10 runs, each run
starting or resuming after the first refusal refused too; 8 runs cut at once by a lost login; 29 kernel kills at
the agents' memory cap."""

import asyncio
import json
import subprocess
import sys
import threading
import time

import pytest

from galaius.agents import agent_queue, quota
from galaius.agents import registry as reg
from galaius.agents.admission import Hold, Paused, SoftMemory
from galaius.agents.events import RunCut
from galaius.agents.spawn_line import Admission, Spawn, SpawnLine
from tests.support import register_run

NOW = 1_800_000_000.0


def _refused(reset: float) -> str:
    """Claude Code's own rejected rate-limit line for the five-hour window, reopening at `reset`."""
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": int(reset)}})


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    """This process's `time.time`, moved by the test."""
    moment = [NOW]
    monkeypatch.setattr(time, "time", lambda: moment[0])
    return moment


def test_a_usage_limit_holds_every_turn_of_its_login_until_the_reset_and_lets_his_continue_try_alone(clock, tmp_path):
    reset = NOW + 3600
    quota.record_refusal("claude", "claude-opus-5-5", said=_refused(reset), now=NOW)
    line = SpawnLine(limit=6, waiting_flag_minutes=5, waiting_gap_seconds=60)
    cut, peer = register_run("cut", pid=None), register_run("peer", pid=None)
    other = register_run("other", pid=None, provider="codex")
    with line.resuming(cut) as admitted:
        assert not admitted
    said = line.waiting_said()["cut"]
    assert said.startswith("en pause : limite d'usage claude jusqu'à ") and said.endswith(", reprise sans clic")
    with line.resuming(other) as admitted:   # another provider keeps running
        assert admitted
    # His « Continuer » on two runs: the first asks the vendor alone, the second waits on its answer.
    for run_id in ("cut", "peer"):
        agent_queue.enqueue(run_id, message_id=f"m-{run_id}", sender=reg.PERSON_SENDER)
    with line.resuming(cut) as admitted:
        assert admitted
    clock[0] += 60
    trying = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])   # its turn at work, asking the vendor
    try:
        register_run("trying", pid=trying.pid)
        with line.resuming(peer) as admitted:
            assert not admitted
    finally:
        trying.kill()
        trying.wait()
    clock[0] = reset + quota.RESET_SLACK   # the reset: every turn starts, no click
    with line.resuming(peer) as admitted:
        assert admitted
    assert "peer" not in line.waiting_said()


def test_a_login_answering_before_the_named_reset_lifts_its_pause(clock):
    quota.record_refusal("claude", "claude-opus-5-5", said=_refused(NOW + 4 * 3600), now=NOW)
    assert Hold.of_login("claude", None) is not None
    quota.served("claude", "claude-sonnet-5", at=NOW + 600)   # another model of the same login answered
    clock[0] = NOW + 601
    assert Hold.of_login("claude", None) is None


def test_a_lost_login_holds_resumed_turns_until_it_is_signed_in_again(clock):
    line = SpawnLine(limit=6, waiting_flag_minutes=5, waiting_gap_seconds=60)
    lost = register_run("lost", pid=None)
    reg._remember_vendor_refusal(lost, "Failed to authenticate: OAuth token revoked. Please log in again", cut=RunCut.login_lost, now=NOW)
    with line.resuming(lost) as admitted:
        assert not admitted
    assert line.waiting_said()["lost"] == "en pause : connexion claude perdue, reprise sans clic une fois reconnectée sur ce PC"
    assert quota.lost_logins() == [("claude", None)]   # the runner asks whether it is signed in again
    agent_queue.enqueue("lost", message_id="m-lost", sender=reg.PERSON_SENDER)
    with line.resuming(lost) as admitted:   # his own message asks again (a PC without the runner): alone
        assert admitted
    agent_queue.enqueue("held", message_id="m-held", sender=reg.PERSON_SENDER)
    with line.resuming(lost) as first, line.resuming(register_run("held", pid=None)) as second:
        assert first and not second   # two of his turns on one login: the second waits on the first's answer
    agent_queue.cancel_pending_locked("lost")
    clock[0] += 300
    quota.login_back("claude")   # signed in again (the runner's check, or a spawn's login check)
    with line.resuming(lost) as admitted:
        assert admitted


def test_concurrent_pauses_and_lifts_are_all_kept():
    """Every process here pauses and lifts logins: none of their writes is lost."""
    def pause(index: int) -> None:
        quota.pause_login(f"p{index}", "login_lost", said="Please run /login")
    threads = [threading.Thread(target=pause, args=(index,)) for index in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(provider for provider, _ in quota.lost_logins()) == sorted(f"p{index}" for index in range(16))


def _cgroup(tmp_path, used_gb: float, cache_gb: float = 6.0, name: str = "slice"):
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    gb = 1_000_000_000
    (folder / "memory.max").write_text(f"{15 * gb}\n")
    (folder / "memory.current").write_text(f"{int((used_gb + cache_gb) * gb)}\n")
    (folder / "memory.stat").write_text(f"anon {int(used_gb * gb)}\ninactive_file {int(cache_gb * gb)}\nactive_file 0\n")
    return folder


@pytest.mark.parametrize(("steps", "held"), [
    ((9.0,), False),                 # 60 %: its page cache alone fills the cap, never a hold
    ((13.0,), True),                 # 87 %: past the soft limit
    ((13.0, 12.3), True),            # back to 82 %: still above the release share
    ((13.0, 11.7), False),           # 78 %: released
])
def test_past_the_soft_memory_limit_no_turn_starts_until_it_is_back_under_the_release_share(tmp_path, monkeypatch, steps, held):
    monkeypatch.setattr(SpawnLine, "_hold", lambda self, run_id, spawn, asker: None)
    memory = SoftMemory(soft_percent=85, release_percent=80, cgroup=_cgroup(tmp_path, 0))
    for used in steps:
        _cgroup(tmp_path, used)
        hold = memory.hold()
    assert (hold is not None) is held
    line = SpawnLine(limit=6, waiting_flag_minutes=5, waiting_gap_seconds=60, memory=memory)
    admission = line.enter(Spawn(task="review it", cwd=".", agent="code-reviewer", session_id="s1")) if held else None
    if held:
        assert admission.ahead == 0 and admission.sentence().startswith("en pause : mémoire des agents à ")


def test_the_memory_hold_names_the_largest_tenant_no_agent_run_holds(tmp_path):
    """Waiting frees nothing a browser venue or a shared server holds: the line says who holds it."""
    _cgroup(tmp_path, 13.0)
    _cgroup(tmp_path, 6.0, 0.5, "slice/galaius-run-ab12-cd34.scope")
    _cgroup(tmp_path, 3.1, 0.5, "slice/critic-venue.service")
    hold = SoftMemory(soft_percent=85, release_percent=80, cgroup=tmp_path / "slice").hold()
    assert "(dont critic-venue.service : 3.1 Go, hors agents)" in hold.line()


def test_a_spawn_every_login_of_which_is_paused_waits_and_starts_once_one_lifts(clock, monkeypatch):
    monkeypatch.setattr(SpawnLine, "_hold", lambda self, run_id, spawn, asker: None)
    reset = NOW + 1800
    quota.record_refusal("claude", "claude-opus-5-5", said=_refused(reset), now=NOW)
    started: list[str] = []

    async def runner(provider, task, *, run_id, **rest):
        if (hold := Hold.of_login("claude", None)) is not None:
            raise Paused((hold,))
        started.append(run_id)

    line = SpawnLine(limit=6, waiting_flag_minutes=5, waiting_gap_seconds=60)
    spawn = Spawn(task="review it", cwd=".", agent="code-reviewer", session_id="s1")
    admission = line.enter(spawn)
    launched = asyncio.run(line.launch(admission, spawn, runner))
    assert isinstance(launched, Admission) and launched.sentence().startswith("en pause : limite d'usage claude jusqu'à ")
    assert line.describe(admission.run_id[:8]) == launched.sentence() and started == []
    assert line._turn(admission.run_id) == "wait"
    clock[0] = reset + quota.RESET_SLACK
    monkeypatch.setattr("galaius.agents.spawn_line.run_agent", runner)
    line.wait(admission.run_id)
    assert started == [admission.run_id] and line.describe(admission.run_id[:8]) is None
