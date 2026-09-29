"""`target="screen"` / `"screen:<n>"` — whole-desktop and per-monitor capture/detection/input.

Multi-monitor correctness is the crux: a monitor target captures only its region and its
detected coords are region-relative, so input must add the monitor origin to land on the right
screen. Display-free: the host desktop backend is a double; the real run is
tests/test_host_desktop_live.py.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from galaius import server as srv
from galaius.desktop import DesktopWindow, _SCREEN_WID
from galaius.desktop.geometry import Monitor

pytestmark = pytest.mark.usefixtures("desktop_gate_open")


class _Host:
    """A host-desktop backend double: two monitors side by side, every call recorded."""

    display = ":0"

    def __init__(self):
        self.calls: list[tuple] = []

    def monitors(self):
        return [
            Monitor(index=0, name="DP-1", x=0, y=0, w=2560, h=1440),
            Monitor(index=1, name="HDMI-1", x=2560, y=0, w=1920, h=1080),
        ]

    def capture(self):
        self.calls.append(("capture",))
        return b"PNG"

    def capture_region(self, x, y, w, h):
        self.calls.append(("capture_region", x, y, w, h))
        return b"PNG"

    def click(self, x, y, button="left", count=1):
        self.calls.append(("click", x, y, button, count))

    def move(self, x, y):
        self.calls.append(("move", x, y))


def test_screen_whole_is_bounding_box_of_all_monitors():
    whole = DesktopWindow.screen("screen", _Host())
    assert whole.is_screen
    assert (whole.w, whole.h) == (4480, 1440) and whole.wid == _SCREEN_WID


@pytest.mark.parametrize("spec", ["screen:1", "screen:HDMI-1"])
def test_screen_by_index_or_output_name(spec):
    mon = DesktopWindow.screen(spec, _Host())
    assert (mon.x, mon.y, mon.w, mon.h) == (2560, 0, 1920, 1080)
    assert mon.wid != _SCREEN_WID  # distinct cache key per monitor (no ref-cache collision)


def test_screen_unknown_monitor_lists_available():
    err = DesktopWindow.screen("screen:9", _Host())
    assert isinstance(err, str) and "0:DP-1" in err and "1:HDMI-1" in err


@pytest.mark.parametrize(
    "spec, expected",
    [("screen", ("capture_region", 0, 0, 4480, 1440)), ("screen:1", ("capture_region", 2560, 0, 1920, 1080))],
)
def test_screen_capture_goes_through_the_host_backend(spec, expected):
    host = _Host()
    DesktopWindow.screen(spec, host).capture()
    assert host.calls == [expected]


@pytest.mark.asyncio
async def test_monitor_input_maps_by_region_origin():
    """A coord detected at (10,10) on the right-hand monitor must click at absolute (2570,10),
    through the backend — never `xdotool --window <synthetic-wid>`."""
    host = _Host()
    await DesktopWindow.screen("screen:1", host).click(10, 10)
    assert host.calls == [("click", 2570, 10, "left", 1)]


def test_window_input_unaffected_uses_coordtransform():
    win = DesktopWindow(name="App", wid=123, x=5, y=5, w=100, h=100)
    assert not win.is_screen
    assert win._input_xy(10, 10) == win._to_xdotool(10, 10)  # window path unchanged


def test_window_capture_grabs_by_window_id():
    win = DesktopWindow(name="t", wid=123, x=0, y=0, w=10, h=10)
    with patch("galaius.desktop.subprocess.run", return_value=MagicMock(returncode=0)), \
         patch("galaius.desktop.subprocess.check_output", return_value=b"PNG") as co:
        win.capture()
    assert co.call_args.args[0] == ["maim", "-i", "123"]


def _nonblank_png() -> bytes:
    import io
    from PIL import Image as PILImage

    img = PILImage.new("RGB", (8, 8), "black")
    img.putpixel((0, 0), (255, 255, 255))  # non-uniform → not treated as a blank GPU surface
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_capture_raises_window_before_grabbing(monkeypatch):
    """A window the user moved or buried must be brought to the front before capture, or maim
    grabs whatever occludes it. This is the consumer red flag: an agent had to xdotool-activate
    by hand because galaius captured the wrong window."""
    win = DesktopWindow(name="App", wid=4242, x=0, y=0, w=100, h=100)
    order: list[str] = []
    monkeypatch.setattr(
        "galaius.desktop.subprocess.run",
        lambda cmd, *a, **k: order.append(" ".join(map(str, cmd))) or MagicMock(returncode=0),
    )
    monkeypatch.setattr(
        "galaius.desktop.subprocess.check_output",
        lambda cmd, *a, **k: order.append(" ".join(map(str, cmd))) or _nonblank_png(),
    )
    win.capture()
    raise_i = next(i for i, c in enumerate(order) if "windowactivate" in c and "4242" in c)
    grab_i = next(i for i, c in enumerate(order) if c.startswith("maim"))
    assert raise_i < grab_i, f"window must be raised before maim grabs it; order={order}"


@pytest.mark.asyncio
async def test_window_target_input_stays_window_relative(monkeypatch):
    """A real window keeps window-relative input (--window <wid>) — unchanged behaviour."""
    from unittest.mock import AsyncMock

    win = DesktopWindow(name="App", wid=4242, x=0, y=0, w=100, h=100)
    monkeypatch.setattr(DesktopWindow, "_run", AsyncMock())
    monkeypatch.setattr(DesktopWindow, "_xdo", AsyncMock())
    monkeypatch.setattr(
        "galaius.desktop.CoordTransform.get",
        lambda wid: __import__("galaius.desktop", fromlist=["CoordTransform"]).CoordTransform(),
    )
    await win.hover(5, 6)
    DesktopWindow._xdo.assert_awaited()  # window path uses --window via _xdo
    assert DesktopWindow._xdo.await_args.args[0] == 4242




def test_resolve_target_routes_screen_to_screen_builder(monkeypatch):
    sentinel = DesktopWindow(name="screen", wid=_SCREEN_WID, x=0, y=0, w=1, h=1, is_screen=True)
    monkeypatch.setattr(srv.targets, "host_desktop", lambda: _Host())
    monkeypatch.setattr(DesktopWindow, "screen", classmethod(lambda cls, spec, host: sentinel))
    win, mgr, err = srv._resolve_target("screen", "default")
    assert win is sentinel and mgr is None and err is None
    win, mgr, err = srv._resolve_target("screen:0", "default")
    assert win is sentinel


# --- #3: record(target="screen:N") must grab the monitor by its known geometry, never ask
#     xdotool for the geometry of the synthetic screen wid (which crashed with exit 1). ---


def _run_capture_video(win):
    """Run capture_video with ffmpeg/xdotool stubbed; return the ffmpeg argv it built."""
    run_cmds: list[list[str]] = []

    def fake_run(cmd, **k):
        run_cmds.append(cmd)
        return MagicMock(returncode=0)

    def no_xdotool(cmd, *a, **k):
        if cmd and cmd[0] == "xdotool":
            raise AssertionError("a screen target must not query xdotool for geometry")
        return "WIDTH=800\nHEIGHT=600\nX=10\nY=20\n"

    with patch("galaius.desktop.subprocess.run", fake_run), \
         patch("galaius.desktop.subprocess.check_output", no_xdotool):
        win.capture_video(duration=1, fps=5)
    return run_cmds[0]


def test_capture_video_screen_target_uses_known_geometry(monkeypatch):
    mon = DesktopWindow.screen("screen:1", _Host())  # 1920x1080 at +2560,0
    ff = _run_capture_video(mon)
    assert "x11grab" in ff and "1920x1080" in ff
    assert ff[ff.index("-i") + 1] == ":0+2560,0"  # region origin = monitor origin


def test_capture_video_window_target_still_queries_xdotool(monkeypatch):
    """A window is recorded on the session's own display, not a hard-coded ``:0``."""
    monkeypatch.setenv("DISPLAY", ":7")
    win = DesktopWindow(name="App", wid=4242, x=0, y=0, w=100, h=100)
    run_cmds: list[list[str]] = []

    def fake_co(cmd, *a, **k):  # the window path DOES read live geometry from xdotool
        assert cmd[:2] == ["xdotool", "getwindowgeometry"]
        return "WIDTH=800\nHEIGHT=600\nX=10\nY=20\n"

    with patch("galaius.desktop.subprocess.run", lambda c, **k: run_cmds.append(c) or MagicMock()), \
         patch("galaius.desktop.subprocess.check_output", fake_co):
        win.capture_video(duration=1, fps=5)
    ff = next(c for c in run_cmds if c[0] == "ffmpeg")  # skip the pre-record window-raise
    assert "800x600" in ff and ff[ff.index("-i") + 1] == ":7+10,20"


