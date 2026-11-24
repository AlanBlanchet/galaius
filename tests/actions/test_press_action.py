"""A held press (#103): mousedown, wait, mouseup — so the browser's GENUINE `:active` UA state
engages and can be screenshotted or recorded.

Neither workaround reached it: a JS-dispatched PointerEvent is untrusted so Chromium never
applies `:active`, and a zero-distance `drag` is treated as an ordinary click the instant mouseup
lands (it even follows an <a href>). Only a real CDP mousedown held open does it.
"""

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from galaius.actions.dispatch import _run_actions_browser
from galaius.actions.models import PressAction
from tests.support import browser_manager

_PAGE = """
<style>
  #b { width: 120px; height: 40px; }
  #b:active { transform: translateY(4px); }
</style>
<button id="b">hold me</button>
"""


def test_hold_must_be_positive_and_bounded():
    assert PressAction(x=1, y=2).hold > 0          # a sane default, not zero
    with pytest.raises(ValidationError):
        PressAction(x=1, y=2, hold=0)
    with pytest.raises(ValidationError):
        PressAction(x=1, y=2, hold=120)            # can't wedge a batch open


@pytest.mark.asyncio
async def test_a_press_engages_real_active_state_then_releases():
    mgr = browser_manager()
    try:
        try:
            await mgr.ensure_ready()
        except Exception as exc:
            pytest.skip(f"no launchable chromium: {exc}")
        page = await mgr.get_page()
        await page.set_content(_PAGE)
        # Assert the RENDERED effect of :active, not `matches(':active')` — Chromium applies the
        # active style under a real CDP press while `matches` still reports false, so `matches` is
        # a false negative here. The computed style is what a screenshot would show anyway.
        transform = "() => getComputedStyle(document.querySelector('#b')).transform"
        assert await page.evaluate(transform) in ("none", "matrix(1, 0, 0, 1, 0, 0)")

        # Sample WHILE the press is held — by polling until the pressed style appears, never at a
        # fixed instant: under full-suite load the mousedown can land later than any chosen delay,
        # and a one-shot read then sees the resting style and calls a working press broken (#129).
        press = asyncio.create_task(PressAction(selector="#b", hold=0.8).execute(page))
        seen: list[str] = []
        while not press.done():
            seen.append(await page.evaluate(transform))
            if seen[-1] == "matrix(1, 0, 0, 1, 0, 4)":
                break
            await asyncio.sleep(0.03)
        await press

        assert "matrix(1, 0, 0, 1, 0, 4)" in seen, f"not pressed while held; saw {sorted(set(seen))}"
        assert await page.evaluate(transform) in ("none", "matrix(1, 0, 0, 1, 0, 0)"), (
            "the press never released"
        )
    finally:
        await mgr.close()


# =============================================================================================
# Touch hold (phone first): a real finger, the long-press menu, the cancel paths, the frame
# =============================================================================================

# Every touch / pointer / menu event the page sees, with whether the browser (not a script) raised it.
_TOUCH_PAGE = """
<body style="margin:0;height:3000px">
<button id="card" style="width:200px;height:100px;margin:50px;background:#09f">hint card</button>
<p id="text" style="margin:20px">some selectable text</p>
<script>
  window.ev = [];
  for (const t of ['touchstart','touchmove','touchend','touchcancel','contextmenu','click'])
    document.addEventListener(t, e => ev.push(t + (e.isTrusted ? '' : '!')), {passive: true});
  document.addEventListener('contextmenu', e => e.preventDefault());
</script></body>
"""

async def _page(mgr):
    """The session's page after `emulate_device` — the path an agent takes to a phone: 390x844 CSS px, touch."""
    try:
        await mgr.ensure_ready()
    except Exception as exc:
        pytest.skip(f"no launchable chromium: {exc}")
    await mgr.emulate_device(width=390, height=844, device_scale_factor=2, is_mobile=True, has_touch=True)
    page = await mgr.get_page()
    await page.set_content(_TOUCH_PAGE)
    return page


async def _events(page) -> list[str]:
    return await page.evaluate("ev.slice()")


