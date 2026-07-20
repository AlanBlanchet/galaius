"""Desktop test suite — the local/nested backend abstraction and a window-drag scenario.

Mirrors the browser ``Scenario`` but for the desktop: drive a real window through the
:class:`DesktopBackend` interface and assert it moved. Two layers:

* ``test_drag_circle_geometry`` — pure, always runs, free. A fake backend records the
  primitive calls so we can assert ``drag_circle`` actually traces a closed circle through
  all four quadrants (the "grab the title bar and orbit the window" motion), independent
  of any display server.
* ``test_drag_window_in_circle`` — integration, skipped unless an X display + Xephyr +
  xdotool + maim + a Tk-capable Python are present. Spins up an isolated nested display,
  launches the WM-less draggable window, orbits its title bar, and asserts the real window
  travelled a circle and returned home. No VLM — the title-bar position is known, so it's
  free and non-intrusive (nothing touches the user's real session).
"""

import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from interact.desktop import DesktopBackend, NestedBackend

FIXTURE = Path(__file__).parent / "fixtures" / "drag_window.py"
PANEL = Path(__file__).parent / "fixtures" / "panel.py"
BAR_H = 32  # matches drag_window.py's title-bar height


class RecordingBackend(DesktopBackend):
    """A backend that records primitive calls instead of touching a display."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def capture(self) -> bytes:
        return b""

    def move(self, x: float, y: float) -> None:
        self.calls.append(("move", x, y))

    def mouse_down(self, button: str = "left") -> None:
        self.calls.append(("down", button))

    def mouse_up(self, button: str = "left") -> None:
        self.calls.append(("up", button))


def test_drag_circle_geometry() -> None:
    be = RecordingBackend()
    cx, cy, radius = 300.0, 136.0, 80.0
    be.drag_circle(cx, cy, radius, steps=24)

    assert be.calls[0] == ("move", cx, cy), "press must start at the grab point"
    assert be.calls[1] == ("down", "left")
    assert be.calls[-1] == ("up", "left"), "must release the button last"
    assert be.calls[-2] == ("move", cx, cy), "must return to the grab point (closed loop)"

    orbit = [(x, y) for tag, x, y in be.calls[2:-2] if tag == "move"]
    radii = [math.hypot(x - cx, y - cy) for x, y in orbit]
    assert all(abs(r - radius) < 1e-6 for r in radii), "every orbit point sits on the circle"
    assert any(x > cx for x, _ in orbit) and any(x < cx for x, _ in orbit), "spans left+right"
    assert any(y > cy for _, y in orbit) and any(y < cy for _, y in orbit), "spans up+down"


def _tk_python() -> str | None:
    """A Python whose tkinter starts a Tk() under X (uv's standalone Tk aborts on XCB)."""
    for exe in ("/usr/bin/python3", sys.executable):
        if not exe or not Path(exe).exists():
            continue
        probe = subprocess.run(
            [exe, "-c", "import tkinter; tkinter.Tk().destroy()"],
            capture_output=True,
        )
        if probe.returncode == 0:
            return exe
    return None


def _skip_reason() -> str | None:
    import os

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "no X display for Xephyr to nest in"
    for tool in ("Xephyr", "xdotool", "maim"):
        if not shutil.which(tool):
            return f"{tool} not installed"
    if _tk_python() is None:
        return "no Tk-capable Python (apt install python3-tk)"
    return None


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_drag_window_in_circle(tmp_path: Path) -> None:
    pos_file = tmp_path / "positions.txt"
    pos_file.write_text("")
    start_geom = "320x220+140+120"

    backend = NestedBackend(display=98, size="700x600")
    try:
        backend.spawn([_tk_python(), str(FIXTURE), str(pos_file), start_geom])

        deadline = time.monotonic() + 8
        geom = None
        while time.monotonic() < deadline:
            geom = backend.window_geometry("interact-drag-window")
            if geom:
                break
            time.sleep(0.2)
        assert geom is not None, "draggable window never appeared in the nested display"
        x, y, w, _h = geom

        # A non-trivial capture of the isolated screen proves the capture path too.
        assert len(backend.capture()) > 1000

        cx, cy, radius = x + w / 2, y + BAR_H / 2, 80.0
        backend.drag_circle(cx, cy, radius, steps=24)
        time.sleep(0.3)

        pts = [tuple(map(int, ln.split(","))) for ln in pos_file.read_text().split() if "," in ln]
        assert len(pts) >= 12, f"expected a full orbit of moves, got {len(pts)}"

        # The window orbits its home (x, y): the path must bracket home on both axes
        # and span at least the circle's diameter-ish, not just drift one way.
        xs = [px for px, _ in pts]
        ys = [py for _, py in pts]
        assert min(xs) < x < max(xs), "window circled left and right of home"
        assert min(ys) < y < max(ys), "window circled above and below home"
        assert max(xs) - min(xs) >= radius and max(ys) - min(ys) >= radius, "full-size orbit"

        end_geom = backend.window_geometry("interact-drag-window")
        assert end_geom is not None
        assert abs(end_geom[0] - x) <= 4 and abs(end_geom[1] - y) <= 4, "window returned home"
    finally:
        backend.close()


def _wait_for_state(state_path: Path, predicate, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    state: dict = {}
    while time.monotonic() < deadline:
        try:
            state = json.loads(state_path.read_text() or "{}")
        except (json.JSONDecodeError, FileNotFoundError):
            state = {}
        if predicate(state):
            return state
        time.sleep(0.15)
    return state


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_panel_interactions_nested(tmp_path: Path) -> None:
    """The local-PC test in a CI-safe, free form: capture is exercised, and every
    interaction (click each button, type into the field) is verified against the panel's
    own recorded state — no VLM (clicks use the widget geometry the panel reports). Runs
    in the isolated nested display so it never touches the real session."""
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    backend = NestedBackend(display=98, size="700x600")
    try:
        backend.spawn([_tk_python(), str(PANEL), str(state_path), "360x420+120+90"])

        state = _wait_for_state(state_path, lambda s: "widgets" in s)
        assert "widgets" in state, "panel never reported its widget geometry"
        widgets = state["widgets"]

        # Window-targeted capture works even though we never raised/focused the panel.
        assert len(backend.capture_window("interact-panel")) > 1000

        def center(name: str) -> tuple[int, int]:
            wx, wy, ww, wh = widgets[name]
            return wx + ww // 2, wy + wh // 2

        backend.click(*center("Click Me"))
        assert _wait_for_state(state_path, lambda s: s.get("last") == "Click Me").get("last") == "Click Me"

        for _ in range(2):
            backend.click(*center("Increment"))
            backend.move(10, 10)  # leave the button so the next press isn't a double-click
            time.sleep(0.4)
        assert _wait_for_state(state_path, lambda s: s.get("count", 0) >= 2).get("count") == 2

        cx, cy = center("Enter text")
        backend.click(cx, cy)
        backend.type_text("hello")
        typed = _wait_for_state(state_path, lambda s: "hello" in s.get("typed", "")).get("typed", "")
        assert "hello" in typed, f"typing did not land (typed={typed!r})"

        backend.click(*center("Reset"))
        assert _wait_for_state(state_path, lambda s: s.get("count") == 0).get("count") == 0
    finally:
        backend.close()


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_window_id_prefers_the_largest_same_titled_window(tmp_path):
    """A toolkit can map several windows with one title — Flutter spawns a hidden ~10x10 GL helper
    alongside the real window. _window_id must pick the largest, or capture/input hit the phantom:
    the exact bug that made `target="nested:aino"` grab the wrong window."""
    backend = NestedBackend(display=98, size="900x900")
    try:
        backend.spawn([_tk_python(), str(PANEL), str(tmp_path / "small.json"), "200x200+0+0"])
        backend.spawn([_tk_python(), str(PANEL), str(tmp_path / "big.json"), "640x760+150+80"])
        chosen = None
        for _ in range(60):
            ids = subprocess.run(
                ["xdotool", "search", "--name", "interact-panel"],
                env=backend.env, capture_output=True, text=True,
            ).stdout.split()
            if len(ids) >= 2:
                chosen = backend._window_id("interact-panel")
                break
            time.sleep(0.25)
        assert chosen is not None, "the two panel windows never both appeared"
        info = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", chosen],
            env=backend.env, capture_output=True, text=True, check=True,
        ).stdout
        geo = dict(ln.split("=", 1) for ln in info.splitlines() if "=" in ln)
        assert int(geo["WIDTH"]) >= 500, f"picked a phantom small window ({geo['WIDTH']}x{geo['HEIGHT']})"
    finally:
        backend.close()


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_desktop_window_drives_nested_backend(tmp_path: Path) -> None:
    """The SAME DesktopWindow the MCP `run_actions` path uses, bound to the nested
    backend, drives real clicks/typing into the sandbox — proving the backend is wired
    through DesktopWindow without touching the real session."""
    import asyncio

    from interact.desktop import DesktopWindow

    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    backend = NestedBackend(display=98, size="700x600")
    try:
        backend.spawn([_tk_python(), str(PANEL), str(state_path), "360x420+120+90"])
        widgets = _wait_for_state(state_path, lambda s: "widgets" in s)["widgets"]

        win = DesktopWindow.find_in(backend, "interact-panel")
        assert win is not None and win.w > 0

        def rel(name: str) -> tuple[int, int]:
            wx, wy, ww, wh = widgets[name]  # screen coords on :N → window-relative
            return wx + ww // 2 - win.x, wy + wh // 2 - win.y

        asyncio.run(win.click(*rel("Click Me")))
        assert _wait_for_state(state_path, lambda s: s.get("last") == "Click Me").get("last") == "Click Me"

        fx, fy = rel("Enter text")
        asyncio.run(win.click(fx, fy))
        asyncio.run(win.type_text("hi"))
        assert "hi" in _wait_for_state(state_path, lambda s: "hi" in s.get("typed", "")).get("typed", "")

        # capture() on a bound window targets the nested window
        assert len(win.capture()) > 1000
    finally:
        backend.close()


def _local_skip_reason() -> str | None:
    if not os.environ.get("INTERACT_LOCAL_E2E"):
        return "opt-in (set INTERACT_LOCAL_E2E=1) — drives the REAL cursor via uinput"
    if not os.environ.get("DISPLAY"):
        return "no real X display"
    if not os.access("/dev/uinput", os.W_OK):
        return "/dev/uinput not writable (needs a udev rule + the `input` group)"
    if _tk_python() is None:
        return "no Tk-capable Python"
    return None


@pytest.mark.skipif(
    not os.access("/dev/uinput", os.W_OK) or not os.environ.get("DISPLAY") or not shutil.which("xinput"),
    reason="needs writable /dev/uinput + an X display + xinput",
)
@pytest.mark.skipif(
    os.environ.get("XDG_SESSION_TYPE") == "wayland",
    reason="Wayland session: uinput devices route to the compositor and never appear in "
    "XWayland's xinput list, so device creation can't be verified this way (#79)",
)
def test_local_backend_creates_pointer_and_keyboard() -> None:
    """Deterministic local-path check (no clicking the live desktop): LocalBackend brings
    up BOTH a uinput pointer and a keyboard — the separate keyboard is the structural fix
    for the dropped-keystrokes bug — and maps the absolute pointer over the FULL X root,
    the fix for the multi-monitor coordinate-scaling bug. Injects nothing."""
    from interact.desktop.backend import LocalBackend, _x11_root_size, _x11_screen_size

    backend = LocalBackend()
    try:
        time.sleep(0.8)  # let libinput register the new devices
        listing = subprocess.run(["xinput", "list"], capture_output=True, text=True).stdout
        assert "interact-virtual-pointer" in listing, "absolute pointer device not created"
        assert "interact-virtual-keyboard" in listing, "keyboard device not created (typing would silently no-op)"

        root_w, root_h = _x11_root_size()
        primary_w, _ = _x11_screen_size()
        assert root_w >= primary_w, "root must span at least the primary monitor"
        assert backend._pointer.screen_w == root_w and backend._pointer.screen_h == root_h, \
            "pointer must map over the whole root, not just the primary monitor"
    finally:
        backend.close()


@pytest.mark.skipif(_local_skip_reason() is not None, reason=_local_skip_reason() or "")
def test_local_backend_drives_real_panel(tmp_path: Path) -> None:
    """The real-PC path: LocalBackend (system-wide uinput) clicks and types into a panel
    on the REAL display, verified against the panel's recorded state. Opt-in
    (INTERACT_LOCAL_E2E=1) because it moves the real cursor and needs /dev/uinput."""
    from interact.desktop.backend import LocalBackend

    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    proc = subprocess.Popen(
        [_tk_python(), str(PANEL), str(state_path), "360x420+60+60"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    backend = LocalBackend()
    try:
        time.sleep(1.2)  # let libinput enumerate the new uinput devices + Tk map/render
        widgets = _wait_for_state(state_path, lambda s: "widgets" in s)["widgets"]

        def center(name: str) -> tuple[int, int]:
            wx, wy, ww, wh = widgets[name]
            return wx + ww // 2, wy + wh // 2

        def activate_panel() -> None:
            wids = subprocess.run(
                ["xdotool", "search", "--name", "interact-panel"], capture_output=True, text=True
            ).stdout.split()
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[-1]])
                time.sleep(0.3)

        # Pointer path: an absolute uinput click lands on the right widget of a window on
        # the REAL (multi-monitor) display — this is what caught the root-vs-primary
        # coordinate-scaling bug. (Repeated/independent clicks on a busy live session are
        # not asserted here; that's environment-dependent, not a backend property.)
        activate_panel()
        backend.click(*center("Click Me"))
        assert _wait_for_state(state_path, lambda s: s.get("last") == "Click Me").get("last") == "Click Me"

        # Keyboard path: uinput key events reach the focused field — this caught the
        # missing-keycodes bug (the device must declare the keys it sends). Activating the
        # window gives it focus (the WM's click-to-focus job on a real desktop).
        activate_panel()
        backend.click(*center("Enter text"))
        time.sleep(0.1)
        backend.type_text("hello")
        assert "hello" in _wait_for_state(state_path, lambda s: "hello" in s.get("typed", "")).get("typed", "")

        assert len(backend.capture()) > 1000  # maim grab of the real screen
    finally:
        backend.close()
        proc.terminate()


def test_nested_server_command() -> None:
    from interact.desktop.backend import nested_server_command

    visible = nested_server_command(":99", "800x600", headless=False)
    assert visible[0] == "Xephyr" and "800x600" in visible

    headless = nested_server_command(":99", "800x600", headless=True)
    assert headless[0] == "Xvfb" and "800x600x24" in headless


@pytest.mark.integration  # uses a REAL model → exempt from the unit-test litellm block, keys-gated
@pytest.mark.skipif(
    not os.environ.get("INTERACT_DESKTOP_E2E") or _skip_reason() is not None,
    reason="opt-in (set INTERACT_DESKTOP_E2E=1 + a grounding API key) — uses a paid VLM call",
)
def test_desktop_scenario_full_e2e() -> None:
    """The full grounding-driven desktop scenario (panel → detect → act), the desktop
    analogue of the browser Scenario. Off by default because detection costs a VLM call;
    enable with INTERACT_DESKTOP_E2E=1. Runs in the nested sandbox."""
    import asyncio

    from interact.probe import DesktopScenario

    run = DesktopScenario.build(model=None, all_providers=False, session_ts="e2e", target="nested")
    asyncio.run(run.run())  # asserts via its own per-step reports; smoke that it completes
