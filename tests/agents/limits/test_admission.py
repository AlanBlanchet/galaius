"""A turn this computer may not start now waits, said why, and starts once that clears with no click
(`galaius.agents.limits.admission`, `SpawnLine`). 2026-10-13..20: 71 usage-limit cuts in bursts of ~10 runs, each run
starting or resuming after the first refusal refused too; 8 runs cut at once by a lost login; 29 kernel kills at
the agents' memory cap."""

import asyncio
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import pytest

from galaius.agents import agent_queue
from galaius.agents.limits import quota
from galaius.agents import registry as reg
from galaius.agents.limits.admission import Hold, Paused, SoftMemory
from galaius.agents.transport.events import RunCut
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


@pytest.mark.parametrize(("days", "said"), [(0, r"jusqu'à \d\d:\d\d$"), (1, r"jusqu'à demain \d\d:\d\d$"), (6, r"jusqu'à \d\d/\d\d \d\d:\d\d$")])
def test_a_hold_line_names_the_day_its_window_reopens_past_today(days, said):
    """A weekly window reopens days later: « jusqu'à 20:01 » alone read as tonight (2026-10-31)."""
    until = (datetime.now().replace(hour=12, minute=0) + timedelta(days=days)).timestamp()
    hold = Hold(cause=quota.Pause(reason="usage_limit", at=time.time(), until=until), provider="claude")
    assert re.search(said, hold.line(promise=False))


def _meminfo(tmp_path, available_gb: float, total_gb: float = 32.0):
    path = tmp_path / "meminfo"
    path.write_text(f"MemTotal:       {int(total_gb * 1e6)} kB\nMemFree:         1000 kB\nMemAvailable:   {int(available_gb * 1e6)} kB\n")
    return path


@pytest.mark.parametrize(("steps", "held"), [
    ((6.0,), False),          # 19 % free: past the 15 % floor
    ((4.0,), True),           # 12.5 %: short, the agents' own share low (2026-11-09: a container stack held the rest)
    ((4.0, 5.5), True),       # 17 %: back over the floor, still under the 20 % it waits for
    ((4.0, 6.6), False),      # 21 %: released
])
def test_the_pc_short_of_free_memory_holds_every_turn_whatever_the_agents_hold(tmp_path, steps, held):
    memory = SoftMemory(soft_percent=85, release_percent=80, cgroup=_cgroup(tmp_path, 2.0),
                        pc_min_percent=15, pc_release_percent=20, meminfo=tmp_path / "meminfo")
    for available in steps:
        _meminfo(tmp_path, available)
        hold = memory.hold()
    assert (hold is not None) is held
    if held:
        assert hold.line() == f"en pause : mémoire du PC à {steps[-1] * 1.024:.1f} Go libres, reprise au-dessus de {32 * 1.024 * 0.2:.1f} Go"


def test_no_meminfo_holds_nothing_and_a_floor_of_zero_reads_none(tmp_path):
    assert SoftMemory(soft_percent=85, release_percent=80, cgroup=_cgroup(tmp_path, 2.0), pc_min_percent=15,
                      pc_release_percent=20, meminfo=None).hold() is None
    _meminfo(tmp_path, 0.5)
    assert SoftMemory(soft_percent=85, release_percent=80, cgroup=_cgroup(tmp_path, 2.0),
                      meminfo=tmp_path / "meminfo").hold() is None


def _pressure(tmp_path, avg10: float, avg60: float):
    path = tmp_path / "pressure"
    path.write_text(f"some avg10={avg10 * 2:.2f} avg60={avg60 * 2:.2f} avg300=0.00 total=1\n"
                    f"full avg10={avg10:.2f} avg60={avg60:.2f} avg300=0.00 total=1\n")
    return path


