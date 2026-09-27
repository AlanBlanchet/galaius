"""Every agent interact starts shares one capped slice, so a swarm cannot take the desktop down."""

import os
import shutil
import subprocess
import sys

import pytest
from pydantic import ValidationError

from interact.agents import ceiling as ceiling_module
from interact.agents.ceiling import SLICE, Ceiling, contained

CEILING = Ceiling(memory_high_percent=40, memory_max_percent=50, cpu_percent=75, cpu_weight=20)


def test_the_ceiling_caps_memory_without_swap_and_cpu_across_all_cores():
    assert CEILING.properties(cores=12) == {
        "MemoryHigh": "40%",
        "MemoryMax": "50%",
        "MemorySwapMax": "0",
        "CPUQuota": "900%",
        "CPUWeight": "20",
        "ManagedOOMMemoryPressure": "kill",
    }


def test_throttling_starts_before_the_kill_limit():
    with pytest.raises(ValidationError):
        Ceiling(memory_high_percent=60, memory_max_percent=50, cpu_percent=75, cpu_weight=20)


@pytest.mark.parametrize("missing", ["systemd-run", "systemctl"])
def test_without_user_systemd_the_agent_runs_unchanged(monkeypatch, missing):
    real = shutil.which
    monkeypatch.setattr(ceiling_module.shutil, "which", lambda name: None if name == missing else real(name))
    assert contained(["claude", "-p", "x"], CEILING) == ["claude", "-p", "x"]


def test_a_slice_that_cannot_be_prepared_leaves_the_agent_running(monkeypatch):
    monkeypatch.setattr(ceiling_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ceiling_module.sys, "platform", "linux")

    def _refuse(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(ceiling_module.subprocess, "run", _refuse)
    assert contained(["claude"], CEILING) == ["claude"]


def _user_manager() -> bool:
    if not sys.platform.startswith("linux") or shutil.which("systemd-run") is None:
        return False
    probe = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True)
    return probe.stdout.strip() in {"running", "degraded"}


@pytest.mark.skipif(not _user_manager(), reason="needs a systemd user manager")
def test_a_contained_agent_keeps_its_pid_and_lands_in_the_capped_slice():
    process = subprocess.Popen(
        contained([sys.executable, "-c", "import os; print(os.getpid()); print(open('/proc/self/cgroup').read())"], CEILING),
        stdout=subprocess.PIPE, text=True,
    )
    out, _ = process.communicate(timeout=30)
    pid, cgroup = out.split("\n", 1)
    assert int(pid) == process.pid
    assert f"/{SLICE}/" in cgroup
    limits = subprocess.run(
        ["systemctl", "--user", "show", SLICE, "-p", "MemorySwapMax", "-p", "CPUWeight"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "MemorySwapMax=0" in limits and "CPUWeight=20" in limits


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
    monkeypatch.setattr(run_module, "contained", lambda argv: wrapped.append(list(argv)) or list(argv))
    run = await run_module.run_agent(_FakeProvider(), "t", agent="tester", name="w", cwd=str(tmp_path))
    await asyncio.wait_for(run.wait(), timeout=30)

    assert wrapped and wrapped[0][0] == sys.executable
