"""The user's own desktop as a target on every OS — keys aimed at the desktop itself, one monitor /
one region / downscaled captures, and waits on a window or on the pixels instead of a sleep.

Display-free: every OS path is exercised here through its pure half (xrandr text, Quartz window
dicts, a fake mss). The real-display run is tests/test_host_desktop_live.py."""

import asyncio
import io

import pytest
from PIL import Image

from galaius.actions.models import WaitForAction
from galaius.desktop import DesktopWindow
from galaius.desktop.backend import PortableBackend
from galaius.desktop.frames import Framing
from galaius.desktop.geometry import HostWindow, Monitor
from galaius.desktop.host import MacDesktop
from galaius.desktop.input import to_xdotool_key
from galaius.desktop.waits import DesktopWaitTimeout, until_window
from galaius.desktop.x11 import X11Display


def _png(w: int, h: int, colour=(255, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, format="PNG")
    return buf.getvalue()


def _size(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


# --- (a) keys: the OS key has one name per habit, and none of them may vanish -------------------


@pytest.mark.parametrize("name", ["super", "cmd", "command", "win", "meta", "Super"])
def test_a_lone_os_key_reaches_xdotool_as_super(name):
    assert to_xdotool_key(name) == "super"


@pytest.mark.parametrize(
    "name, pynput_attr",
    [("super", "cmd"), ("win", "cmd"), ("Escape", "esc"), ("Prior", "page_up"), ("Next", "page_down")],
)
def test_the_portable_backend_resolves_the_names_the_screen_target_sends(name, pynput_attr):
    """``DesktopWindow.press_key`` translates to X names BEFORE any backend sees the key, so the
    Windows / macOS backend receives ``Prior`` for pageup — which used to raise there."""
    be = PortableBackend.__new__(PortableBackend)

    class Key:
        pass

    for attr in ("cmd", "esc", "page_up", "page_down"):
        setattr(Key, attr, attr)
    be._Key = Key
    assert be._resolve_key(to_xdotool_key(name)) == pynput_attr


# --- (b) monitors: every OS lists them with geometry --------------------------------------------

_XRANDR = """Monitors: 3
 0: +*DP-1 2560/598x1440/336+0+0  DP-1
 1: +HDMI-1 1920/509x1080/286+2560+0  HDMI-1
 2: L 1720/400x1440/336+4480+0
"""


def test_x11_monitors_parse_index_geometry_and_name_including_virtual_ones(monkeypatch):
    """A virtual monitor (`xrandr --setmonitor L … none`, an ultrawide split in two) has no output
    after its geometry; it is still a monitor."""
    x = X11Display(":0")
    monkeypatch.setattr(x, "_xrandr_monitors", lambda: _XRANDR)
    assert x.monitors() == [
        Monitor(index=0, name="DP-1", x=0, y=0, w=2560, h=1440),
        Monitor(index=1, name="HDMI-1", x=2560, y=0, w=1920, h=1080),
        Monitor(index=2, name="L", x=4480, y=0, w=1720, h=1440),
    ]


class _FakeMss:
    """mss's shape: monitors[0] is the whole virtual screen, [1:] the physical monitors; a grab of a
    Retina region returns 2x the pixels asked for."""

    def __init__(self, scale: int = 1):
        self.scale = scale
        self.monitors = [
            {"left": -1920, "top": 0, "width": 3360, "height": 1080},
            {"left": 0, "top": 0, "width": 1440, "height": 900},
            {"left": -1920, "top": 0, "width": 1920, "height": 1080},
        ]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def grab(self, box):
        class Shot:
            pass

        shot = Shot()
        shot.size = (box["width"] * self.scale, box["height"] * self.scale)
        shot.bgra = b"\x00\x00\x00\xff" * (shot.size[0] * shot.size[1])
        return shot


def _portable(scale: int = 1) -> PortableBackend:
    be = PortableBackend.__new__(PortableBackend)
    fake = _FakeMss(scale)

    class Module:
        @staticmethod
        def mss():
            return fake

    be._mss = Module
    return be


def test_portable_monitors_come_from_mss_with_negative_origins():
    assert _portable().monitors() == [
        Monitor(index=0, name="monitor0", x=0, y=0, w=1440, h=900),
        Monitor(index=1, name="monitor1", x=-1920, y=0, w=1920, h=1080),
    ]


def test_a_retina_region_is_returned_in_input_units():
    """One image pixel must equal one pointer unit, or every click lands at 2x on a Retina Mac."""
    png = _portable(scale=2).capture_region(0, 0, 1440, 900)
    assert _size(png) == (1440, 900)


def test_the_whole_screen_spans_negative_origins():
    """A monitor LEFT of the primary has a negative x; the whole-screen target starts there so
    image (0, 0) maps to it, not to the primary's corner."""
    whole = DesktopWindow.screen("screen", _portable())
    assert (whole.x, whole.y, whole.w, whole.h) == (-1920, 0, 3360, 1080)
    assert whole.to_screen(0, 0) == (-1920, 0)


@pytest.mark.parametrize("spec", ["screen:1", "screen:monitor1"])
def test_one_monitor_by_index_or_name_on_any_backend(spec):
    mon = DesktopWindow.screen(spec, _portable())
    assert (mon.x, mon.y, mon.w, mon.h) == (-1920, 0, 1920, 1080)
    assert mon.to_screen(10, 10) == (-1910, 10)


def test_an_unknown_monitor_lists_what_exists():
    err = DesktopWindow.screen("screen:7", _portable())
    assert isinstance(err, str) and "0:monitor0" in err and "1:monitor1" in err


def test_a_monitor_capture_grabs_only_that_monitor():
    grabbed = []

    class Host(PortableBackend):
        def __init__(self):
            pass

        def monitors(self):
            return [Monitor(index=0, name="a", x=0, y=0, w=100, h=50),
                    Monitor(index=1, name="b", x=100, y=0, w=80, h=40)]

        def capture_region(self, x, y, w, h):
            grabbed.append((x, y, w, h))
            return _png(w, h)

    assert _size(DesktopWindow.screen("screen:1", Host()).capture()) == (80, 40)
    assert grabbed == [(100, 0, 80, 40)]


# --- (b) windows on macOS: Quartz dicts → titled, on-screen, normal-layer windows ---------------


def test_mac_windows_keep_normal_layer_titled_windows():
    entries = [
        {"kCGWindowNumber": 7, "kCGWindowLayer": 0, "kCGWindowName": "Inbox",
         "kCGWindowOwnerName": "Mail", "kCGWindowBounds": {"X": 10, "Y": 20, "Width": 800, "Height": 600}},
        {"kCGWindowNumber": 8, "kCGWindowLayer": 25, "kCGWindowName": "Menubar",
         "kCGWindowOwnerName": "SystemUIServer", "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1440, "Height": 24}},
        # No Screen Recording permission → macOS withholds the title; the owner still names it.
        {"kCGWindowNumber": 9, "kCGWindowLayer": 0, "kCGWindowOwnerName": "Finder",
         "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 400, "Height": 300}},
        {"kCGWindowNumber": 10, "kCGWindowLayer": 0, "kCGWindowName": "tiny",
         "kCGWindowOwnerName": "x", "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1, "Height": 1}},
    ]
    assert MacDesktop.windows_from_quartz(entries) == [
        HostWindow(handle=7, title="Inbox — Mail", x=10, y=20, w=800, h=600),
        HostWindow(handle=9, title="Finder", x=0, y=0, w=400, h=300),
    ]


# --- (b) region + max_width: crop, then fit, and say how to map back ------------------------------


def test_framing_crops_then_downscales_and_reports_the_mapping():
    png, note = Framing(region=(1920, 0, 1920, 1080), max_width=1280).apply(_png(3840, 1080))
    assert _size(png) == (1280, 720)
    assert "1920x1080+1920+0" in note and "1.5" in note


def test_framing_is_a_no_op_when_nothing_is_asked_or_the_image_is_already_small():
    raw = _png(800, 600)
    assert Framing().apply(raw) == (raw, "")
    png, note = Framing(max_width=1280).apply(raw)
    assert png == raw and note == ""


@pytest.mark.parametrize("region", [(0, 0, 900, 10), (-1, 0, 10, 10)])
def test_framing_refuses_a_region_outside_the_capture(region):
    with pytest.raises(ValueError, match="800x600"):
        Framing(region=region).apply(_png(800, 600))


def test_an_empty_region_is_refused_before_anything_is_captured():
    with pytest.raises(ValueError, match="positive width and height"):
        Framing(region=(0, 0, 0, 10))


# --- (c) waits: a window, or the pixels, instead of a sleep ----------------------------------------


def test_wait_for_takes_one_condition():
    assert WaitForAction(window="Settings").runs_on_desktop
    assert WaitForAction().runs_on_desktop  # bare pause
    assert not WaitForAction(selector="#x").runs_on_desktop
    with pytest.raises(ValueError, match="one"):
        WaitForAction(window="a", selector="#b")
    with pytest.raises(ValueError, match="visible"):
        WaitForAction(window="a", state="attached")


def _window(title: str) -> HostWindow:
    return HostWindow(handle=1, title=title, x=0, y=0, w=100, h=100)


def test_until_window_appears_then_goes():
    seen = iter([[], [], [_window("Search — whispering")]])
    out = asyncio.run(until_window(lambda: next(seen), "whisper", present=True, timeout_s=2, poll_s=0))
    assert "Search — whispering" in out
    gone = iter([[_window("Launcher")], []])
    assert "gone" in asyncio.run(until_window(lambda: next(gone), "launcher", present=False, timeout_s=2, poll_s=0))


def test_until_window_times_out_naming_what_was_there():
    with pytest.raises(DesktopWaitTimeout, match="Terminal"):
        asyncio.run(until_window(lambda: [_window("Terminal")], "Settings", present=True, timeout_s=0.05, poll_s=0.01))


def test_a_window_wait_runs_through_run_actions_on_a_screen_target(monkeypatch):
    """`key_press super` then `wait_for window=`: the wait reads the SAME host backend the screen
    target is bound to, and sees the launcher window the key opened."""
    from galaius.actions import dispatch
    from galaius.actions.models import KeyPressAction
    from galaius.state import DesktopState

    class Host(PortableBackend):
        def __init__(self):
            self.open: list[HostWindow] = []

        def key(self, name):
            self.open = [_window("Search — launcher")]

        def windows(self):
            return self.open

    monkeypatch.setattr(
        DesktopState, "capture",
        classmethod(lambda cls, name: cls(window_name=name, visible_text="", focused_element=None)),
    )
    screen = DesktopWindow(name="screen", wid=-1, x=0, y=0, w=320, h=200, is_screen=True)
    screen._backend = Host()
    report = asyncio.run(dispatch._run_actions_desktop(
        screen, [KeyPressAction(key="super"), WaitForAction(window="launcher", timeout=500)], None,
    ))
    assert "is visible" in report and "ERROR" not in report, report


def test_listed_windows_become_regions_of_the_screen_capture():
    """On Windows / macOS the capture starts at the leftmost monitor (x=-1920 here), so a window at
    desktop x=100 is region x=2020 — and one hanging off the right edge is clipped."""
    from galaius.server.tools_desktop import _capture_regions

    whole = DesktopWindow.screen("screen", _portable())
    text = _capture_regions(
        [HostWindow(handle=1, title="Notes", x=100, y=50, w=400, h=300),
         HostWindow(handle=2, title="Wide", x=1300, y=0, w=400, h=100)],
        whole,
    )
    assert "region=[2020, 50, 400, 300]" in text
    assert "region=[3220, 0, 140, 100]" in text
