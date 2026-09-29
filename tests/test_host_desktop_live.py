"""The host-desktop tools on a REAL display, end to end through the MCP tool functions — the
session an agent ran with raw xdotool, redone with galaius only:

    open a window → wait for it (no sleep) → click it → type into it with OS-level keys → Return
    → wait for it to be gone
    → list monitors → capture one monitor, one region, downscaled.

Linux: a PRIVATE Xvfb started here; DISPLAY is repointed for the test, so the user's own session
is never touched. Windows / macOS: the runner's real desktop, and only when
GALAIUS_LIVE_DESKTOP=1 (set by CI) — a developer's full-suite run must not type into their screen.
"""

import asyncio
import io
import os
import shutil
import subprocess
import sys
import textwrap

import numpy as np
import pytest
from PIL import Image

from galaius import server as srv
from galaius.actions.models import (
    ClickAction,
    KeyPressAction,
    ScreenshotAction,
    SleepAction,
    TypeTextAction,
    WaitForAction,
)
from galaius.desktop import host
from tests.support.desktop import tk_env, tk_python

pytestmark = pytest.mark.timeout(120)

TITLE = "galaius-live-host"

# A Text widget filling the window, so a click at the window's centre lands in it on every OS
# (Windows' GetWindowRect includes the title bar). Return prints what was typed and exits.
_APP = textwrap.dedent(
    """
    import sys, tkinter as tk
    root = tk.Tk()
    root.title(sys.argv[1])
    root.geometry("420x240+60+60")
    text = tk.Text(root, bg="white")
    text.pack(fill="both", expand=True)
    def done(_):
        print(text.get("1.0", "end").strip(), flush=True)
        root.destroy()
        return "break"
    text.bind("<Return>", done)
    root.after(60000, root.destroy)
    root.mainloop()
    """
)


@pytest.fixture
def live_display(monkeypatch, tmp_path):
    """The display the test may drive: a private Xvfb on Linux, the runner desktop elsewhere."""
    if sys.platform.startswith("linux"):
        missing = [t for t in ("Xvfb", "xdotool", "maim", "xrandr") if shutil.which(t) is None]
        if missing:
            pytest.skip(f"needs {', '.join(missing)} for a private X display")
        number = 180 + os.getpid() % 60
        server = subprocess.Popen(
            ["Xvfb", f":{number}", "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            ready = False
            for _ in range(50):
                ready = subprocess.run(
                    ["xdpyinfo", "-display", f":{number}"], capture_output=True
                ).returncode == 0
                if ready or server.poll() is not None:
                    break
                asyncio.run(asyncio.sleep(0.1))
            if not ready:
                pytest.skip(f"Xvfb :{number} did not start")
            monkeypatch.setenv("DISPLAY", f":{number}")
            monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
            monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
            host.host_desktop.cache_clear()
            yield
        finally:
            host.host_desktop.cache_clear()
            server.terminate()
            server.wait(timeout=5)
        return
    if os.environ.get("GALAIUS_LIVE_DESKTOP") != "1":
        pytest.skip("drives the real desktop: set GALAIUS_LIVE_DESKTOP=1 (CI does)")
    host.host_desktop.cache_clear()
    yield
    host.host_desktop.cache_clear()


def _app(tmp_path) -> subprocess.Popen:
    python = tk_python()
    if python is None:
        pytest.skip("no Python here can start Tk on this display (apt install python3-tk)")
    script = tmp_path / "app.py"
    script.write_bytes(_APP.encode())
    return subprocess.Popen(
        [python, str(script), TITLE], env=tk_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _run(actions) -> str:
    out = asyncio.run(srv.run_actions(actions=actions, target="screen"))
    assert "ERROR" not in out and "SKIPPED" not in out, out
    return out


def test_open_wait_type_close_capture(live_display, tmp_path):
    app = _app(tmp_path)
    try:
        out = asyncio.run(srv.run_actions(actions=[WaitForAction(window=TITLE, timeout=20000)], target="screen"))
        if "ERROR" in out:
            died = app.poll() is not None
            pytest.fail(f"{out}\napp {'exited: ' + app.communicate()[1] if died else 'still running'}")
        window = next(w for w in host.host_desktop().windows() if TITLE in w.title)
        whole = srv.DesktopWindow.screen("screen", host.host_desktop())
        cx = window.x + window.w // 2 - whole.x
        cy = window.y + window.h // 2 - whole.y
        report = _run([
            ClickAction(x=cx, y=cy),
            TypeTextAction(text="whispering", clear_first=False),
            KeyPressAction(key="Return"),
            WaitForAction(window=TITLE, state="hidden", timeout=10000),
        ])
        assert "gone" in report, report
        stdout, stderr = app.communicate(timeout=10)
        assert stdout.strip() == "whispering", (stdout, stderr)
    finally:
        if app.poll() is None:
            app.kill()

    listing = asyncio.run(srv.list_desktop_windows())
    assert 'target="screen:0"' in listing, listing
    shot = tmp_path / "monitor.png"
    reply = asyncio.run(
        srv.screenshot(target="screen:0", region=[0, 0, 400, 200], max_width=200, path=str(shot))
    )
    assert not reply.startswith("ERROR"), reply
    assert Image.open(io.BytesIO(shot.read_bytes())).size == (200, 100), reply
    assert "scale 2" in reply, reply


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="Xvfb has no OS launcher to open")
def test_os_launcher_opens_and_closes(live_display, tmp_path):
    """The pasted session's first move: the OS key opens the launcher (Start menu / Spotlight), so
    the screen changes; Escape closes it."""
    chord = "cmd+space" if sys.platform == "darwin" else "super"
    before, opened = tmp_path / "before.png", tmp_path / "opened.png"
    assert not asyncio.run(srv.screenshot(target="screen", path=str(before))).startswith("ERROR")
    try:
        _run([KeyPressAction(key=chord), SleepAction(duration=2), ScreenshotAction(path=str(opened))])
    finally:
        _run([KeyPressAction(key="Escape")])
    a = np.asarray(Image.open(before).convert("L"), dtype=np.int16)
    b = np.asarray(Image.open(opened).convert("L"), dtype=np.int16)
    moved = float((np.abs(a - b) > 24).mean()) if a.shape == b.shape else 1.0
    assert moved > 0.005, f"the launcher did not appear: {moved:.3%} of pixels changed"