@pytest.mark.parametrize(("steps", "held"), [
    (((0.5, 0.2),), False),                # stalls under the 5 % it holds at
    (((12.0, 4.0),), True),                # 12 % of the last 10 s: held, free memory and the slice both fine (2026-11-16)
    (((12.0, 4.0), (0.0, 2.0)), True),     # the last 10 s calm, the last minute still over the 1 % it waits under
    (((12.0, 4.0), (0.0, 0.6)), False),    # released
])
def test_the_pc_stalling_on_memory_holds_every_turn_whatever_its_free_memory(tmp_path, steps, held):
    _meminfo(tmp_path, 10.0)
    memory = SoftMemory(soft_percent=85, release_percent=80, cgroup=_cgroup(tmp_path, 2.0), pc_min_percent=15,
                        pc_release_percent=20, meminfo=tmp_path / "meminfo", stall_percent=5, stall_release_percent=1,
                        pressure=tmp_path / "pressure")
    for avg10, avg60 in steps:
        _pressure(tmp_path, avg10, avg60)
        hold = memory.hold()
    assert (hold is not None) is held
    if held:
        assert hold.line() == f"en pause : le PC attend sa mémoire {max(steps[-1]):.0f} % du temps, reprise sous 1 %"


def test_no_pressure_file_or_a_stall_limit_of_zero_holds_nothing(tmp_path):
    _pressure(tmp_path, 90.0, 90.0)
    assert SoftMemory(soft_percent=85, release_percent=80, cgroup=_cgroup(tmp_path, 2.0), stall_percent=5,
                      stall_release_percent=1, pressure=None).hold() is None
    assert SoftMemory(soft_percent=85, release_percent=80, cgroup=_cgroup(tmp_path, 2.0),
                      pressure=tmp_path / "pressure").hold() is None


def _throttled(tmp_path, used_gb: float, high_gb: float | None, group_stall: float | None = None):
    """The slice's cgroup with a 15 GB `MemoryMax`, its soft limit `MemoryHigh` (None: `max`) and its own memory pressure."""
    folder = _cgroup(tmp_path, used_gb, cache_gb=0.5)
    (folder / "memory.high").write_text("max\n" if high_gb is None else f"{int(high_gb * 1_000_000_000)}\n")
    if group_stall is not None:
        (folder / "memory.pressure").write_text(f"some avg10={group_stall * 2:.2f} avg60=0.00 avg300=0.00 total=1\n"
                                                f"full avg10={group_stall:.2f} avg60=0.00 avg300=0.00 total=1\n")
    return folder


@pytest.mark.parametrize(("used_gb", "high_gb", "held"), [
    (9.6, 9.35, True),    # 2026-11-24: 64 % of the 15 GB cap, 103 % of the 9.35 GB the kernel throttles at: held, the tenants named
    (9.6, None, False),   # no soft limit: 64 % of the cap, a turn starts
    (6.0, 9.35, False),   # 64 % of the throttle: starts
])
def test_the_cap_is_the_lower_of_the_slices_hard_and_soft_limits(tmp_path, used_gb, high_gb, held):
    _throttled(tmp_path, used_gb, high_gb)
    _cgroup(tmp_path, 2.9, 0.1, "slice/critic-venue-limit-tap44.service")
    hold = SoftMemory(soft_percent=85, release_percent=80, cgroup=tmp_path / "slice").hold()
    assert (hold is not None) is held
    if held:
        assert "critic-venue-limit-tap44.service : 2.9 Go, hors agents" in hold.line()


@pytest.mark.parametrize(("group_stall", "held"), [
    (42.0, False),   # the slice's own tasks stall 42 %: the throttle makes that pressure, waiting for it to clear never ends
    (0.5, True),     # the slice calm: the PC's other programs stall it, waiting is right
])
def test_a_stall_the_agents_make_themselves_holds_no_turn(tmp_path, group_stall, held):
    _throttled(tmp_path, 2.0, 9.35, group_stall)
    _pressure(tmp_path, 12.0, 12.0)
    memory = SoftMemory(soft_percent=85, release_percent=80, cgroup=tmp_path / "slice", stall_percent=5, stall_release_percent=1,
                        pressure=tmp_path / "pressure")
    assert (memory.hold() is not None) is held
