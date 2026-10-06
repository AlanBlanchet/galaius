"""Every agent interact starts shares one capped slice, so a swarm cannot take the desktop down, and
sits in its own capped scope, so one run's memory cannot end another."""

import os
import shutil
import subprocess
import sys

import pytest
from pydantic import ValidationError

from interact.agents import ceiling as ceiling_module
from interact.agents.ceiling import SLICE, Ceiling, contained, end_run_scope

CEILING = Ceiling(memory_max_percent=45, run_memory_max_percent=20, oomd_pressure_percent=80, cpu_percent=75, cpu_weight=20)


def test_the_slice_has_a_hard_cap_and_no_throttle_below_it():
    """A throttle below the cap stalls every agent long before memory is short; oomd then kills a run."""
    assert CEILING.properties(cores=12) == {
        "MemoryHigh": "infinity",
        "MemoryMax": "45%",
        "MemorySwapMax": "0",
        "CPUQuota": "900%",
        "CPUWeight": "20",
        "ManagedOOMMemoryPressure": "kill",
        "ManagedOOMMemoryPressureLimit": "80%",
    }


def test_one_run_cannot_exceed_all_agents_together():
    with pytest.raises(ValidationError):
        Ceiling(memory_max_percent=45, run_memory_max_percent=50, oomd_pressure_percent=80, cpu_percent=75, cpu_weight=20)


@pytest.mark.parametrize("missing", ["systemd-run", "systemctl"])
def test_without_user_systemd_the_agent_runs_unchanged(monkeypatch, missing):
    real = shutil.which
    monkeypatch.setattr(ceiling_module.shutil, "which", lambda name: None if name == missing else real(name))
    assert contained(["claude", "-p", "x"], CEILING) == ["claude", "-p", "x"]


@pytest.mark.parametrize("failure, scoped", [
    pytest.param(subprocess.TimeoutExpired("systemctl", 5), True, id="slow_manager_still_scopes"),
    pytest.param(subprocess.CalledProcessError(1, "systemctl"), False, id="no_manager_runs_uncapped"),
])
def test_a_slice_that_cannot_be_prepared(monkeypatch, failure, scoped):
    """Slow: the run still gets its own scope (unscoped, it would die with its launcher's). No
    reachable user manager: systemd-run would fail too, so the run starts uncapped."""
    monkeypatch.setattr(ceiling_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ceiling_module.sys, "platform", "linux")

    def _refuse(*args, **kwargs):
        raise failure

    monkeypatch.setattr(ceiling_module.subprocess, "run", _refuse)
    argv = contained(["claude"], CEILING)
    if scoped:
        assert argv[:3] == ["/usr/bin/systemd-run", "--user", "--scope"] and argv[-2:] == ["--", "claude"]
        assert "--property=OOMPolicy=continue" in argv and "--property=MemoryMax=20%" in argv
    else:
        assert argv == ["claude"]


@pytest.mark.parametrize("unit, stopped", [
    pytest.param("interact-run-run-1-0a1b2c3d.scope", True, id="own_scope"),
    pytest.param("interact-run-parent-9-0a1b2c3d.scope", False, id="launchers_scope"),
    pytest.param("interact-run-run-10-0a1b2c3d.scope", False, id="longer_id_sharing_a_prefix"),
    pytest.param("app-com.microsoft.VSCode-1.scope", False, id="editor_scope"),
    pytest.param(None, False, id="no_unit"),
])
def test_an_ended_run_stops_only_its_own_scope(monkeypatch, unit, stopped):
    """What the agent left running is ended; the editor or a parent run holding it never is."""
    monkeypatch.setattr(ceiling_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[list[str]] = []
    monkeypatch.setattr(ceiling_module.subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    assert end_run_scope(unit, "run-1") is stopped
    assert calls == ([["/usr/bin/systemctl", "--user", "stop", unit]] if stopped else [])


def _user_manager() -> bool:
    if not sys.platform.startswith("linux") or shutil.which("systemd-run") is None:
        return False
    probe = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True)
    return probe.stdout.strip() in {"running", "degraded"}


@pytest.mark.skipif(not _user_manager(), reason="needs a systemd user manager")
def test_a_contained_agent_keeps_its_pid_and_lands_in_its_own_capped_scope():
    process = subprocess.Popen(
        contained([sys.executable, "-c", "import os; print(os.getpid()); print(open('/proc/self/cgroup').read())"], CEILING, run_id="probe-1"),
        stdout=subprocess.PIPE, text=True,
    )
    out, _ = process.communicate(timeout=30)
    pid, cgroup = out.split("\n", 1)
    assert int(pid) == process.pid
    assert f"/{SLICE}/interact-run-probe-1-" in cgroup
    limits = subprocess.run(
        ["systemctl", "--user", "show", SLICE, "-p", "MemoryHigh", "-p", "MemorySwapMax", "-p", "CPUWeight"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "MemoryHigh=infinity" in limits and "MemorySwapMax=0" in limits and "CPUWeight=20" in limits


@pytest.mark.asyncio
async def test_every_launched_agent_goes_through_the_ceiling(tmp_path, monkeypatch):
    """The one thing that must never regress: a spawn path that bypasses the slice."""
    import asyncio

    from interact.agents import run as run_module
    from tests.support.agents import install_provider, use_policy
    from tests.test_agent_run import _FakeProvider

    use_policy(monkeypatch, agents={"tester": "fixture-model"}, reasoning={"tester": "medium"})
    install_provider(monkeypatch, _FakeProvider())
    wrapped: list[list[str]] = []
    monkeypatch.setattr(run_module, "contained", lambda argv, **named: wrapped.append(list(argv)) or list(argv))
    run = await run_module.run_agent(_FakeProvider(), "t", agent="tester", name="w", cwd=str(tmp_path))
    await asyncio.wait_for(run.wait(), timeout=30)

    assert wrapped and wrapped[0][0] == sys.executable
