"""launch_app must not accumulate ghost app instances in the shared sandbox.

- #92: repeated ``launch_app`` calls left FOUR live ``bundle/aino`` processes on one display; a
  capture then composited a stray element from an OLDER instance over the current app, and a
  "fresh" relaunch only added a fifth.
- #87: one ``launch_app`` call produced TWO process trees (a client-side retry of a slow launch),
  after which ``target="nested:<title>"`` silently swapped between two same-titled windows and
  ~20 actions landed on the wrong one.
- #118: an Electron app's helpers ``setsid`` out of the launcher's process group, so a group kill
  left them running — the old window stayed mapped holding its profile socket while ``launch_app``
  reported "Replaced N app(s)" from the PRE-kill count, and the next launch opened nothing.

All cured at the same place: the sandbox owns its children as PROCESS GROUPS, sweeps its own
display for what escaped them, reports what it actually killed, a relaunch replaces the previous
app by default, and an identical still-running command is not spawned twice.
"""

import os
import subprocess
import sys
import time

import pytest

from interact.desktop import orphans
from interact.desktop.nested import NestedBackend

# The sandbox is Linux-only, and everything here asserts POSIX process-GROUP semantics
# (`os.getpgid`, `killpg`, `SIGKILL`). On Windows these names do not exist, so the module has to
# skip rather than fail — the CI leg that caught this is the only one that can (#24 tracks the
# Windows desktop backend).
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="sandbox process-group semantics are Linux-only"
)

SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


def _unused_display() -> str:
    """A display NOTHING on this machine serves or is bound to.

    The sweep under test kills every process bound to the display it is handed, and live sandboxes
    of real interact servers run beside this suite (an `Xephyr :99` is the normal state on the
    maintainer's box) — so the fixture's display must be one only this test's own children can
    ever carry, never a hardcoded ":99".
    """
    for number in NestedBackend._free_displays(7000):
        display = f":{number}"
        if not orphans.display_clients(display):
            return display
    pytest.skip("no free display number to confine the sweep to")


@pytest.fixture
def backend(tmp_path, monkeypatch):
    """A NestedBackend with no X server — only the child-process bookkeeping is under test."""
    be = NestedBackend.__new__(NestedBackend)
    be.display = _unused_display()
    be._procs = []
    be._cmds = {}
    be._logs = {}
    be.env = {"PATH": "/usr/bin:/bin", "DISPLAY": be.display}
    monkeypatch.setattr(NestedBackend, "_open_log", staticmethod(lambda label: str(tmp_path / f"{label}.log")))
    monkeypatch.setattr(NestedBackend, "_ensure_audio_sink", lambda self: None)
    yield be
    be.kill_apps()


@pytest.fixture
def owned_backend(backend):
    """The display-OWNING shape: our X server reports alive. That is what licenses the display
    sweep — a display whose server died may already belong to another interact server (#33), so a
    bare ``backend`` never sweeps."""
    backend._xserver = type("X", (), {"poll": lambda self: None})()
    return backend


def _app_with_child(marker, *, own_session: bool) -> list[str]:
    """A launched app that forks a longer-lived child and writes the child's pid to ``marker``.
    ``own_session`` makes the child ``setsid`` — the Chromium/Electron helper shape (#118), which
    leaves the launcher's process group AND session, where no group kill can follow."""
    return [sys.executable, "-c", (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        f"                         start_new_session={own_session!r})\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(30)\n"
    )]


