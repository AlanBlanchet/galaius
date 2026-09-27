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
from interact.desktop import NestedBackend
from interact.server import sandbox as _sandbox_mod, targets as _targets_mod
from tests.support.desktop import bare_nested_backend

# Spawn real short-lived processes the cross-platform way — `sh`/`sleep` don't exist on Windows
# (the CI matrix runs macOS + Windows too), but the Python interpreter always does.
_EXIT0 = [sys.executable, "-c", ""]
_CRASH = [sys.executable, "-c", "import sys; sys.stderr.write('kaboom'); sys.exit(3)"]
_SLEEP = [sys.executable, "-c", "import time; time.sleep(0.3)"]


# --- is_alive: dead server, or a server that no longer answers, is not alive ---


def test_is_alive_false_when_server_exited(monkeypatch):
    nb = bare_nested_backend()
    nb._xserver = type("P", (), {"poll": lambda self: 1})()  # exited
    monkeypatch.setattr("interact.desktop.nested._x11_screen_size", lambda env: (400, 400))
    assert nb.is_alive() is False


def test_is_alive_false_when_display_unresponsive(monkeypatch):
    nb = bare_nested_backend()
    nb._xserver = type("P", (), {"poll": lambda self: None})()  # running...
    def boom(env):
        raise subprocess.CalledProcessError(1, "xdotool")
    monkeypatch.setattr("interact.desktop.nested._x11_screen_size", boom)  # ...but not answering
    assert nb.is_alive() is False


def test_is_alive_true_when_running_and_answering(monkeypatch):
    nb = bare_nested_backend()
    nb._xserver = type("P", (), {"poll": lambda self: None})()
    monkeypatch.setattr("interact.desktop.nested._x11_screen_size", lambda env: (400, 400))
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


def test_free_displays_skips_taken(monkeypatch):
    """A display is taken if its X lock or socket exists; _free_displays returns free ones from the
    preferred number up, so concurrent sandboxes don't collide on :99 (#33)."""
    import interact.desktop.nested as db

    taken = {"/tmp/.X99-lock", "/tmp/.X11-unix/X100", "/tmp/.X101-lock"}
    monkeypatch.setattr(db.os.path, "exists", lambda p: p in taken)
    got = db.NestedBackend._free_displays(99)
    assert got[0] == 102  # first free at/above 99 (99,100,101 taken)
    assert all(n not in got for n in (99, 100, 101))


def test_get_sandbox_respawns_a_dead_display(monkeypatch):
    monkeypatch.setattr("interact.desktop.NestedBackend", _FakeNested)
    dead = _FakeNested(alive=False)
    srv.sandbox._sandbox = dead

    fresh = srv._get_sandbox()

    assert dead.closed is True, "the dead sandbox must be torn down"
    assert fresh is not dead and fresh.alive, "a fresh sandbox replaces it"
    assert srv.sandbox._sandbox is fresh


def test_get_sandbox_reuses_a_live_display(monkeypatch):
    monkeypatch.setattr("interact.desktop.NestedBackend", _FakeNested)
    live = _FakeNested(alive=True)
    srv.sandbox._sandbox = live
    assert srv._get_sandbox() is live
    assert len(_FakeNested.instances) == 1, "no needless respawn of a healthy sandbox"


# --- self-heal carries its reason forward, and a caller can tell its sandbox was replaced (#141/#159) ---


def test_get_sandbox_respawn_records_the_death_reason(monkeypatch):
    """`_get_sandbox`'s self-heal must not silently swap in a fresh sandbox — the next caller
    (targets._sandbox_death_diagnostics) needs to say WHY the previous one vanished (#141)."""
    monkeypatch.setattr("interact.desktop.NestedBackend", _FakeNested)
    dead = _FakeNested(alive=False)
    dead.display_health = lambda: "The sandbox Xephyr :99 is DOWN (SIGKILL — likely OOM-killed)"
    srv.sandbox._sandbox = dead

    srv._get_sandbox()

    assert srv.sandbox.last_replace_reason() == (
        "The sandbox Xephyr :99 is DOWN (SIGKILL — likely OOM-killed)"
    )


