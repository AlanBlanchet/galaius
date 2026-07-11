"""`target="screen"` / `"screen:<n>"` — whole-desktop and per-monitor capture/detection/input.

Multi-monitor correctness is the crux: a monitor target captures only its region (maim -g) and
its detected coords are region-relative, so input must add the monitor origin to land on the
right screen. These are display-free unit tests (xrandr/maim mocked); the real e2e is opt-in.
"""

from unittest.mock import MagicMock, patch

import pytest

from interact import server as srv
from interact.desktop import DesktopWindow, _SCREEN_WID


@pytest.fixture(autouse=True)
def _desktop_gate_open(monkeypatch):
    # These tests verify Linux desktop-resolution logic with mocked backends, so they run on every
    # CI OS (no display needed). Pin the Linux resolution path: force desktop_supported() True (so
    # _resolve_target doesn't take the macOS/Windows portable-screen branch on a mac/win runner) and
    # open the unsupported gate. The off-Linux behaviour is covered in test_cross_platform.py.
    monkeypatch.setattr("interact.desktop.backend.desktop_supported", lambda: True)
    monkeypatch.setattr(srv.targets, "_desktop_unsupported", lambda *a, **k: None)


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
    with patch("interact.desktop.subprocess.run", return_value=MagicMock(returncode=0)), \
         patch("interact.desktop.subprocess.check_output", return_value=b"PNG") as co:
        win.capture()
    assert co.call_args.args[0] == expected_cmd


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
    by hand because interact captured the wrong window."""
    win = DesktopWindow(name="App", wid=4242, x=0, y=0, w=100, h=100)
    order: list[str] = []
    monkeypatch.setattr(
        "interact.desktop.subprocess.run",
        lambda cmd, *a, **k: order.append(" ".join(map(str, cmd))) or MagicMock(returncode=0),
    )
    monkeypatch.setattr(
        "interact.desktop.subprocess.check_output",
        lambda cmd, *a, **k: order.append(" ".join(map(str, cmd))) or _nonblank_png(),
    )
    win.capture()
    raise_i = next(i for i, c in enumerate(order) if "windowactivate" in c and "4242" in c)
    grab_i = next(i for i, c in enumerate(order) if c.startswith("maim"))
    assert raise_i < grab_i, f"window must be raised before maim grabs it; order={order}"


def test_screen_target_capture_does_not_activate(monkeypatch):
    """A screen/monitor target has no window to raise — never call windowactivate for it."""
    with patch.object(DesktopWindow, "monitors", return_value=_MONS):
        mon = DesktopWindow.screen("screen:1")
    run = MagicMock(returncode=0)
    with patch("interact.desktop.subprocess.run", return_value=run) as r, \
         patch("interact.desktop.subprocess.check_output", return_value=_nonblank_png()):
        mon.capture()
    assert not any("windowactivate" in " ".join(map(str, c.args[0])) for c in r.call_args_list)


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

    with patch("interact.desktop.subprocess.run", fake_run), \
         patch("interact.desktop.subprocess.check_output", no_xdotool):
        win.capture_video(duration=1, fps=5)
    return run_cmds[0]


def test_capture_video_screen_target_uses_known_geometry(monkeypatch):
    with patch.object(DesktopWindow, "monitors", return_value=_MONS):
        mon = DesktopWindow.screen("screen:1")  # 1920x1080 at +2560,0
    ff = _run_capture_video(mon)
    assert "x11grab" in ff and "1920x1080" in ff
    assert ff[ff.index("-i") + 1] == ":0+2560,0"  # region origin = monitor origin


def test_capture_video_window_target_still_queries_xdotool():
    win = DesktopWindow(name="App", wid=4242, x=0, y=0, w=100, h=100)
    run_cmds: list[list[str]] = []

    def fake_co(cmd, *a, **k):  # the window path DOES read live geometry from xdotool
        assert cmd[:2] == ["xdotool", "getwindowgeometry"]
        return "WIDTH=800\nHEIGHT=600\nX=10\nY=20\n"

    with patch("interact.desktop.subprocess.run", lambda c, **k: run_cmds.append(c) or MagicMock()), \
         patch("interact.desktop.subprocess.check_output", fake_co):
        win.capture_video(duration=1, fps=5)
    ff = next(c for c in run_cmds if c[0] == "ffmpeg")  # skip the pre-record window-raise
    assert "800x600" in ff and ff[ff.index("-i") + 1] == ":0+10,20"


# --- #1.3: title targeting must prefer an exact match and refuse to silently guess between
#     several partial matches (e.g. "aino" vs "aino - Visual Studio Code"). ---


def _all(*windows):
    return classmethod(lambda cls, *a, **k: list(windows))


def test_exact_title_wins_over_a_larger_partial_match(monkeypatch):
    small = DesktopWindow(name="aino", wid=1, x=0, y=0, w=50, h=50)
    ide = DesktopWindow(name="aino - Visual Studio Code", wid=2, x=0, y=0, w=2000, h=1200)
    monkeypatch.setattr(DesktopWindow, "all", _all(ide, small))
    assert srv._find_desktop_window("aino") is small  # exact beats the bigger IDE window


def test_ambiguous_partial_matches_error_lists_candidates(monkeypatch):
    a = DesktopWindow(name="Chrome — Gmail", wid=1, x=0, y=0, w=100, h=100)
    b = DesktopWindow(name="Chrome — GitHub", wid=2, x=0, y=0, w=100, h=100)
    monkeypatch.setattr(DesktopWindow, "all", _all(a, b))
    out = srv._find_desktop_window("Chrome")
    assert isinstance(out, str) and "Gmail" in out and "GitHub" in out
    assert "2" in out  # tells the agent how many matched, so it can disambiguate


def test_single_partial_match_is_returned(monkeypatch):
    only = DesktopWindow(name="aino - Quiz", wid=1, x=0, y=0, w=100, h=100)
    monkeypatch.setattr(DesktopWindow, "all", _all(only))
    assert srv._find_desktop_window("aino") is only


def test_sole_editor_window_partial_match_is_not_silently_driven(monkeypatch):
    """10x in client logs: target='aino' matched ONLY the IDE window ('shared.rs - aino - Visual
    Studio Code') because the app itself ran in the sandbox — and the agent then typed into the
    user's editor. A lone PARTIAL match with an editor/terminal-pattern title needs explicit
    targeting (exact title or wid:), never a silent guess."""
    ide = DesktopWindow(name="shared.rs - aino - Visual Studio Code", wid=7, x=0, y=0, w=2000, h=1200)
    monkeypatch.setattr(DesktopWindow, "all", _all(ide))
    out = srv._find_desktop_window("aino")
    assert isinstance(out, str) and "wid:7" in out
    assert "nested" in out  # hints the app may be in the sandbox instead


def test_exact_editor_title_still_resolves(monkeypatch):
    """Explicitly naming the editor window (exact title) is intentional — never blocked."""
    ide = DesktopWindow(name="shared.rs - aino - Visual Studio Code", wid=7, x=0, y=0, w=2000, h=1200)
    monkeypatch.setattr(DesktopWindow, "all", _all(ide))
    assert srv._find_desktop_window("shared.rs - aino - Visual Studio Code") is ide


# --- #5 part 2: when no title is unique (an app titled "aino" is a substring of the IDE's
#     "aino - Visual Studio Code"), the window id is the only stable selector. ---


def test_listing_includes_window_id():
    app = DesktopWindow(name="aino", wid=29360135, x=0, y=0, w=464, h=1014)
    assert "wid:29360135" in DesktopWindow.listing([app])  # the id the user can copy to target


def test_target_by_window_id_selects_exactly(monkeypatch):
    app = DesktopWindow(name="aino", wid=29360135, x=0, y=0, w=464, h=1014)
    ide = DesktopWindow(name="aino - Visual Studio Code", wid=12, x=0, y=0, w=1920, h=1080)
    monkeypatch.setattr(DesktopWindow, "all", _all(ide, app))
    assert srv._find_desktop_window("wid:29360135") is app  # decimal
    assert srv._find_desktop_window("wid:0x1c00007") is app  # hex (== 29360135), as xwininfo prints


def test_unknown_window_id_errors_with_listing(monkeypatch):
    app = DesktopWindow(name="aino", wid=29360135, x=0, y=0, w=464, h=1014)
    monkeypatch.setattr(DesktopWindow, "all", _all(app))
    out = srv._find_desktop_window("wid:999")
    assert isinstance(out, str) and "999" in out and "aino" in out


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
