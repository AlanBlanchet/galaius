"""The nested sandbox must survive a long session: a dead/exhausted X server is respawned
transparently, exited apps are reaped (no leak), a crash surfaces the app's own output, and a
`reset_sandbox` tool clears everything on demand (#10). Display-free — no real X server is started;
the genuine death/respawn was verified live.
"""

import os
import subprocess
import sys
import types
from unittest.mock import MagicMock

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

    def touch(self):
        self.touched = True

    def idle_seconds(self):
        return 0.0

    def is_recording_any(self):
        return False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_sandbox_global():
    srv.sandbox._sandbox = None
    _FakeNested.instances = []
    yield
    srv.sandbox._sandbox = None


def test_get_sandbox_respawns_a_dead_display(monkeypatch):
    monkeypatch.setattr("interact.desktop_backend.NestedBackend", _FakeNested)
    dead = _FakeNested(alive=False)
    srv.sandbox._sandbox = dead

    fresh = srv._get_sandbox()

    assert dead.closed is True, "the dead sandbox must be torn down"
    assert fresh is not dead and fresh.alive, "a fresh sandbox replaces it"
    assert srv.sandbox._sandbox is fresh


def test_get_sandbox_reuses_a_live_display(monkeypatch):
    monkeypatch.setattr("interact.desktop_backend.NestedBackend", _FakeNested)
    live = _FakeNested(alive=True)
    srv.sandbox._sandbox = live
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

    srv.sandbox._sandbox = S()
    msg = await srv.reset_sandbox()
    assert closed["v"] is True and srv.sandbox._sandbox is None
    assert "2 app" in msg


@pytest.mark.asyncio
async def test_reset_sandbox_when_none():
    srv.sandbox._sandbox = None
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


# --- empty-sandbox guidance: never let the agent bail to the real desktop ---
# A real session hit this: launch_app(device="phone") then screenshot(nested:aino) found an empty
# sandbox (the pre-#50/#53 respawn dropped the app); the message said "(none — launch_app first)" —
# which misled the agent (it HAD just launched) into driving the real desktop with DISPLAY=:0
# xdotool/import. The message must steer recovery INSIDE the sandbox and forbid the real desktop.


def _nested_backend_with(windows):
    backend = MagicMock()
    backend.list_windows.return_value = windows
    backend.screen_w, backend.screen_h = 412, 915
    return backend


def test_empty_sandbox_message_steers_recovery_and_forbids_the_real_desktop(monkeypatch):
    monkeypatch.setattr(srv.sandbox, "_get_sandbox", lambda *a, **k: _nested_backend_with([]))
    monkeypatch.setattr(srv.DesktopWindow, "find_in", classmethod(lambda cls, b, t: None))
    win, _, err = srv._resolve_nested_target("nested:aino")
    assert win is None
    low = err.lower()
    assert "launch_app" in err and "reset_sandbox" in err  # how to recover in-sandbox
    assert "do not" in low and ("real desktop" in low or "xdotool" in low or "display=:0" in low)
    assert "(none — launch_app first)" not in err  # the misleading bare line is gone


def test_nested_target_still_lists_windows_that_exist(monkeypatch):
    monkeypatch.setattr(srv.sandbox, "_get_sandbox", lambda *a, **k: _nested_backend_with([(1, "aino")]))
    monkeypatch.setattr(srv.DesktopWindow, "find_in", classmethod(lambda cls, b, t: None))
    _, _, err = srv._resolve_nested_target("nested:other")
    assert 'target="nested:aino"' in err  # a wrong title still shows what IS available
    assert "Do NOT" not in err  # the recovery warning is only for a genuinely empty sandbox


# --- the lingering-Xephyr fix: an IDLE sandbox is reaped like an idle browser session ----------


def _fake_sandbox(idle: float, recording: bool = False):
    class FakeSandbox:
        def is_alive(self):
            return True

        def idle_seconds(self):
            return idle

        def is_recording_any(self):
            return recording

    return FakeSandbox()


def test_idle_sandbox_is_reaped(monkeypatch):
    """The user kept finding agent-left Xephyr windows on his desktop: agents open the sandbox and
    never close it. Browser sessions already idle-reap (#36); the sandbox now does too — same TTL."""
    import interact.server as srv

    closed = []
    monkeypatch.setattr(srv.sandbox, "_sandbox", _fake_sandbox(idle=901.0))
    monkeypatch.setattr(srv.sandbox, "_close_sandbox", lambda: closed.append(True))
    srv._reap_sandbox(ttl=900)
    assert closed == [True]


def test_active_or_recording_sandbox_survives(monkeypatch):
    import interact.server as srv

    closed = []
    monkeypatch.setattr(srv.sandbox, "_close_sandbox", lambda: closed.append(True))
    monkeypatch.setattr(srv.sandbox, "_sandbox", _fake_sandbox(idle=10.0))
    srv._reap_sandbox(ttl=900)                     # recently used → kept
    monkeypatch.setattr(srv.sandbox, "_sandbox", _fake_sandbox(idle=99999.0, recording=True))
    srv._reap_sandbox(ttl=900)                     # mid-recording → NEVER reaped under the agent
    assert closed == []


def test_sandbox_touch_marks_use(monkeypatch):
    """Every _get_sandbox() attach/launch refreshes idleness, so an actively-driven sandbox
    never hits the TTL."""
    import time as _time

    from interact.desktop_backend import NestedBackend

    nb = NestedBackend.__new__(NestedBackend)
    nb.touch()
    assert nb.idle_seconds() < 1.0
    nb._last_used = _time.monotonic() - 500
    assert nb.idle_seconds() > 499


def test_sandbox_reaping_runs_even_with_browser_ttl_disabled(monkeypatch):
    """session_idle_ttl=0 (browser reaping off) must NOT silently disable sandbox reaping — each
    ttl gates only its own half."""
    import asyncio

    import interact.server as srv

    monkeypatch.setattr(srv.config, "sandbox_idle_ttl", 300)
    reaped = []
    monkeypatch.setattr(srv.sandbox, "_reap_sandbox", lambda ttl: reaped.append(ttl))

    async def fast(run_secs):
        orig_sleep = asyncio.sleep

        async def instant(_):
            await orig_sleep(0)

        monkeypatch.setattr(srv.asyncio, "sleep", instant)
        task = asyncio.ensure_future(srv._idle_session_reaper(0))
        for _ in range(10):
            await orig_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(fast(0))
    assert reaped and all(t == 300 for t in reaped)
