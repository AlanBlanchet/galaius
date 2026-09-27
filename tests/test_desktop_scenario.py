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

import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from interact.desktop import DesktopBackend, DesktopWindow, NestedBackend
from tests.support.desktop import RecordingBackend

FIXTURE = Path(__file__).parent / "fixtures" / "drag_window.py"
PANEL = Path(__file__).parent / "fixtures" / "panel.py"
BAR_H = 32  # matches drag_window.py's title-bar height


def test_drag_circle_geometry() -> None:
    be = RecordingBackend()
    cx, cy, radius = 300.0, 136.0, 80.0
    be.drag_circle(cx, cy, radius, steps=24)

    assert be.calls[0] == ("move", cx, cy), "press must start at the grab point"
    assert be.calls[1] == ("down", "left")
    # #136: a settle move at the drop point follows the release — a webview that captured the
    # pointer for the drag only learns the button is up from the NEXT motion/button event.
    assert be.calls[-1] == ("move", cx, cy), "must settle-move after release (#136)"
    assert be.calls[-2] == ("up", "left"), "must release the button before the settle move"
    assert be.calls[-3] == ("move", cx, cy), "must return to the grab point (closed loop)"

    orbit = [(x, y) for tag, x, y in be.calls[2:-3] if tag == "move"]
    radii = [math.hypot(x - cx, y - cy) for x, y in orbit]
    assert all(abs(r - radius) < 1e-6 for r in radii), "every orbit point sits on the circle"
    assert any(x > cx for x, _ in orbit) and any(x < cx for x, _ in orbit), "spans left+right"
    assert any(y > cy for _, y in orbit) and any(y < cy for _, y in orbit), "spans up+down"