# --- headless/dedicated env: target="nested[:title]" drives an app in the isolated sandbox,
#     non-intrusive and occlusion-proof — the fix for a window that fought the user's WM. ---


class _FakeSandbox:
    screen_w, screen_h = 640, 480

    def list_windows(self):
        return [(1, "xclock")]


def test_resolve_nested_whole_screen_binds_the_sandbox(monkeypatch):
    monkeypatch.setattr(srv.sandbox, "_get_sandbox", lambda: _FakeSandbox())
    win, mgr, err = srv._resolve_target("nested", "default")
    assert err is None and mgr is None
    assert win.name == "sandbox" and (win.w, win.h) == (640, 480)
    assert win._backend is not None  # capture/input route through the sandbox, not the real display


def test_resolve_nested_titled_window(monkeypatch):
    sentinel = DesktopWindow(name="xclock", wid=5, x=0, y=0, w=164, h=164)
    monkeypatch.setattr(srv.sandbox, "_get_sandbox", lambda: _FakeSandbox())
    monkeypatch.setattr(
        DesktopWindow, "find_in",
        classmethod(lambda cls, be, title: sentinel if title == "xclock" else None),
    )
    win, mgr, err = srv._resolve_target("nested:xclock", "default")
    assert win is sentinel and err is None


def test_resolve_nested_unknown_title_lists_sandbox_windows(monkeypatch):
    monkeypatch.setattr(srv.sandbox, "_get_sandbox", lambda: _FakeSandbox())
    monkeypatch.setattr(DesktopWindow, "find_in", classmethod(lambda cls, be, title: None))
    win, mgr, err = srv._resolve_target("nested:missing", "default")
    assert win is None and isinstance(err, str) and "xclock" in err


def test_a_capture_failure_reaches_the_agent_as_an_ERROR_string(monkeypatch):
    """galaius's whole tool contract is "a short prose summary, errors prefixed ERROR: so an
    agent can branch" (CLAUDE.md). A CaptureError RAISED instead becomes a transport-level error:
    the text still arrives, but not in the shape every other failure takes, so an agent testing
    `result.startswith("ERROR:")` sees an exception where it expected a string it can read.

    Fixed at the one seam every tool already passes through rather than per tool, because the next
    capture-taking tool would otherwise have to remember.
    """
    import asyncio

    from galaius.desktop import CaptureError, DesktopWindow

    win = DesktopWindow(name="doomed", wid=9, x=0, y=0, w=10, h=10)

    def boom(self):
        raise CaptureError('Could not capture: stale. Try target="screen".')

    monkeypatch.setattr(DesktopWindow, "capture", boom)
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (win, None, None))
    fn = getattr(srv.screenshot, "fn", srv.screenshot)
    out = asyncio.run(fn(target="doomed"))
    assert isinstance(out, str), "a capture failure must be a readable result, not an exception"
    assert out.startswith("ERROR:"), out
    assert 'target="screen"' in out, "the actionable half must survive the wrapping"
