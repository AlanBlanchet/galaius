import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image as PILImage

from interact.desktop import (
    Box,
    CoordTransform,
    Cursor,
    DesktopElement,
    DesktopWindow,
    Motion,
)


@pytest.mark.asyncio
async def test_detection_discards_prior_screen_when_content_changes(monkeypatch):
    """A single-window app (Flutter/aino) keeps ONE window title across every screen, so re-detecting
    after navigating must DISCARD the prior screen's refs — not union them onto the new screenshot.
    Regression: home-screen refs (1..8) piled onto the quiz screen because the cache keyed off the
    (unchanging) window title."""
    import interact.desktop as desktop
    import interact.detect as detect

    def png(color):
        b = io.BytesIO()
        PILImage.new("RGB", (412, 915), color).save(b, "PNG")
        return b.getvalue()

    home_el = [DesktopElement(index=1, role="button", name="Quiz du jour", x=10, y=300, w=180, h=120)]
    quiz_el = [DesktopElement(index=1, role="button", name="le Tibre", x=10, y=600, w=380, h=60)]
    captures = iter([png((235, 235, 235)), png((20, 20, 60))])  # home, then a different screen

    win = MagicMock(wid=778899, w=412, h=915)
    win.name = "aino"  # NOT a MagicMock(name=) kwarg — that sets the mock's repr, not the attr
    win.capture = lambda: next(captures)
    monkeypatch.setattr(detect, "_desktop_context", lambda w: "ctx")
    monkeypatch.setattr(detect.CoordTransform, "from_xprop", staticmethod(lambda wid: CoordTransform()))
    monkeypatch.setattr(detect.CoordTransform, "store", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(detect.Debug, "save", lambda *a, **k: None)
    monkeypatch.setattr(detect, "_vlm_detect_elements",
                        AsyncMock(side_effect=[(home_el, 0.1, "", "vlm"), (quiz_el, 0.1, "", "vlm")]))
    desktop._element_cache.pop(778899, None)
    desktop._page_sig.pop(778899, None)

    _, first, *_ = await detect._detect_desktop_elements(win, method="vlm")
    _, second, *_ = await detect._detect_desktop_elements(win, method="vlm")
    assert [e.name for e in first] == ["Quiz du jour"]
    assert [e.name for e in second] == ["le Tibre"], "prior screen's elements were not discarded"


def test_page_signature_tracks_content_not_identity():
    """The page key must be deterministic, change with screen CONTENT, and never raise — it errs
    toward resetting (a new key) rather than ever keeping stale refs."""
    import interact.detect as detect

    def png(color):
        b = io.BytesIO()
        PILImage.new("RGB", (400, 400), color).save(b, "PNG")
        return b.getvalue()

    home, quiz = png((230, 230, 230)), png((20, 20, 60))
    assert detect._page_signature(home) == detect._page_signature(home)  # deterministic
    assert detect._page_signature(home) != detect._page_signature(quiz)  # content change → new key
    assert detect._page_signature(b"not a png")  # bad bytes → a hash, never an exception


def test_area_computed():
    win = DesktopWindow(name="test", wid=1, w=800, h=600, x=0, y=0)
    assert win.area == 480000


def test_window_listing_format():
    windows = [
        DesktopWindow(name="Zed", wid=2, w=1920, h=1080, x=0, y=0),
        DesktopWindow(name="Alacritty", wid=1, w=800, h=600, x=10, y=10),
    ]
    result = DesktopWindow.listing(windows)
    lines = result.split("\n")
    assert len(lines) == 2
    assert lines[0] == "  Alacritty (800x600, wid:1)"  # wid shown for exact targeting (#5)
    assert lines[1] == "  Zed (1920x1080, wid:2)"


def test_window_listing_empty():
    assert DesktopWindow.listing([]) == ""


def test_desktop_element_center():
    el = DesktopElement(index=1, x=100, y=200, w=80, h=40, role="button", name="OK")
    assert el.center_x == 140
    assert el.center_y == 220


def test_map_key_single():
    assert DesktopWindow.map_key("Enter") == "Return"
    assert DesktopWindow.map_key("ArrowDown") == "Down"
    assert DesktopWindow.map_key("Tab") == "Tab"
    assert DesktopWindow.map_key("a") == "a"


def test_map_key_combo():
    assert DesktopWindow.map_key("Control+a") == "ctrl+a"
    assert DesktopWindow.map_key("Control+Shift+ArrowUp") == "ctrl+shift+Up"
    assert DesktopWindow.map_key("Alt+F4") == "alt+F4"


def test_format_desktop_elements():
    elements = [
        DesktopElement(index=1, x=10, y=20, w=100, h=30, role="button", name="Save"),
        DesktopElement(index=2, x=120, y=20, w=100, h=30, role="button", name="Cancel"),
    ]
    result = DesktopElement.format_list(elements)
    # LLM-facing output: role/name only — no pixel coords (agents reference by index).
    assert "[1] button: 'Save'" in result
    assert "[2] button: 'Cancel'" in result
    assert "100x30" not in result
    assert "at 10,20" not in result


@pytest.mark.parametrize(
    "response, count",
    [
        ('elements: [{"role":"button","name":"OK","x":100,"y":200,"w":80,"h":40}]', 1),
        ("No elements found in this image.", 0),  # no JSON → None
        ("[{invalid json}]", 0),  # malformed → None
        ('[{"role":"b","name":"OK","x":10,"y":20,"w":30,"h":40},{"bad":true}]', 1),  # skips bad entry
    ],
    ids=["valid", "no-json", "malformed", "partial"],
)
def test_parse_vlm_elements_count(response, count):
    els = DesktopElement.parse_vlm(response)
    assert (len(els) if els else 0) == count


def test_parse_vlm_elements_fields():
    el = DesktopElement.parse_vlm(
        '[{"role":"button","name":"OK","x":100,"y":200,"w":80,"h":40}]'
    )[0]
    assert (el.role, el.name, el.x, el.center_x) == ("button", "OK", 100, 140)


def test_ref_to_index():
    assert DesktopElement.ref_to_index("e0") == 0
    assert DesktopElement.ref_to_index("e42") == 42
    assert DesktopElement.ref_to_index("e999") == 999


# --- Async desktop action tests ---


@pytest.fixture
def mock_run():
    with (
        patch("interact.desktop.DesktopWindow._run", new_callable=AsyncMock) as m,
        patch(
            "interact.desktop.DesktopWindow.active_id",
            new_callable=AsyncMock,
            return_value="60818159",
        ),
    ):
        yield m


@pytest.fixture
def _win():
    return DesktopWindow(name="test", wid=123, w=800, h=600, x=0, y=0)


@pytest.mark.parametrize("button", [1, 3], ids=["left", "right"])
@pytest.mark.asyncio
async def test_desktop_click_commands(mock_run, _win, button):
    await _win.click(50, 100, button=button)
    assert mock_run.call_count == 3
    mock_run.assert_any_call("xdotool", "windowactivate", "--sync", "123")
    mock_run.assert_any_call("xdotool", "mousemove", "--window", "123", "50", "100")
    mock_run.assert_any_call("xdotool", "click", str(button))


@pytest.mark.asyncio
async def test_desktop_type_commands(mock_run, _win):
    await _win.type_text("hello world")
    assert mock_run.call_count == 3
    mock_run.assert_any_call("xdotool", "windowactivate", "--sync", "123")
    mock_run.assert_any_call("xdotool", "windowfocus", "--sync", "123")
    mock_run.assert_any_call(
        "xdotool",
        "type",
        "--clearmodifiers",
        "--delay",
        "12",
        "--",
        "hello world",
    )


@pytest.mark.asyncio
async def test_desktop_key_commands(mock_run, _win):
    await _win.press_key("Enter")
    assert mock_run.call_count == 3
    mock_run.assert_any_call("xdotool", "windowactivate", "--sync", "123")
    mock_run.assert_any_call("xdotool", "windowfocus", "--sync", "123")
    mock_run.assert_any_call(
        "xdotool",
        "key",
        "--clearmodifiers",
        "--",
        "Return",
    )


@pytest.mark.asyncio
async def test_desktop_key_combo(mock_run, _win):
    await _win.press_key("Control+a")
    assert mock_run.call_count == 3
    mock_run.assert_any_call("xdotool", "windowactivate", "--sync", "123")
    mock_run.assert_any_call("xdotool", "windowfocus", "--sync", "123")
    mock_run.assert_any_call(
        "xdotool",
        "key",
        "--clearmodifiers",
        "--",
        "ctrl+a",
    )


@pytest.mark.parametrize(
    "direction, clicks, button",
    [("down", 3, "5"), ("up", 2, "4"), ("left", 2, "6"), ("right", 2, "7")],
    ids=["down", "up", "left", "right"],
)
@pytest.mark.asyncio
async def test_desktop_scroll(mock_run, _win, direction, clicks, button):
    await _win.scroll(50, 100, direction, clicks)
    mock_run.assert_any_call("xdotool", "mousemove", "--window", "123", "50", "100")
    scroll_calls = [c for c in mock_run.call_args_list if "click" in c.args]
    assert len(scroll_calls) == clicks
    for c in scroll_calls:
        assert c.args == ("xdotool", "click", "--window", "123", button)


@pytest.mark.parametrize(
    "direction, amount, expected",
    [
        ("up", 3, (3, False)),
        ("down", 2, (-2, False)),
        ("right", 4, (4, True)),     # horizontal must NOT collapse to a vertical button (#54)
        ("left", 1, (-1, True)),
    ],
    ids=["up", "down", "right", "left"],
)
@pytest.mark.asyncio
async def test_backend_scroll_threads_axis(direction, amount, expected):
    """A scroll on a sandbox-bound window must reach the backend with the right axis + sign. Before
    the fix, left/right fell into the vertical branch (clicks=-amount, no axis), so a Flutter
    horizontal carousel never advanced (#54)."""
    win = DesktopWindow(name="aino", wid=7, w=412, h=915, x=0, y=0)
    calls: list[tuple] = []

    class FakeBackend:
        def move(self, x, y):
            pass

        def focus_wid(self, wid):
            pass

        def scroll(self, clicks, horizontal=False):
            calls.append((clicks, horizontal))

    win._backend = FakeBackend()
    await win.scroll(100, 200, direction, amount)
    assert calls == [expected]


@pytest.mark.asyncio
async def test_desktop_drag_commands(mock_run, _win):
    await _win.drag(0, 0, 100, 100, steps=5)
    # activate + mousemove start + mousedown + 5 intermediate mousemoves + mouseup = 9
    assert mock_run.call_count == 9
    mock_run.assert_any_call("xdotool", "windowactivate", "--sync", "123")
    mock_run.assert_any_call("xdotool", "mousemove", "--window", "123", "0", "0")
    mock_run.assert_any_call("xdotool", "mousedown", "1")
    mock_run.assert_any_call("xdotool", "mouseup", "1")
    # verify intermediate coords (linear interpolation)
    for i in range(1, 6):
        expected_x = str(100 * i // 5)
        expected_y = str(100 * i // 5)
        mock_run.assert_any_call(
            "xdotool", "mousemove", "--window", "123", expected_x, expected_y
        )


@pytest.mark.asyncio
async def test_desktop_hover_commands(mock_run, _win):
    await _win.hover(200, 300)
    assert mock_run.call_count == 2
    mock_run.assert_any_call("xdotool", "windowactivate", "--sync", "123")
    mock_run.assert_any_call("xdotool", "mousemove", "--window", "123", "200", "300")


@pytest.mark.asyncio
async def test_mouse_no_focus_stealing(mock_run):
    w = DesktopWindow(name="test", wid=1, w=800, h=600, x=0, y=0)
    await w.scroll(0, 0, "down", 1)
    await w.drag(0, 0, 1, 1, steps=1)
    await w.hover(0, 0)
    restore_calls = [
        c
        for c in mock_run.call_args_list
        if c.args == ("xdotool", "windowactivate", "60818159")
    ]
    assert len(restore_calls) == 0


@pytest.mark.asyncio
async def test_keyboard_targets_window(mock_run):
    w = DesktopWindow(name="test", wid=1, w=800, h=600, x=0, y=0)
    await w.type_text("x")
    await w.press_key("a")
    activate_calls = [c for c in mock_run.call_args_list if "windowactivate" in c.args]
    focus_calls = [c for c in mock_run.call_args_list if "windowfocus" in c.args]
    assert len(activate_calls) == 2
    assert len(focus_calls) == 2


def test_detect_motion_ffmpeg_failure():
    with patch("interact.desktop.subprocess.run", side_effect=RuntimeError("boom")):
        assert Motion.detect(b"fake video") is True


def test_store_and_get_element():
    elements = [
        DesktopElement(index=1, x=10, y=20, w=30, h=40, role="button", name="OK"),
        DesktopElement(index=2, x=50, y=60, w=70, h=80, role="link", name="Help"),
    ]
    DesktopElement.store(999, elements)
    assert DesktopElement.get_by_index(999, 1) == elements[0]
    assert DesktopElement.get_by_index(999, 2) == elements[1]


def test_get_element_invalid_index():
    DesktopElement.store(888, [])
    assert DesktopElement.get_by_index(888, 1) is None
    assert DesktopElement.get_by_index(777, 5) is None


@pytest.mark.asyncio
async def test_run_raises_on_failure():
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"some error")
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(RuntimeError, match="xdotool failed"):
            await DesktopWindow._run("xdotool", "fake", "cmd")


# --- Cursor type detection tests ---


@pytest.mark.parametrize(
    "name,expected",
    [
        ("left_ptr", "default"),
        ("col-resize", "resize"),
        ("custom_cursor_abc", "custom_cursor_abc"),
        ("xterm", "text"),
    ],
    ids=["normal", "compound", "unknown", "text"],
)
def test_classify_cursor_name(name, expected):
    assert Cursor.classify(name) == expected


@pytest.mark.parametrize(
    "cursor_type,expected",
    [
        ("pointer", "clickable"),
        ("default", "normal"),
        ("custom_thing", "custom_thing"),
    ],
    ids=["clickable", "normal", "passthrough"],
)
def test_cursor_label(cursor_type, expected):
    assert Cursor.label(cursor_type) == expected


def test_get_cursor_type_no_libs():
    with patch("interact.desktop._libx11", None):
        assert Cursor.current_type() == "unknown"


def test_get_cursor_type_named_cursor():
    import ctypes

    mock_display = ctypes.c_void_p(1)
    mock_x11 = type(
        "FakeX11",
        (),
        {
            "XOpenDisplay": lambda self, _: mock_display,
            "XCloseDisplay": lambda self, _: None,
            "XFree": lambda self, _: None,
        },
    )()

    from interact.desktop import _XFixesCursorImage

    cursor_img = _XFixesCursorImage()
    cursor_img.name = b"hand2"
    cursor_img.width = 32
    cursor_img.height = 32
    cursor_ptr = ctypes.pointer(cursor_img)

    mock_xfixes = type(
        "FakeXFixes",
        (),
        {
            "XFixesGetCursorImage": lambda self, _: cursor_ptr,
        },
    )()

    with (
        patch("interact.desktop._libx11", mock_x11),
        patch("interact.desktop._libxfixes", mock_xfixes),
    ):
        assert Cursor.current_type() == "pointer"


def test_get_cursor_type_dimension_heuristic():
    import ctypes

    mock_display = ctypes.c_void_p(1)
    mock_x11 = type(
        "FakeX11",
        (),
        {
            "XOpenDisplay": lambda self, _: mock_display,
            "XCloseDisplay": lambda self, _: None,
            "XFree": lambda self, _: None,
        },
    )()

    from interact.desktop import _XFixesCursorImage

    cursor_img = _XFixesCursorImage()
    cursor_img.name = None
    cursor_img.width = 8
    cursor_img.height = 24
    cursor_ptr = ctypes.pointer(cursor_img)

    mock_xfixes = type(
        "FakeXFixes",
        (),
        {
            "XFixesGetCursorImage": lambda self, _: cursor_ptr,
        },
    )()

    with (
        patch("interact.desktop._libx11", mock_x11),
        patch("interact.desktop._libxfixes", mock_xfixes),
    ):
        assert Cursor.current_type() == "text"


def test_get_cursor_type_exception_fallback():
    mock_x11 = type(
        "FakeX11",
        (),
        {
            "XOpenDisplay": property(
                lambda self: (_ for _ in ()).throw(OSError("boom"))
            ),
        },
    )()
    with patch("interact.desktop._libx11", mock_x11):
        assert Cursor.current_type() == "unknown"


# --- IoU and fusion tests ---


def _el(
    x: int, y: int, w: int, h: int, role: str = "button", name: str = ""
) -> DesktopElement:
    return DesktopElement(index=1, x=x, y=y, w=w, h=h, role=role, name=name)


@pytest.mark.parametrize(
    "a, b, expected",
    [
        # Perfect overlap
        (_el(0, 0, 100, 100), _el(0, 0, 100, 100), 1.0),
        # No overlap
        (_el(0, 0, 50, 50), _el(100, 100, 50, 50), 0.0),
        # Partial overlap (50x50 intersection, union = 2*100*100 - 2500 = 17500)
        (_el(0, 0, 100, 100), _el(50, 50, 100, 100), 2500 / 17500),
        # Zero-area element
        (_el(10, 10, 0, 0), _el(10, 10, 100, 100), 0.0),
        # Contained element (50x50 inside 100x100)
        (_el(25, 25, 50, 50), _el(0, 0, 100, 100), 2500 / 10000),
    ],
    ids=["perfect", "disjoint", "partial", "zero-area", "contained"],
)
def test_iou(a, b, expected):
    assert abs(a.iou(b) - expected) < 1e-6


@pytest.mark.parametrize(
    "method",
    [
        lambda el: el.clamp(1000, 1000),
        lambda el: el.scale(2.0, 2.0),
        lambda el: el.translate(10, 20),
        lambda el: el.transform(
            CoordTransform(scale_x=1.5, scale_y=1.5, crop_x=5, crop_y=10)
        ),
    ],
    ids=["clamp", "scale", "translate", "transform"],
)
def test_box_methods_preserve_subclass(method):
    el = DesktopElement(index=5, x=100, y=200, w=80, h=40, role="button", name="Save")
    result = method(el)
    assert isinstance(result, DesktopElement)
    assert result.index == 5
    assert result.role == "button"
    assert result.name == "Save"


def test_fuse_elements_snaps_bbox():
    vlm = [_el(10, 20, 80, 30, "button", "Save")]
    atspi = [_el(12, 18, 82, 32, "push-button", "Save Button")]
    fused = DesktopElement.fuse(vlm, atspi)
    assert len(fused) == 1
    # VLM role/name kept, AT-SPI geometry used
    assert fused[0].role == "button"
    assert fused[0].name == "Save"
    assert fused[0].x == 12
    assert fused[0].y == 18
    assert fused[0].w == 82
    assert fused[0].h == 32


def test_fuse_elements_low_iou_keeps_both():
    """Non-overlapping elements: VLM kept as-is + AT-SPI appended as unmatched."""
    vlm = [_el(0, 0, 50, 50, "button", "A")]
    atspi = [_el(500, 500, 50, 50, "push-button", "B")]
    fused = DesktopElement.fuse(vlm, atspi)
    assert len(fused) == 2
    assert fused[0].x == 0
    assert fused[0].name == "A"
    assert fused[1].x == 500
    assert fused[1].name == "B"
    assert fused[1].index == 2


def test_fuse_elements_inherits_atspi_name_when_vlm_empty():
    vlm = [_el(10, 10, 100, 100, "button", "")]
    atspi = [_el(10, 10, 100, 100, "push-button", "Toolbar Save")]
    fused = DesktopElement.fuse(vlm, atspi)
    assert fused[0].name == "Toolbar Save"


def test_fuse_elements_reindexes():
    vlm = [
        _el(0, 0, 50, 50, "button", "A"),
        _el(100, 100, 50, 50, "link", "B"),
    ]
    atspi = [_el(0, 0, 50, 50, "push-button", "X")]
    fused = DesktopElement.fuse(vlm, atspi)
    assert [f.index for f in fused] == [1, 2]


# --- CoordTransform coordinate transforms ---


@pytest.mark.parametrize(
    "offsets,x,y,method,expected",
    [
        # screenshot_to_xdotool: adds shadow offsets
        (
            CoordTransform(
                shadow_left=26,
                shadow_right=26,
                shadow_top=9,
                shadow_bottom=43,
                decoration_top=37,
            ),
            100,
            87,
            "screenshot_to_xdotool",
            (126, 96),
        ),
        (CoordTransform(), 100, 50, "screenshot_to_xdotool", (100, 50)),
    ],
    ids=[
        "screen2xdo-typical",
        "screen2xdo-zero",
    ],
)
def test_coord_transform_frame_offsets(offsets, x, y, method, expected):
    assert getattr(offsets, method)(x, y) == expected


# --- CoordTransform.from_xprop parsing ---


_XPROP_GTK_AND_MUTTER = """\
WM_STATE(WM_STATE):
		window state: Normal
_GTK_FRAME_EXTENTS(CARDINAL) = 26, 26, 9, 43
_NET_WM_STATE(ATOM) = _NET_WM_STATE_FOCUSED
_MUTTER_FRAME_EXTENTS(CARDINAL) = 0, 0, 37, 0
WM_NAME(UTF8_STRING) = "Test"
"""

_XPROP_GTK_ONLY = """\
_GTK_FRAME_EXTENTS(CARDINAL) = 10, 10, 5, 20
WM_NAME(UTF8_STRING) = "Test"
"""

_XPROP_NONE = """\
WM_STATE(WM_STATE):
		window state: Normal
WM_NAME(UTF8_STRING) = "Test"
"""


@pytest.mark.parametrize(
    "xprop_output,expected",
    [
        (
            _XPROP_GTK_AND_MUTTER,
            CoordTransform(
                shadow_left=26,
                shadow_right=26,
                shadow_top=9,
                shadow_bottom=43,
                decoration_top=37,
            ),
        ),
        (
            _XPROP_GTK_ONLY,
            CoordTransform(
                shadow_left=10,
                shadow_right=10,
                shadow_top=5,
                shadow_bottom=20,
                decoration_top=0,
            ),
        ),
        (_XPROP_NONE, CoordTransform()),
    ],
    ids=["gtk+mutter", "gtk-only", "no-properties"],
)
def test_from_xprop_parsing(xprop_output, expected):
    with patch(
        "interact.desktop.subprocess.check_output", return_value=xprop_output
    ):
        assert CoordTransform.from_xprop(12345) == expected


def test_from_xprop_subprocess_failure():
    with patch(
        "interact.desktop.subprocess.check_output", side_effect=FileNotFoundError
    ):
        assert CoordTransform.from_xprop(12345) == CoordTransform()


# --- Offset cache behavior ---


def test_store_get_has_offsets():
    wid = 99999
    assert not CoordTransform.has(wid)
    assert CoordTransform.get(wid) == CoordTransform()

    offsets = CoordTransform(shadow_left=10, shadow_top=5, decoration_top=20)
    CoordTransform.store(wid, offsets)
    assert CoordTransform.has(wid)
    assert CoordTransform.get(wid) == offsets


# --- Desktop actions apply offsets ---


@pytest.fixture
def mock_run_with_offsets():
    """Like mock_run but pre-stores non-zero offsets for wid 123."""
    CoordTransform.store(
        123,
        CoordTransform(
            shadow_left=26,
            shadow_right=26,
            shadow_top=9,
            shadow_bottom=43,
            decoration_top=37,
        ),
    )
    with (
        patch("interact.desktop.DesktopWindow._run", new_callable=AsyncMock) as m,
        patch(
            "interact.desktop.DesktopWindow.active_id",
            new_callable=AsyncMock,
            return_value="60818159",
        ),
    ):
        yield m


@pytest.mark.asyncio
async def test_desktop_click_applies_offsets(mock_run_with_offsets, _win):
    await _win.click(100, 87)
    # screenshot_to_xdotool: (100+26, 87+9) = (126, 96)
    mock_run_with_offsets.assert_any_call(
        "xdotool", "mousemove", "--window", "123", "126", "96"
    )


@pytest.mark.asyncio
async def test_desktop_scroll_applies_offsets(mock_run_with_offsets, _win):
    await _win.scroll(100, 87, "down", 1)
    mock_run_with_offsets.assert_any_call(
        "xdotool", "mousemove", "--window", "123", "126", "96"
    )


@pytest.mark.asyncio
async def test_desktop_drag_applies_offsets(mock_run_with_offsets, _win):
    await _win.drag(100, 87, 200, 187, steps=1)
    # from: (100+26, 87+9) = (126, 96), to: (200+26, 187+9) = (226, 196)
    mock_run_with_offsets.assert_any_call(
        "xdotool", "mousemove", "--window", "123", "126", "96"
    )
    mock_run_with_offsets.assert_any_call(
        "xdotool", "mousemove", "--window", "123", "226", "196"
    )


@pytest.mark.asyncio
async def test_desktop_hover_applies_offsets(mock_run_with_offsets, _win):
    await _win.hover(100, 87)
    mock_run_with_offsets.assert_any_call(
        "xdotool", "mousemove", "--window", "123", "126", "96"
    )


# --- CoordTransform VLM scaling + crop ---


@pytest.mark.parametrize(
    "scale_x,scale_y,crop_x,crop_y,x,y,w,h,expected",
    [
        # Identity
        (1.0, 1.0, 0, 0, 100, 200, 80, 30, (100, 200, 80, 30)),
        # Scale only (1.5x)
        (1.5, 1.5, 0, 0, 100, 200, 80, 30, (150, 300, 120, 45)),
        # Crop only
        (1.0, 1.0, 50, 30, 100, 200, 80, 30, (150, 230, 80, 30)),
        # Scale + crop
        (1.5, 1.5, 50, 30, 100, 200, 80, 30, (200, 330, 120, 45)),
    ],
    ids=["identity", "scale-only", "crop-only", "scale-and-crop"],
)
def test_vlm_to_screenshot(scale_x, scale_y, crop_x, crop_y, x, y, w, h, expected):
    t = CoordTransform(scale_x=scale_x, scale_y=scale_y, crop_x=crop_x, crop_y=crop_y)
    box = Box(x=x, y=y, w=w, h=h)
    result = box.transform(t)
    assert (result.x, result.y, result.w, result.h) == expected


@pytest.mark.parametrize(
    "x,y,w,h,img_w,img_h,expected",
    [
        # Fully inside
        (100, 200, 80, 30, 1920, 1080, (100, 200, 80, 30)),
        # Partially outside right edge
        (1880, 200, 100, 30, 1920, 1080, (1880, 200, 40, 30)),
        # Partially outside bottom
        (100, 1060, 80, 40, 1920, 1080, (100, 1060, 80, 20)),
        # Negative coords clamped
        (-10, -5, 30, 20, 1920, 1080, (0, 0, 20, 15)),
        # Fully outside
        (2000, 2000, 50, 50, 1920, 1080, None),
    ],
    ids=["inside", "right-edge", "bottom-edge", "negative", "outside"],
)
def test_coord_transform_clamp(x, y, w, h, img_w, img_h, expected):
    box = Box(x=x, y=y, w=w, h=h)
    result = box.clamp(img_w, img_h)
    if expected is None:
        assert result is None
    else:
        assert (result.x, result.y, result.w, result.h) == expected


@pytest.mark.parametrize(
    "w, h, sx",
    [
        (900, 700, 1.0),  # between bounds → no scaling
        (1920, 1080, 1920 / 1280),  # over max_dim → downscale
        (400, 383, 400 / 768),  # under min_dim → upscale
    ],
    ids=["noop", "downscale", "upscale"],
)
def test_for_resize_scale(w, h, sx):
    t = CoordTransform.for_resize(w, h, max_dim=1280, min_dim=768)
    assert t.scale_x == pytest.approx(sx)
    assert t.scale_y == t.scale_x


def test_with_crop():
    t = CoordTransform.for_resize(1920, 1080)
    tc = t.with_crop(50, 30)
    assert tc.crop_x == 50
    assert tc.crop_y == 30
    assert tc.scale_x == t.scale_x
    assert tc.scale_y == t.scale_y


@pytest.mark.parametrize(
    "orig, expected, noop",
    [
        ((800, 600), (800, 600), True),  # default transform → unchanged, same bytes
        ((1920, 1080), (1280, 720), False),  # downscale to max_dim
        ((400, 383), (768, 735), False),  # upscale to min_dim
    ],
    ids=["noop", "downscale", "upscale"],
)
def test_resize_image(orig, expected, noop):
    from PIL import Image as PILImage
    import io

    buf = io.BytesIO()
    PILImage.new("RGB", orig, color="red").save(buf, format="PNG")
    png = buf.getvalue()
    t = CoordTransform() if noop else CoordTransform.for_resize(*orig, max_dim=1280, min_dim=768)
    result, w, h = t.resize_image(png, *orig)
    assert (w, h) == expected
    if noop:
        assert result is png  # untouched, no re-encode
    else:
        assert PILImage.open(io.BytesIO(result)).size == expected


def test_merge_into_accumulates_within_page_and_clears_on_change():
    """Detections accumulate (refs add up) while the page signature is stable, and clear
    when it changes — the per-window ref session behaviour."""
    from interact.desktop import DesktopElement, _element_cache, _page_sig

    wid = 987654
    _element_cache.pop(wid, None)
    _page_sig.pop(wid, None)

    def el(x: int, name: str) -> DesktopElement:
        return DesktopElement.from_vlm_dict({"x": x, "y": 0, "w": 10, "h": 10, "role": "button", "name": name}, 1)

    first = DesktopElement.merge_into(wid, [el(0, "a")], "page A")
    assert [e.name for e in first] == ["a"]

    # same page → a second/targeted detect ADDS to the existing refs (non-overlapping), re-indexed
    second = DesktopElement.merge_into(wid, [el(100, "b")], "page A")
    assert [e.name for e in second] == ["a", "b"]
    assert [e.index for e in second] == [1, 2]
    assert DesktopElement.get_by_index(wid, 2).name == "b"

    # page change → stale refs cleared, only the new detection remains
    third = DesktopElement.merge_into(wid, [el(0, "c")], "page B")
    assert [e.name for e in third] == ["c"]

    _element_cache.pop(wid, None)
    _page_sig.pop(wid, None)