@pytest.mark.parametrize("count", [1, 2], ids=["click", "double_click"])
def test_click_count_repeats_the_press_release_pair(count: int) -> None:
    """#116: a double-click is the click primitive with count=2 — the same down/up pair, repeated
    at the same point, with a gap inside every toolkit's double-click interval."""
    be = RecordingBackend()
    be.click(10.0, 20.0, "left", count=count)
    assert be.calls[0] == ("move", 10.0, 20.0)
    assert be.calls[1:] == [("down", "left"), ("up", "left")] * count


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
        # Keys go where keyboard focus IS, not where the click just went: wait for the panel to
        # report the entry focused before typing, or under load "hello" lands nowhere (#130).
        focused = _wait_for_state(state_path, lambda s: s.get("focus") == "Enter text")
        assert focused.get("focus") == "Enter text", "the entry never took keyboard focus after the click"
        backend.type_text("hello")
        typed = _wait_for_state(state_path, lambda s: "hello" in s.get("typed", "")).get("typed", "")
        assert "hello" in typed, f"typing did not land (typed={typed!r})"

        backend.click(*center("Reset"))
        assert _wait_for_state(state_path, lambda s: s.get("count") == 0).get("count") == 0
    finally:
        backend.close()


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_a_ctrl_chord_reaches_the_app_through_the_nested_path(tmp_path: Path) -> None:
    """A Ctrl-chord and a shifted character reach a real toolkit — through the XDOTOOL path.

    **This does not settle #115, and it is worth being exact about why.** The nested backend sends
    keys with `xdotool key`; #115 reports the UINPUT backend, which synthesises evdev events on a
    virtual device. Those are different code paths, so a pass here says nothing about the reported
    failure — it says the sandbox's own path is sound, which is worth pinning but was never the
    question.

    The uinput path cannot be exercised from a test: it is a SYSTEM-WIDE virtual keyboard, so its
    keystrokes land in whichever window holds focus at that instant — including the one running
    the agent that launched the test. Settling #115 needs a machine whose real desktop is
    disposable, not this one.

    What it does cover: a toolkit binding either fires or it does not, so this pins the nested
    path against regression, and the shifted character pins the single-frame write that falsified
    the atomic-SYN-frame theory of the chord bug.
    """
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    backend = NestedBackend(display=97, size="700x600")
    try:
        backend.spawn([_tk_python(), str(PANEL), str(state_path), "360x420+120+90"])
        state = _wait_for_state(state_path, lambda s: "widgets" in s)
        wx, wy, ww, wh = state["widgets"]["Enter text"]
        backend.click(wx + ww // 2, wy + wh // 2)  # focus, so the toplevel has the keyboard

        backend.type_text("Hi!")
        typed = _wait_for_state(state_path, lambda s: s.get("typed")).get("typed", "")
        assert typed == "Hi!", (
            f"a shifted character did not survive the sandbox: {typed!r}. type_text writes "
            "shift-down, key, shift-up in ONE evdev frame, so this failing would mean the "
            "atomic-frame theory of #115 is right after all"
        )

        backend.key("ctrl+p")
        got = _wait_for_state(state_path, lambda s: s.get("chord"), timeout=6).get("chord")
        assert got == "ctrl+p", (
            "the chord arrived without its modifier — #115 reproduced in the sandbox, which is "
            "the evidence needed to go looking at the udev settle race rather than the framing"
        )
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


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_double_click_fires_the_apps_dblclick_binding_in_the_nested_sandbox(tmp_path: Path) -> None:
    """#116: two rapid `click`s don't reliably coalesce into an OS-level dblclick, so a product
    behaviour gated on one stayed unverifiable live. `click(count=2)` through the SAME DesktopWindow
    `run_actions` drives must fire the toolkit's OWN <Double-Button-1> binding, which the app
    records itself."""
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    backend = NestedBackend(display=98, size="700x600")
    try:
        backend.spawn([_tk_python(), str(PANEL), str(state_path), "360x420+120+90"])
        widgets = _wait_for_state(state_path, lambda s: "widgets" in s)["widgets"]
        win = DesktopWindow.find_in(backend, "interact-panel")
        assert win is not None
        wx, wy, ww, wh = widgets["Click Me"]
        asyncio.run(win.click(wx + ww // 2 - win.x, wy + wh // 2 - win.y, count=2))
        got = _wait_for_state(state_path, lambda s: s.get("double")).get("double")
        assert got == "Click Me", f"the app saw no double-click (state double={got!r})"
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
    not os.access("/dev/uinput", os.W_OK) or not os.environ.get("DISPLAY"),
    reason="needs writable /dev/uinput + an X display",
)
def test_local_backend_creates_pointer_and_keyboard() -> None:
    """Deterministic local-path check (no clicking the live desktop): LocalBackend brings
    up BOTH a uinput pointer and a keyboard — the separate keyboard is the structural fix
    for the dropped-keystrokes bug — and maps the absolute pointer over the FULL X root,
    the fix for the multi-monitor coordinate-scaling bug. Injects nothing.

    Device creation is asserted against the KERNEL's sysfs listing, not ``xinput list``. xinput
    only ever enumerated XWayland's own X11 devices, so on a Wayland host this check failed on a
    device that was in fact created and working — the environment-dependent failure of #79. Sysfs
    is written by the kernel at UI_DEV_CREATE, identically under Xorg and Wayland, so the check is
    now display-server-agnostic and the Wayland skip is gone. (Injection itself does reach both
    Wayland and XWayland clients: XWayland receives them forwarded via the compositor's own
    wl_seat, so there is no separate X11 injection path to verify.)"""
    from interact.desktop.backend import LocalBackend, _x11_root_size, _x11_screen_size
    from interact.desktop.input import kernel_input_device_names

    backend = LocalBackend()
    try:
        time.sleep(0.8)  # let udev/libinput register the new devices
        names = kernel_input_device_names()
        assert "interact-virtual-pointer" in names, f"absolute pointer device not created: {names}"
        assert "interact-virtual-keyboard" in names, "keyboard device not created (typing would silently no-op)"

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