def test_get_sandbox_respawn_without_display_health_still_records_a_reason(monkeypatch):
    """A backend with no `display_health` (the reservation/#159 fake used elsewhere) must still
    get SOME reason recorded — never a silent respawn."""
    monkeypatch.setattr("interact.desktop.NestedBackend", _FakeNested)
    dead = _FakeNested(alive=False)
    srv.sandbox._sandbox = dead

    srv._get_sandbox()

    assert srv.sandbox.last_replace_reason()


def test_reservation_replaced_reason_none_while_still_the_live_sandbox(monkeypatch):
    monkeypatch.setattr("interact.desktop.NestedBackend", _FakeNested)
    reservation = srv.sandbox.reserve_sandbox()
    assert reservation.replaced_reason() is None


def test_reservation_replaced_reason_names_the_resize_that_replaced_it(monkeypatch):
    """#159: caller A reserves the sandbox; caller B's launch_app asks for a different explicit
    size, which respawns the singleton. A's reservation must now say it was replaced, and why —
    never hand back stale state with no explanation."""
    monkeypatch.setattr("interact.desktop.NestedBackend", _FakeNested)
    reservation = srv.sandbox.reserve_sandbox(size="640x480")

    srv._get_sandbox(size="800x600")  # a different caller, explicit new size → respawns it

    reason = reservation.replaced_reason()
    assert reason is not None
    assert "640x480" in reason and "800x600" in reason


def test_reservation_replaced_reason_after_death_respawn(monkeypatch):
    """#141 + #159 together: a reservation holder whose sandbox died and self-healed under it (via
    someone else's call) can ask why its handle is now stale."""
    monkeypatch.setattr("interact.desktop.NestedBackend", _FakeNested)
    reservation = srv.sandbox.reserve_sandbox()
    srv.sandbox._sandbox.alive = False  # the X server dies after the reservation was taken

    srv._get_sandbox()  # another caller's attach self-heals it

    reason = reservation.replaced_reason()
    assert reason is not None and "stopped answering" in reason


# --- reaping + crash diagnostics use real short-lived processes (no X needed) ---


def test_spawn_captures_output_readable_after_crash():
    nb = bare_nested_backend()
    proc = nb.spawn(_CRASH)
    proc.wait(timeout=5)
    assert proc.returncode == 3
    assert "kaboom" in nb.proc_output(proc)


def test_capture_reaps_exited_apps(monkeypatch):
    """capture() reaps apps that exited since the last spawn, so zombies don't accumulate between
    launches in a long session (#11)."""
    nb = bare_nested_backend()
    proc = nb.spawn(_EXIT0)
    proc.wait(timeout=5)
    monkeypatch.setattr(
        "interact.desktop.nested.subprocess.run",
        lambda *a, **k: types.SimpleNamespace(stdout=b"PNG"),
    )
    nb.capture()
    assert proc not in nb._procs, "capture() must reap an exited app (#11)"


def test_capture_video_grabs_nested_display_not_zero(monkeypatch):
    """record() on a sandbox window must x11grab the NESTED display (:N), not :0 — the bug that
    returned all-black frames for a nested window while screenshot() worked (#18)."""
    nb = bare_nested_backend()
    nb.env = {"DISPLAY": ":99"}
    monkeypatch.setattr(nb, "window_geometry", lambda name: (10, 20, 300, 400))
    monkeypatch.setattr(nb, "force_repaint", lambda name: True)
    captured = {}

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        with open(cmd[-1], "wb") as fh:
            fh.write(b"\x00\x00FAKEMP4")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("interact.desktop.nested.subprocess.run", fake_run)
    data = nb.capture_video("aino", duration=1, fps=5)
    cmd = captured["cmd"]
    grab = cmd[cmd.index("-i") + 1]
    assert grab == ":99+10,20", f"must grab the nested display+offset, got {grab!r}"
    assert data == b"\x00\x00FAKEMP4"


def test_reap_drops_exited_apps_and_unlinks_logs():
    import os

    nb = bare_nested_backend()
    proc = nb.spawn(_EXIT0)
    proc.wait(timeout=5)
    log = nb._logs[proc.pid]
    assert os.path.exists(log)
    nb.spawn(_SLEEP)  # a second spawn reaps the first (now-exited) proc
    assert proc not in nb._procs, "an exited app is reaped on the next spawn"
    assert not os.path.exists(log), "its captured-output log is unlinked"
    for p in nb._procs:
        p.terminate()


