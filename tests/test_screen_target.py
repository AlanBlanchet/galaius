"""`target="screen"` / `"screen:<n>"` — whole-desktop and per-monitor capture/detection/input.

Multi-monitor correctness is the crux: a monitor target captures only its region (maim -g) and
its detected coords are region-relative, so input must add the monitor origin to land on the
right screen. These are display-free unit tests (xrandr/maim mocked); the real e2e is opt-in.
"""

from unittest.mock import patch

import pytest

from interact import server as srv
from interact.desktop import DesktopWindow, _SCREEN_WID

_XRANDR = """Monitors: 2
 0: +*DP-1 2560/598x1440/336+0+0  DP-1
 1: +HDMI-1 1920/509x1080/286+2560+0  HDMI-1
"""
_MONS = [
    {"index": 0, "name": "DP-1", "w": 2560, "h": 1440, "x": 0, "y": 0},
    {"index": 1, "name": "HDMI-1", "w": 1920, "h": 1080, "x": 2560, "y": 0},
]


def test_monitors_parses_index_geometry_and_clean_output_name():
    with patch("interact.desktop.subprocess.check_output", return_value=_XRANDR):
        assert DesktopWindow.monitors() == _MONS


def test_monitors_empty_when_xrandr_missing():
    with patch("interact.desktop.subprocess.check_output", side_effect=FileNotFoundError):
        assert DesktopWindow.monitors() == []


def test_screen_whole_is_bounding_box_of_all_monitors():
    with patch.object(DesktopWindow, "monitors", return_value=_MONS):
        whole = DesktopWindow.screen("screen")
    assert whole.is_screen and whole.screen_geometry == ""  # "" → bare maim, the full root
    assert (whole.w, whole.h) == (4480, 1440) and whole.wid == _SCREEN_WID


@pytest.mark.parametrize("spec", ["screen:1", "screen:HDMI-1"])
def test_screen_by_index_or_output_name(spec):
    with patch.object(DesktopWindow, "monitors", return_value=_MONS):
        mon = DesktopWindow.screen(spec)
    assert mon.screen_geometry == "1920x1080+2560+0"
    assert mon.wid != _SCREEN_WID  # distinct cache key per monitor (no ref-cache collision)


def test_screen_unknown_monitor_lists_available():
    with patch.object(DesktopWindow, "monitors", return_value=_MONS):
        err = DesktopWindow.screen("screen:9")
    assert isinstance(err, str) and "0:DP-1" in err and "1:HDMI-1" in err


def test_monitor_input_maps_by_region_origin():
    """A coord detected at (10,10) on the right-hand monitor must click at absolute (2570,10)."""
    with patch.object(DesktopWindow, "monitors", return_value=_MONS):
        mon = DesktopWindow.screen("screen:1")
    assert mon._input_xy(10, 10) == (2570, 10)


def test_window_input_unaffected_uses_coordtransform():
    win = DesktopWindow(name="App", wid=123, x=5, y=5, w=100, h=100)
    assert not win.is_screen
    assert win._input_xy(10, 10) == win._to_xdotool(10, 10)  # window path unchanged


@pytest.mark.parametrize(
    "win_kwargs, expected_cmd",
    [
        ({"screen_geometry": ""}, ["maim"]),  # whole virtual screen
        ({"screen_geometry": "1920x1080+2560+0"}, ["maim", "-g", "1920x1080+2560+0"]),  # monitor
        ({}, ["maim", "-i", "123"]),  # ordinary window
    ],
)
def test_capture_command_per_target(win_kwargs, expected_cmd):
    win = DesktopWindow(name="t", wid=123, x=0, y=0, w=10, h=10, **win_kwargs)
    with patch("interact.desktop.subprocess.check_output", return_value=b"PNG") as co:
        win.capture()
    assert co.call_args.args[0] == expected_cmd


@pytest.mark.asyncio
async def test_screen_target_input_is_absolute_not_window_scoped(monkeypatch):
    """A screen/monitor target has no real window — input must use absolute pointer positioning,
    never `xdotool --window <synthetic-wid>` (which fails). Regression for the screen-input bug."""
    from unittest.mock import AsyncMock

    with patch.object(DesktopWindow, "monitors", return_value=_MONS):
        mon = DesktopWindow.screen("screen:1")
    run, xdo = AsyncMock(), AsyncMock()
    monkeypatch.setattr(DesktopWindow, "_run", run)
    monkeypatch.setattr(DesktopWindow, "_xdo", xdo)

    await mon.click(10, 10)

    assert not xdo.called, "screen target must not scope input to a (synthetic) window id"
    moves = [c.args for c in run.call_args_list if "mousemove" in c.args]
    assert moves == [("xdotool", "mousemove", "2570", "10")]  # absolute, region-origin mapped


@pytest.mark.asyncio
async def test_window_target_input_stays_window_relative(monkeypatch):
    """A real window keeps window-relative input (--window <wid>) — unchanged behaviour."""
    from unittest.mock import AsyncMock

    win = DesktopWindow(name="App", wid=4242, x=0, y=0, w=100, h=100)
    monkeypatch.setattr(DesktopWindow, "_run", AsyncMock())
    monkeypatch.setattr(DesktopWindow, "_xdo", AsyncMock())
    monkeypatch.setattr(
        "interact.desktop.CoordTransform.get",
        lambda wid: __import__("interact.desktop", fromlist=["CoordTransform"]).CoordTransform(),
    )
    await win.hover(5, 6)
    DesktopWindow._xdo.assert_awaited()  # window path uses --window via _xdo
    assert DesktopWindow._xdo.await_args.args[0] == 4242


def test_blank_gpu_surface_capture_raises_actionable_error(monkeypatch):
    """An Android-emulator / GPU-surface window grabs uniform black via X — don't hand back a
    black image; raise a clear error naming the cause + the adb/compositor fixes."""
    import io
    from PIL import Image as PILImage
    from interact.desktop import CaptureError

    buf = io.BytesIO()
    PILImage.new("RGB", (40, 40), "black").save(buf, format="PNG")
    black = buf.getvalue()

    def fake(cmd, *a, **k):  # maim → black; xdotool geometry → a valid region
        if cmd[0] == "xdotool":
            return "WIDTH=388\nHEIGHT=863\nX=0\nY=0\n"
        return black

    monkeypatch.setattr("interact.desktop.subprocess.check_output", fake)
    win = DesktopWindow(name="Android Emulator - Pixel_7:5554", wid=123, x=0, y=0, w=388, h=863)
    with pytest.raises(CaptureError) as exc:
        win.capture()
    msg = str(exc.value)
    assert "GPU" in msg and "adb" in msg and "Android Emulator" in msg


def test_resolve_target_routes_screen_to_screen_builder(monkeypatch):
    sentinel = DesktopWindow(name="screen", wid=_SCREEN_WID, x=0, y=0, w=1, h=1, screen_geometry="")
    monkeypatch.setattr(DesktopWindow, "screen", classmethod(lambda cls, spec: sentinel))
    win, mgr, err = srv._resolve_target("screen", "default")
    assert win is sentinel and mgr is None and err is None
    win, mgr, err = srv._resolve_target("screen:0", "default")
    assert win is sentinel
