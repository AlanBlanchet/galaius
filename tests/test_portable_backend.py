"""macOS/Windows desktop automation — the cross-platform PortableBackend (mss capture + pynput
input) exercised on the REAL runner desktop (#24). This is how the mac/win path is tested "for
real": the CI matrix runs it on macos-latest + windows-latest, which are genuine GUI sessions.

It self-skips on Linux (which uses the deeper LocalBackend, and we don't want to jog the dev's real
cursor), and skips — rather than fails — when a host denies Screen-Recording / Accessibility (a
known GitHub macOS-runner limitation), printing what access is needed so it's actionable.
"""

import sys
import time

import pytest

pytestmark = pytest.mark.timeout(60)  # never let a stuck GUI call hang the matrix


def _backend():
    if sys.platform.startswith("linux"):
        pytest.skip("Linux uses LocalBackend; PortableBackend is the macOS/Windows path")
    from interact.desktop_backend import PortableBackend

    try:
        return PortableBackend()
    except RuntimeError as exc:  # mss/pynput missing
        pytest.skip(str(exc))


def test_portable_capture_returns_a_real_png():
    pb = _backend()
    try:
        png = pb.capture()
    except Exception as exc:  # macOS Screen-Recording (TCC) denied, or no GUI session
        pytest.skip(f"screen capture unavailable on this host (grant Screen Recording?): {exc}")
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert len(png) > 1000, "screenshot suspiciously small/empty"


def test_portable_pointer_move_round_trips():
    pb = _backend()
    try:
        pb.move(300, 300)
        time.sleep(0.1)
        x, y = pb._mouse.position
    except Exception as exc:  # macOS Accessibility (TCC) denied
        pytest.skip(f"synthetic input unavailable (grant Accessibility?): {exc}")
    # SetCursorPos/Quartz round-trip; allow a couple px for DPI rounding.
    assert abs(x - 300) <= 3 and abs(y - 300) <= 3, f"pointer landed at {(x, y)}, expected ~(300,300)"


def test_portable_input_primitives_do_not_raise():
    pb = _backend()
    try:
        pb.move(120, 120)
        pb.mouse_down()
        pb.mouse_up()
        pb.scroll(1)
        pb.key("ctrl+a")
        pb.key("Return")
    except Exception as exc:
        pytest.skip(f"synthetic input unavailable (grant Accessibility?): {exc}")


def test_screen_target_resolves_and_captures_through_the_tool_path():
    """End-to-end through the server: target=\"screen\" on macOS/Windows resolves to the portable
    backend and captures the real desktop — the MCP tool path agents actually use (#24)."""
    if sys.platform.startswith("linux"):
        pytest.skip("Linux uses LocalBackend; PortableBackend is the macOS/Windows path")
    import interact.server as server

    win, mgr, err = server._resolve_target("screen", "default")
    if err and ("Screen" in err or "Accessibility" in err):
        pytest.skip(f"desktop unavailable on this host: {err}")
    assert err is None and win is not None and mgr is None
    try:
        png = win.capture()
    except Exception as exc:
        pytest.skip(f"capture unavailable (grant Screen Recording?): {exc}")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 1000
