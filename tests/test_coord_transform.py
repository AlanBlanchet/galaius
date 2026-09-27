"""The desktop coordinate stack: CoordTransform frame offsets, xprop-derived decoration/shadow
parsing, the offset cache, desktop actions applying those offsets, VLM-scaling/crop/resize math,
and the Frame tree (screen → monitor → window → captured image) every x/y travels through."""

from unittest.mock import AsyncMock, patch

import pytest

from interact.desktop import Box, CoordTransform
from interact.desktop.frames import Frame
from tests.support.desktop import desktop_window


@pytest.fixture
def _win():
    return desktop_window()


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


# =============================================================================================
# Frame math — the coordinate stack every desktop action's x/y travels through
# =============================================================================================


@pytest.fixture
def frame_stack():
    """screen → monitor(+1920,+0) → window(+50,+80) → image(window captured @2x for the VLM)."""
    screen = Frame(name="screen")
    monitor = screen.child("monitor1", offset_x=1920, offset_y=0)
    window = monitor.child("window", offset_x=50, offset_y=80)
    # image is the window captured then upscaled 2x (so 1 image px = 0.5 window px)
    image = window.child("image", scale_x=0.5, scale_y=0.5)
    return screen, monitor, window, image


class TestFrame:
    def test_window_to_screen_adds_monitor_and_window_offsets(self, frame_stack):
        _screen, _monitor, window, _image = frame_stack
        # (10,10) in the window sits at monitor (1920+50+10, 80+10)
        assert window.to_root(10, 10) == (1980, 90)

    def test_image_to_window_undoes_resize(self, frame_stack):
        _s, _m, window, image = frame_stack
        # (200,200) in the 2x image → (100,100) in the window
        assert image.convert(200, 200, to=window) == (100, 100)

    def test_image_to_screen_composes_resize_and_offsets(self, frame_stack):
        screen, _m, _w, image = frame_stack
        # (200,200) image → (100,100) window → +offsets → screen
        assert image.convert(200, 200, to=screen) == (1920 + 50 + 100, 80 + 100)

    def test_round_trip_is_identity(self, frame_stack):
        screen, _m, _w, image = frame_stack
        sx, sy = image.convert(123, 45, to=screen)
        assert image.from_root(*screen.to_root(sx, sy)) == pytest.approx((123, 45))

    def test_screen_to_window_subtracts_offsets(self, frame_stack):
        screen, _m, window, _i = frame_stack
        assert screen.convert(1980, 90, to=window) == (10, 10)
