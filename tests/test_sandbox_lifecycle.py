"""The nested sandbox must survive a long session: a dead/exhausted X server is respawned
transparently, exited apps are reaped (no leak), a crash surfaces the app's own output, and a
`reset_sandbox` tool clears everything on demand (#10). Display-free — no real X server is started;
the genuine death/respawn was verified live.
"""

import os
import subprocess
import sys
import types

import pytest

from interact import server as srv
from interact.desktop_backend import NestedBackend

# Spawn real short-lived processes the cross-platform way — `sh`/`sleep` don't exist on Windows
# (the CI matrix runs macOS + Windows too), but the Python interpreter always does.
_EXIT0 = [sys.executable, "-c", ""]
_CRASH = [sys.executable, "-c", "import sys; sys.stderr.write('kaboom'); sys.exit(3)"]
_SLEEP = [sys.executable, "-c", "import time; time.sleep(0.3)"]


def _bare_backend() -> NestedBackend:
    """A NestedBackend without an X server — every X call is stubbed by the test."""
    nb = NestedBackend.__new__(NestedBackend)
    nb.env = {"DISPLAY": ":88"}
    nb.screen_w, nb.screen_h = 400, 400
    nb._procs = []
    nb._logs = {}
    nb._repaint_useless = set()
    nb._repaint_attempts = {}
    return nb


# --- is_alive: dead server, or a server that no longer answers, is not alive ---


def test_is_alive_false_when_server_exited(monkeypatch):
    nb = _bare_backend()
    nb._xserver = type("P", (), {"poll": lambda self: 1})()  # exited
    monkeypatch.setattr("interact.desktop_backend._x11_screen_size", lambda env: (400, 400))
    assert nb.is_alive() is False


def test_is_alive_false_when_display_unresponsive(monkeypatch):
    nb = _bare_backend()
    nb._xserver = type("P", (), {"poll": lambda self: None})()  # running...
    def boom(env):
        raise subprocess.CalledProcessError(1, "xdotool")
    monkeypatch.setattr("interact.desktop_backend._x11_screen_size", boom)  # ...but not answering
    assert nb.is_alive() is False


def test_is_alive_true_when_running_and_answering(monkeypatch):
    nb = _bare_backend()
    nb._xserver = type("P", (), {"poll": lambda self: None})()
    monkeypatch.setattr("interact.desktop_backend._x11_screen_size", lambda env: (400, 400))
    assert nb.is_alive() is True


# --- _get_sandbox: a dead sandbox is torn down and respawned; a live one is reused ---


class _FakeNested:
    instances: list["_FakeNested"] = []

    def __init__(self, *a, alive=True, **k):
        self.alive = alive
        self.closed = False
        # mirror NestedBackend.size (2nd positional arg) so _get_sandbox's size-change check works
        self.size = a[1] if len(a) > 1 else srv.config.nested_size
        _FakeNested.instances.append(self)

    def is_alive(self):
        return self.alive

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_sandbox_global():
    srv._sandbox = None
    _FakeNested.instances = []
    yield
    srv._sandbox = None


def test_get_sandbox_respawns_a_dead_display(monkeypatch):
    monkeypatch.setattr("interact.desktop_backend.NestedBackend", _FakeNested)
    dead = _FakeNested(alive=False)
    srv._sandbox = dead

    fresh = srv._get_sandbox()

    assert dead.closed is True, "the dead sandbox must be torn down"
    assert fresh is not dead and fresh.alive, "a fresh sandbox replaces it"
    assert srv._sandbox is fresh


def test_get_sandbox_reuses_a_live_display(monkeypatch):
    monkeypatch.setattr("interact.desktop_backend.NestedBackend", _FakeNested)
    live = _FakeNested(alive=True)
    srv._sandbox = live
    assert srv._get_sandbox() is live
    assert len(_FakeNested.instances) == 1, "no needless respawn of a healthy sandbox"


# --- reaping + crash diagnostics use real short-lived processes (no X needed) ---


def test_spawn_captures_output_readable_after_crash():
    nb = _bare_backend()
    proc = nb.spawn(_CRASH)
    proc.wait(timeout=5)
    assert proc.returncode == 3
    assert "kaboom" in nb.proc_output(proc)