def _pid_from(marker) -> int:
    for _ in range(100):
        if marker.exists() and marker.read_text():
            return int(marker.read_text())
        time.sleep(0.05)
    raise AssertionError("the launched app never wrote its child's pid")


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    except OSError:
        return False
    stat = out.stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _gone(pid: int, timeout: float = 1.0) -> bool:
    """Whether ``pid`` exits within ``timeout`` — a signal is asynchronous, so a single instant
    probe would report a process mid-exit as a survivor."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_a_spawned_app_leads_its_own_process_group(backend):
    """A ``uv run app`` / bundle launcher spawns CHILDREN; terminating only the direct child
    orphans them onto the display (the four ghost processes of #92). Its own session/group is
    what makes a group kill reach the whole tree."""
    proc = backend.spawn(SLEEPER)
    assert os.getpgid(proc.pid) == proc.pid


def test_kill_apps_stops_every_launched_app_and_reports_the_count(backend):
    backend.spawn(SLEEPER)
    backend.spawn(SLEEPER)
    report = backend.kill_apps()
    assert (report.killed, report.survivors) == (2, {})
    assert all(p.poll() is not None for p in backend._procs) or backend._procs == []


@pytest.mark.parametrize(
    "own_session", [False, True], ids=["grandchild_in_the_group", "helper_in_its_own_session"]
)
def test_kill_apps_reaches_the_whole_app_tree(owned_backend, tmp_path, own_session):
    """#92: the launched command forks a longer-lived child — a group kill must take the whole tree
    down, or the child keeps its window mapped on the display.

    #118: Chromium — so VS Code and every Electron app — ``setsid``s its helpers into their OWN
    session, where no group kill reaches. They kept the old window mapped and its profile socket
    open while launch_app reported the app "replaced", and the next launch was handed to the zombie
    and opened nothing. Only a sweep of everything still bound to the display reaches those."""
    marker = tmp_path / "child.pid"
    leader = owned_backend.spawn(_app_with_child(marker, own_session=own_session))
    child = _pid_from(marker)
    assert (os.getsid(child) != os.getsid(leader.pid)) is own_session
    report = owned_backend.kill_apps()
    assert _gone(child), "a child of the launched app survived kill_apps"
    assert leader.poll() is not None
    assert (report.killed, report.survivors) == (1, {})


def test_kill_apps_reports_survivors_instead_of_counting_them_replaced(owned_backend, tmp_path):
    """The other half of #118: ``launch_app(replace=True)`` said "Replaced N app(s)" from the
    PRE-kill count, so a survivor was reported as replaced. What a kill did NOT achieve is reported
    — the launched app by its executable, an escaped helper by its pid and executable — so the
    caller can say so instead of handing the agent a window that was never there."""
    marker = tmp_path / "child.pid"
    leader = owned_backend.spawn(_app_with_child(marker, own_session=True))
    helper = _pid_from(marker)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(NestedBackend, "_signal_tree", staticmethod(lambda proc, hard: None))  # no group kill lands
        mp.setattr(orphans, "_terminate", lambda pid: False)  # ...and the sweep cannot signal either
        mp.setattr(NestedBackend, "_KILL_GRACE_S", 0.1)
        report = owned_backend.kill_apps()
    assert report.killed == 0
    assert set(report.survivors) == {leader.pid, helper}
    assert all(command == os.path.basename(sys.executable) for command in report.survivors.values())
    assert _pid_alive(leader.pid) and _pid_alive(helper)


def test_the_sweep_spares_the_servers_own_helpers(owned_backend):
    """The display sweep reaches what ESCAPED the app's group — never the server's own handles. A
    live ffmpeg recorder, a `maim`/`xdotool` in flight: direct children of the server bound to the
    same display, each managed by the handle that spawned it. Sweeping them would cut a recording
    short or abort a concurrent capture on the way to killing the app."""
    recorder = subprocess.Popen(SLEEPER, env={**os.environ, "DISPLAY": owned_backend.display})
    try:
        owned_backend.spawn(SLEEPER)
        report = owned_backend.kill_apps()
        assert (report.killed, report.survivors) == (1, {})
        assert recorder.poll() is None, "the sweep killed a helper the server itself holds"
    finally:
        recorder.kill()
        recorder.wait()


def test_kill_apps_sweeps_a_detached_helper_after_its_leader_expires(owned_backend, tmp_path):
    """An app can exit after leaving a detached helper. Cleanup must still sweep the helper before
    the next launch, even though the tracked leader is no longer live."""
    marker = tmp_path / "expired-child.pid"
    leader = owned_backend.spawn([sys.executable, "-c", (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "start_new_session=True)\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
    )])
    helper = _pid_from(marker)
    leader.wait(timeout=5)
    report = owned_backend.kill_apps()
    assert _gone(helper)
    assert (report.killed, report.swept, report.survivors) == (0, 1, {})


def test_kill_apps_does_not_touch_a_process_on_a_foreign_display(owned_backend):
    """The sweep is bound to this backend's display, not a global process hunt."""
    base = int(owned_backend.display.lstrip(":")) + 1
    foreign_display = next(
        f":{number}" for number in NestedBackend._free_displays(base)
        if f":{number}" != owned_backend.display
    )
    foreign = subprocess.Popen(
        SLEEPER,
        env={**os.environ, "DISPLAY": foreign_display},
        start_new_session=True,
    )
    try:
        report = owned_backend.kill_apps()
        assert report.survivors == {}
        assert foreign.poll() is None
    finally:
        foreign.terminate()
        foreign.wait(timeout=5)


def test_running_command_finds_an_identical_live_launch(backend):
    """#87: the guard that turns a duplicate launch into a no-op instead of a second window."""
    backend.spawn(SLEEPER)
    assert backend.running_command(SLEEPER) is not None
    assert backend.running_command([sys.executable, "-c", "pass"]) is None


def test_running_command_ignores_an_exited_launch(backend):
    backend.spawn([sys.executable, "-c", "pass"])
    time.sleep(0.5)
    assert backend.running_command([sys.executable, "-c", "pass"]) is None


def test_spawn_layers_a_launch_env_over_the_sandbox_pins(backend):
    """`FOO=bar app` (#117): the caller's variables reach the child with the sandbox's own DISPLAY
    pin still underneath them — the launch env is merged OVER the sandbox's, never swapped for it."""
    proc = backend.spawn(["sh", "-c", "echo $FOO $DISPLAY"], env={"FOO": "bar"})
    assert proc.wait(timeout=10) == 0
    assert backend.proc_output(proc).split() == ["bar", backend.display]

