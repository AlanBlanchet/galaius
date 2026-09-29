"""The host-desktop tools on a REAL display, end to end through the MCP tool functions — the
session an agent ran with raw xdotool, redone with galaius only:

    open a window → wait for it (no sleep) → click it → type into it with OS-level keys → wait until
    its result row READS "Results for whispering" (OCR) → Return → wait for it to be gone
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

import pytest
from PIL import Image

from galaius import server as srv
from galaius.actions.models import (
    ClickAction,
    KeyPressAction,
    TypeTextAction,
    WaitForAction,
)
from galaius.desktop import host
from tests.support.desktop import tk_env, tk_python

pytestmark = pytest.mark.timeout(120)

TITLE = "galaius-live-host"

# A tiny launcher: a Text field filling the top half (a click at the window's centre lands in it on
# every OS — Windows' GetWindowRect includes the title bar), and under it a result row that shows
# "Results for <query>" a moment after typing, the way a launcher lists its hits. Return prints
# the query and exits.
_APP = textwrap.dedent(
    """
    import sys, tkinter as tk
    root = tk.Tk()
    root.title(sys.argv[1])
    root.geometry("520x300+60+60")
    text = tk.Text(root, bg="white", height=4)
    text.pack(fill="both", expand=True)
    row = tk.Label(root, text="", font=("Helvetica", 22), bg="white", fg="black", anchor="w")
    row.pack(fill="x", ipady=12)
    def show(_=None):
        query = text.get("1.0", "end").strip()
        row.configure(text=f"Results for {query}" if len(query) >= 3 else "")
    text.bind("<KeyRelease>", lambda _: root.after(300, show))
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
        # Only this window's rectangle is read: a launcher's list, not the whole desktop.
        region = (max(window.x - whole.x, 0), max(window.y - whole.y, 0), window.w, window.h)
        report = _run([
            ClickAction(x=cx, y=cy),
            TypeTextAction(text="whispering", clear_first=False),
            WaitForAction(text="Results for whispering", region=region, timeout=20000),
            KeyPressAction(key="Return"),
            WaitForAction(window=TITLE, state="hidden", timeout=10000),
            WaitForAction(text="Results for whispering", state="hidden", timeout=10000),
        ])
        assert "appeared" in report and report.count("gone") == 2, report
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


# (chord, query typed, a result title that differs from the query): the wait must see the RESULT
# row, not just echo the typed text back.
_LAUNCHERS = {
    "win32": ("super", "notep", "Notepad"),
    "darwin": ("cmd+space", "calcul", "Calculator"),
}


@pytest.mark.skipif(sys.platform not in _LAUNCHERS, reason="Xvfb has no OS launcher to open")
def test_os_launcher_search_shows_a_result(live_display):
    """The pasted session, end to end: the OS key opens the launcher, a query is typed, the wait
    reads the screen until the result row shows — no sleep — and Escape closes it again."""
    chord, query, result = _LAUNCHERS[sys.platform]
    try:
        report = _run([
            KeyPressAction(key=chord),
            WaitForAction(timeout=1500),  # the launcher's own open animation
            TypeTextAction(text=query, clear_first=False),
            WaitForAction(text=result, timeout=30000),
        ])
        assert "appeared" in report, report
    finally:
        _run([KeyPressAction(key="Escape")])
    assert "gone" in _run([WaitForAction(text=result, state="hidden", timeout=15000)])