def test_capture_reaps_exited_apps(monkeypatch):
    """capture() reaps apps that exited since the last spawn, so zombies don't accumulate between
    launches in a long session (#11)."""
    nb = _bare_backend()
    proc = nb.spawn(_EXIT0)
    proc.wait(timeout=5)
    monkeypatch.setattr(
        "interact.desktop_backend.subprocess.run",
        lambda *a, **k: types.SimpleNamespace(stdout=b"PNG"),
    )
    nb.capture()
    assert proc not in nb._procs, "capture() must reap an exited app (#11)"


def test_capture_video_grabs_nested_display_not_zero(monkeypatch):
    """record() on a sandbox window must x11grab the NESTED display (:N), not :0 — the bug that
    returned all-black frames for a nested window while screenshot() worked (#18)."""
    nb = _bare_backend()
    nb.env = {"DISPLAY": ":99"}
    monkeypatch.setattr(nb, "window_geometry", lambda name: (10, 20, 300, 400))
    monkeypatch.setattr(nb, "force_repaint", lambda name: True)
    captured = {}

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        with open(cmd[-1], "wb") as fh:
            fh.write(b"\x00\x00FAKEMP4")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("interact.desktop_backend.subprocess.run", fake_run)
    data = nb.capture_video("aino", duration=1, fps=5)
    cmd = captured["cmd"]
    grab = cmd[cmd.index("-i") + 1]
    assert grab == ":99+10,20", f"must grab the nested display+offset, got {grab!r}"
    assert data == b"\x00\x00FAKEMP4"


def test_reap_drops_exited_apps_and_unlinks_logs():
    import os

    nb = _bare_backend()
    proc = nb.spawn(_EXIT0)
    proc.wait(timeout=5)
    log = nb._logs[proc.pid]
    assert os.path.exists(log)
    nb.spawn(_SLEEP)  # a second spawn reaps the first (now-exited) proc
    assert proc not in nb._procs, "an exited app is reaped on the next spawn"
    assert not os.path.exists(log), "its captured-output log is unlinked"
    for p in nb._procs:
        p.terminate()


# --- reset_sandbox tool ---


@pytest.mark.asyncio
async def test_reset_sandbox_tears_down_and_clears_global():
    closed = {"v": False}

    class S:
        _procs = [object(), object()]

        def close(self):
            closed["v"] = True

    srv._sandbox = S()
    msg = await srv.reset_sandbox()
    assert closed["v"] is True and srv._sandbox is None
    assert "2 app" in msg


@pytest.mark.asyncio
async def test_reset_sandbox_when_none():
    srv._sandbox = None
    msg = await srv.reset_sandbox()
    assert "No sandbox" in msg


# --- sandbox forces software GL so a GPU app renders instead of capturing black (agent-friendly) ---


class _DummyProc:
    def poll(self):
        return None


def _construct_without_xserver(monkeypatch):
    """Run NestedBackend.__init__ without actually starting an X server."""
    monkeypatch.setattr("interact.desktop_backend.shutil.which", lambda _: "/usr/bin/Xephyr")
    monkeypatch.setattr("interact.desktop_backend.subprocess.Popen", lambda *a, **k: _DummyProc())
    monkeypatch.setattr(NestedBackend, "_open_log", staticmethod(lambda label: os.devnull))
    monkeypatch.setattr(NestedBackend, "_await_ready", lambda self, timeout: None)


def test_sandbox_forces_software_gl_by_default(monkeypatch):
    monkeypatch.delenv("LIBGL_ALWAYS_SOFTWARE", raising=False)
    _construct_without_xserver(monkeypatch)
    nb = NestedBackend(display=77)
    assert nb.env["LIBGL_ALWAYS_SOFTWARE"] == "1", "GPU apps must software-render or they capture black"


def test_sandbox_respects_explicit_gl_override(monkeypatch):
    monkeypatch.setenv("LIBGL_ALWAYS_SOFTWARE", "0")
    _construct_without_xserver(monkeypatch)
    nb = NestedBackend(display=77)
    assert nb.env["LIBGL_ALWAYS_SOFTWARE"] == "0", "an explicit global setting still wins"