def test_close_kills_through_the_sweeps_and_forgets_its_apps(monkeypatch):
    """#118: `close` was the only path that reached an app's helpers OUTSIDE its process group (the
    display + profile sweeps) — it must share ONE kill path with `kill_apps`, and once torn down it
    holds no app: it kept `_procs` populated, so a closed backend still claimed the apps it killed."""
    nb = bare_nested_backend()
    nb.display = ":88"
    nb._video_sessions = {}
    nb._xserver = type("X", (), {"poll": lambda self: None, "terminate": lambda self: None,
                                 "wait": lambda self, timeout: 0})()
    swept: list[tuple[str, bool]] = []
    monkeypatch.setattr("interact.desktop.orphans.sweep_if_owned",
                        lambda display, *, owned: swept.append((display, owned)) or [])
    monkeypatch.setattr("interact.desktop.orphans.display_clients", lambda display: [])
    monkeypatch.setattr("interact.launch.sandbox_profiles", lambda display: [])
    proc = nb.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
    nb.close()
    assert proc.poll() is not None
    assert swept == [(":88", True)]
    assert nb._procs == []


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
    monkeypatch.setattr("interact.desktop.backend.shutil.which", lambda _: "/usr/bin/Xephyr")
    monkeypatch.setattr("interact.desktop.nested.subprocess.Popen", lambda *a, **k: _DummyProc())
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
    monkeypatch.setattr(srv.sandbox, "_close_sandbox", lambda reason=None: closed.append(reason))
    srv._reap_sandbox(ttl=900)
    assert len(closed) == 1
    assert "idle" in closed[0], "the reap reason must be carried so a later caller can tell why (#141)"


def test_active_or_recording_sandbox_survives(monkeypatch):
    import interact.server as srv

    closed = []
    monkeypatch.setattr(srv.sandbox, "_close_sandbox", lambda reason=None: closed.append(True))
    monkeypatch.setattr(srv.sandbox, "_sandbox", _fake_sandbox(idle=10.0))
    srv._reap_sandbox(ttl=900)                     # recently used → kept
    monkeypatch.setattr(srv.sandbox, "_sandbox", _fake_sandbox(idle=99999.0, recording=True))
    srv._reap_sandbox(ttl=900)                     # mid-recording → NEVER reaped under the agent
    assert closed == []


def test_sandbox_touch_marks_use(monkeypatch):
    """Every _get_sandbox() attach/launch refreshes idleness, so an actively-driven sandbox
    never hits the TTL."""
    import time as _time

    from interact.desktop import NestedBackend

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


# =========================================================================================
# `_sandbox_death_diagnostics` names WHY the sandbox was recreated (from
# test_sandbox_death_diagnostics.py) — it must not merely report the FRESH backend's
# (necessarily healthy) status when a self-heal happened under a caller (#141).
# =========================================================================================


class _HealthyBackend:
    """Stands in for the fresh sandbox `_get_sandbox` already self-healed — `is_alive` is True,
    so `display_health()`/`last_app_output()` alone say nothing about the PREVIOUS one's death."""

    def display_health(self) -> str:
        return ""

    def last_app_output(self, limit: int = 800) -> str:
        return ""


def test_diagnostics_lead_with_the_recorded_replace_reason(monkeypatch):
    monkeypatch.setattr(
        _sandbox_mod, "last_replace_reason", lambda: "The sandbox Xephyr :99 is DOWN (SIGKILL)"
    )
    msg = _targets_mod._sandbox_death_diagnostics(_HealthyBackend())
    assert "recreated automatically" in msg
    assert "SIGKILL" in msg


def test_diagnostics_fall_back_to_current_health_when_never_replaced(monkeypatch):
    """No respawn happened — must fall through to the fresh backend's own health/output, not
    claim a replacement that never occurred."""
    monkeypatch.setattr(_sandbox_mod, "last_replace_reason", lambda: None)
    msg = _targets_mod._sandbox_death_diagnostics(_HealthyBackend())
    assert "recreated automatically" not in msg
    assert msg == ""
