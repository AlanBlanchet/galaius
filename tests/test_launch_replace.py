"""launch_app must not accumulate ghost app instances in the shared sandbox.

- #92: repeated ``launch_app`` calls left FOUR live ``bundle/aino`` processes on one display; a
  capture then composited a stray element from an OLDER instance over the current app, and a
  "fresh" relaunch only added a fifth.
- #87: one ``launch_app`` call produced TWO process trees (a client-side retry of a slow launch),
  after which ``target="nested:<title>"`` silently swapped between two same-titled windows and
  ~20 actions landed on the wrong one.

Both are cured at the same place: the sandbox owns its children as PROCESS GROUPS, a relaunch
replaces the previous app by default, and an identical still-running command is not spawned twice.
"""

import subprocess
import sys
import time

import pytest

from interact.desktop.nested import NestedBackend

# The sandbox is Linux-only, and everything here asserts POSIX process-GROUP semantics
# (`os.getpgid`, `killpg`, `SIGKILL`). On Windows these names do not exist, so the module has to
# skip rather than fail — the CI leg that caught this is the only one that can (#24 tracks the
# Windows desktop backend).
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="sandbox process-group semantics are Linux-only"
)


@pytest.fixture
def backend(tmp_path, monkeypatch):
    """A NestedBackend with no X server — only the child-process bookkeeping is under test."""
    be = NestedBackend.__new__(NestedBackend)
    be._procs = []
    be._cmds = {}
    be._logs = {}
    be.env = {**{"PATH": "/usr/bin:/bin"}, "DISPLAY": ":99"}
    monkeypatch.setattr(NestedBackend, "_open_log", staticmethod(lambda label: str(tmp_path / f"{label}.log")))
    monkeypatch.setattr(NestedBackend, "_ensure_audio_sink", lambda self: None)
    yield be
    be.kill_apps()


SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


def test_a_spawned_app_leads_its_own_process_group(backend):
    """A ``uv run app`` / bundle launcher spawns CHILDREN; terminating only the direct child
    orphans them onto the display (the four ghost processes of #92). Its own session/group is
    what makes a group kill reach the whole tree."""
    import os

    proc = backend.spawn(SLEEPER)
    assert os.getpgid(proc.pid) == proc.pid


def test_kill_apps_stops_every_launched_app_and_reports_the_count(backend):
    backend.spawn(SLEEPER)
    backend.spawn(SLEEPER)
    assert backend.kill_apps() == 2
    time.sleep(0.3)
    assert all(p.poll() is not None for p in backend._procs) or backend._procs == []


def test_kill_apps_reaches_a_grandchild(backend, tmp_path):
    """The real #92 shape: the launched command forks a longer-lived child. A group kill must take
    the whole tree down, or the grandchild keeps its window mapped on the display."""
    marker = tmp_path / "grandchild.pid"
    script = (
        "import os, subprocess, sys, time\n"
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(marker)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(30)\n"
    )
    backend.spawn([sys.executable, "-c", script])
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.1)
    grandchild = int(marker.read_text())
    backend.kill_apps()
    time.sleep(0.5)
    assert not _pid_alive(grandchild), "a grandchild survived the sandbox teardown"


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    except OSError:
        return False
    stat = out.stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def test_running_command_finds_an_identical_live_launch(backend):
    """#87: the guard that turns a duplicate launch into a no-op instead of a second window."""
    backend.spawn(SLEEPER)
    assert backend.running_command(SLEEPER) is not None
    assert backend.running_command([sys.executable, "-c", "pass"]) is None


def test_running_command_ignores_an_exited_launch(backend):
    backend.spawn([sys.executable, "-c", "pass"])
    time.sleep(0.5)
    assert backend.running_command([sys.executable, "-c", "pass"]) is None