@pytest.mark.asyncio
async def test_a_touch_hold_is_a_real_long_press_with_no_click_after():
    mgr = browser_manager()
    try:
        page = await _page(mgr)
        await PressAction(selector="#card", hold=0.9).execute(page)
        seen = await _events(page)
        # Trusted (no "!"), in order: finger down, the platform's long-press menu, cancelled — never a click.
        assert seen == ["touchstart", "contextmenu", "touchcancel"], seen
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_a_short_touch_hold_is_a_tap_and_a_bare_touch_sequence_never_opens_the_menu():
    mgr = browser_manager()
    try:
        page = await _page(mgr)
        await PressAction(selector="#card", hold=0.2).execute(page)
        assert await _events(page) == ["touchstart", "touchend", "click"]
        await page.evaluate("ev.length = 0")
        await PressAction(selector="#card", hold=0.8, long_press=False).execute(page)
        assert "contextmenu" not in await _events(page)
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_a_move_before_the_threshold_cancels_the_long_press_and_a_move_after_it_drags():
    mgr = browser_manager()
    try:
        page = await _page(mgr)
        await PressAction(selector="#card", hold=0.9, move_to=(150, 400), move_at=0.1).execute(page)
        seen = await _events(page)
        assert "touchmove" in seen and "contextmenu" not in seen and "click" not in seen, seen
        await page.evaluate("ev.length = 0")
        await PressAction(selector="#card", hold=0.9, move_to=(150, 400)).execute(page)  # drag after the hold
        seen = await _events(page)
        assert seen[0] == "touchstart" and "contextmenu" in seen and "touchmove" in seen, seen
        assert seen.index("contextmenu") < seen.index("touchmove")
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_a_touch_drag_scrolls_the_page_while_held():
    mgr = browser_manager()
    try:
        page = await _page(mgr)
        await PressAction(x=200, y=600, hold=0.3, move_to=(200, 200), move_at=0.1).execute(page)
        assert await page.evaluate("scrollY") > 100
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_the_touch_is_lifted_when_the_hold_is_cancelled():
    mgr = browser_manager()
    try:
        page = await _page(mgr)
        task = asyncio.create_task(PressAction(selector="#card", hold=20).execute(page))
        while "touchstart" not in await _events(page):
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)
        assert (await _events(page))[-1] == "touchcancel"  # lifted, and no tap on the target
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_the_frame_is_taken_while_the_pointer_is_still_down():
    mgr = browser_manager()
    try:
        try:
            await mgr.ensure_ready()
        except Exception as exc:
            pytest.skip(f"no launchable chromium: {exc}")
        page = await mgr.get_page()
        await page.set_content(
            "<style>body{margin:0}#b{display:block;width:100px;height:100px;background:#00f}"
            "#b:active{background:#f00}</style><button id='b'></button>"
        )
        action = PressAction(selector="#b", hold=0.8, capture=True)
        await action.execute(page)

        def centre(png: bytes):
            return Image.open(io.BytesIO(png)).convert("RGB").getpixel((50, 50))

        assert centre(action._frame) == (255, 0, 0), "the frame did not show the held state"
        assert centre(await page.screenshot(type="png")) == (0, 0, 255), "the press never released"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_a_batch_returns_the_held_frame_saved_and_reported(tmp_path):
    mgr = browser_manager()
    try:
        page = await _page(mgr)
        dest = tmp_path / "held.png"
        report = await _run_actions_browser(
            mgr, [PressAction(selector="#card", hold=0.8, path=str(dest))], None, None, None, "default"
        )
        assert "held frame: Saved to" in report and "touchcancel" not in report
        assert Image.open(dest).size[0] > 0
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_a_mouse_hold_can_drag_while_down_and_a_right_hold_opens_the_menu():
    mgr = browser_manager()
    try:
        try:
            await mgr.ensure_ready()
        except Exception as exc:
            pytest.skip(f"no launchable chromium: {exc}")
        page = await mgr.get_page()
        await page.set_content(
            "<button id='b' style='width:100px;height:60px'></button><script>window.ev=[];"
            "for (const t of ['mousedown','mousemove','mouseup','contextmenu']) "
            "document.addEventListener(t, e => ev.push(t + ':' + Math.round(e.clientX) + ',' + Math.round(e.clientY)));"
            "document.addEventListener('contextmenu', e => e.preventDefault());</script>"
        )
        await PressAction(selector="#b", hold=0.3, move_to=(300, 200)).execute(page)
        seen = await _events(page)
        held = seen[next(i for i, e in enumerate(seen) if e.startswith("mousedown")):]
        assert held[-1] == "mouseup:300,200" and any(e.startswith("mousemove:3") for e in held), seen
        await page.evaluate("ev.length = 0")
        await PressAction(selector="#b", hold=0.2, button="right").execute(page)
        assert any(e.startswith("contextmenu") for e in await _events(page))
    finally:
        await mgr.close()


# =============================================================================================
# Fast rows (no browser)
# =============================================================================================


def test_the_hold_shape_is_validated():
    with pytest.raises(ValidationError, match="move_at needs move_to"):
        PressAction(x=1, y=2, move_at=0.1)
    with pytest.raises(ValidationError, match="within the hold"):
        PressAction(x=1, y=2, hold=1, move_to=(5, 5), move_at=2)
    assert PressAction(x=1, y=2, path="a.png").capture is True
    assert PressAction(x=1, y=2, query="what is shown").capture is True
    with pytest.raises(ValidationError):
        PressAction(x=1, y=2, nonsense=1)


@pytest.mark.asyncio
async def test_a_touch_context_on_an_engine_without_cdp_says_it_only_taps():
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=True)  # maxTouchPoints > 0
    page.context.new_cdp_session = AsyncMock(side_effect=PlaywrightError("CDP is Chromium only"))
    with pytest.raises(ValueError, match="needs Chromium"):
        await PressAction(x=5, y=5).execute(page)


@pytest.mark.asyncio
async def test_a_touch_has_no_buttons():
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=True)
    with pytest.raises(ValueError, match="no buttons"):
        await PressAction(x=5, y=5, button="right").execute(page)


@pytest.mark.asyncio
async def test_a_touch_that_cannot_start_still_releases_its_cdp_session_and_lifts_nothing():
    cdp = MagicMock()
    cdp.send = AsyncMock(side_effect=PlaywrightError("target closed"))
    cdp.detach = AsyncMock()
    page = MagicMock()
    page.evaluate = AsyncMock(return_value=True)
    page.context.new_cdp_session = AsyncMock(return_value=cdp)
    with pytest.raises(PlaywrightError, match="target closed"):
        await PressAction(x=5, y=5).execute(page)
    cdp.detach.assert_awaited_once()
    assert [call.args[1]["type"] for call in cdp.send.await_args_list] == ["touchStart"]   # no lift of a touch never down
