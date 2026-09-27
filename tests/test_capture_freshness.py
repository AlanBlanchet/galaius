"""Capture freshness: a screenshot or element scan never surfaces stale frame content.

- #19: cached refs are only valid for the frame they were detected on.
- #17: a query screenshot saves the analysed frame, even if the VLM errors.
- #109: capture settle — photograph what is on screen NOW, not before the animation (the
  window, an element capture, AND a nested scroller — bounded on a marquee that never stops).
- #57: get_interactive_elements(fresh=True) force-invalidates the accumulated element cache
  before detecting, so returned refs reflect only the current frame; without `fresh`, the
  accumulating cache (the #19 same-screen union) is left intact.
"""

import base64
import io
from unittest.mock import patch

import pytest
from PIL import Image

from interact.desktop import DesktopElement
from interact.state import PageState
from tests.support import browser_manager, ready_or_skip


@pytest.fixture
def srv():
    import interact.server as _srv
    from interact.server import breaker

    breaker.clear()
    _srv.config.component_criteria = "cap.gui_grounding"
    with patch.object(_srv.Debug, "save"):
        yield _srv
    _srv.config.clear_overrides()  # drop the transient override so it can't leak into later tests
    breaker.clear()


def test_cached_for_returns_refs_only_when_signature_matches():
    wid = 9911
    els = [DesktopElement(index=0, x=1, y=2, w=3, h=4, role="button", name="Quiz")]
    DesktopElement.merge_into(wid, els, "sigA")  # detected on screen A

    assert DesktopElement.cached_for(wid, "sigA"), "same frame → refs surfaced"
    assert DesktopElement.cached_for(wid, "sigB") is None, "navigated away → stale refs withheld (#19)"




@pytest.mark.asyncio
async def test_media_response_saves_file_even_when_vlm_errors(monkeypatch, tmp_path):
    import interact.server as srv

    saved = {}
    monkeypatch.setattr(srv.core, "_save_to_path", lambda p, d: saved.update(path=p, data=d))

    async def boom(*a, **k):
        raise RuntimeError("vlm down")

    monkeypatch.setattr(srv.vlm, "_vlm", boom)
    out = str(tmp_path / "shot.png")
    with pytest.raises(RuntimeError):
        await srv._media_response(b"FRAMEBYTES", "ctx", query="what is this?", path=out)
    assert saved == {"path": out, "data": b"FRAMEBYTES"}, "file must be the analysed frame, written even on error"





# Top screen is red, the one below it blue. If the capture is stale we photograph red.
_SETTLE_PAGE = (
    "<style>html{scroll-behavior:smooth}body{margin:0}div{height:100vh}</style>"
    "<div style='background:#ff0000'></div><div style='background:#0000ff'></div>"
)


def _centre(b64: str) -> tuple[int, int, int]:
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    return img.getpixel((img.width // 2, img.height // 2))


@pytest.mark.asyncio
async def test_capture_waits_for_a_smooth_scroll_to_land():
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(_SETTLE_PAGE)
        # The premise, asserted rather than assumed: set_content returns before layout is
        # necessarily tall enough to scroll, and a document that cannot scroll would photograph
        # the red screen for a reason that has nothing to do with settling. Under a loaded
        # machine that is exactly what happened, and the failure looked like the fix regressing.
        await page.wait_for_function("() => document.body.scrollHeight > window.innerHeight + 10")
        await page.evaluate("() => window.scrollTo(0, window.innerHeight)")
        state = await PageState.capture(page)  # no sleep: the capture must settle itself
        landed = await page.evaluate("() => window.scrollY")
        assert landed > 0, "the page never scrolled — the test's setup failed, not the capture"
        r, g, b = _centre(state.screenshot_base64)
        assert b > 200 and r < 60, f"captured the pre-scroll screen: rgb({r},{g},{b})"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_capture_does_not_hang_on_a_page_that_never_stops_moving():
    """A carousel/marquee scrolls forever. Settling must give up, not block the tool."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            "<style>body{margin:0}div{height:400vh}</style><div></div>"
            "<script>setInterval(() => window.scrollBy(0, 3), 8)</script>"
        )
        import time

        t0 = time.monotonic()
        await PageState.capture(page)
        assert time.monotonic() - t0 < 3.0, "settling must be bounded on a never-still page"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_an_element_capture_settles_too():
    """`screenshot(selector=...)` and `get_interactive_elements` photograph through their own
    paths, not through PageState.capture — so fixing only the funnel leaves the same staleness
    reachable two other ways. Worse for the annotated capture, where boxes are drawn at
    coordinates scanned before the page finished moving."""
    from interact.server import capture as cap

    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            "<style>html{scroll-behavior:smooth}body{margin:0}div{height:100vh}</style>"
            "<div style='background:#ff0000'></div>"
            "<div id=target style='background:#0000ff'></div>"
        )
        await page.wait_for_function("() => document.body.scrollHeight > window.innerHeight + 10")
        await page.evaluate("() => window.scrollTo(0, window.innerHeight)")
        png, _ = await cap._annotate_page(mgr)
        assert await page.evaluate("() => window.scrollY") > 0, "setup failed, not the capture"
        img = Image.open(io.BytesIO(png)).convert("RGB")
        r, g, b = img.getpixel((img.width // 2, img.height // 2))
        assert b > 200 and r < 60, f"annotated capture is pre-scroll: rgb({r},{g},{b})"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_a_smooth_scroll_inside_a_nested_scroller_is_waited_for():
    """Sampling window.scrollX/Y cannot see a scroller that is not the window — and a panel, a
    modal, a virtualised list or anything reached by scrollIntoView scrolls its own container.
    The document never moves, so three stable samples pass immediately and the capture photographs
    the pre-scroll frame: the same defect as #109, one element down."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            "<style>body{margin:0}#box{height:100vh;overflow-y:scroll;scroll-behavior:smooth}"
            "#box>div{height:100vh}</style><div id=box>"
            "<div style='background:#ff0000'></div><div style='background:#0000ff'></div></div>"
        )
        await page.evaluate("() => { const b = document.getElementById('box');"
                            "  b.scrollTo({ top: b.clientHeight }); }")
        state = await PageState.capture(page)
        assert await page.evaluate("() => document.getElementById('box').scrollTop") > 0, "setup"
        r, g, b = _centre(state.screenshot_base64)
        assert b > 200 and r < 60, f"captured the pre-scroll panel: rgb({r},{g},{b})"
    finally:
        await mgr.close()




@pytest.mark.asyncio
async def test_get_interactive_elements_fresh_invalidates_the_cache_first(srv):
    """#57: fresh=True clears the window's accumulated element cache BEFORE detecting, so the
    returned refs reflect only the current frame — the recovery path for a stale cache."""
    from unittest.mock import AsyncMock, MagicMock
    from interact.desktop import DesktopElement, DesktopWindow

    win = MagicMock(spec=DesktopWindow)
    win.wid = 4242
    win.name = "aino"
    order: list[str] = []
    with (
        patch.object(srv.targets, "_resolve_target", return_value=(win, None, None)),
        patch.object(DesktopElement, "invalidate", side_effect=lambda wid: order.append(f"invalidate:{wid}")),
        patch.object(srv.capture, "_annotate_desktop", new=AsyncMock(side_effect=lambda *a, **k: order.append("detect") or ([], "report"))),
        patch.object(srv.core, "_desktop_label", return_value="aino"),
    ):
        await srv.get_interactive_elements(target="aino", fresh=True)
    assert order == ["invalidate:4242", "detect"]  # cleared, THEN detected


@pytest.mark.asyncio
async def test_get_interactive_elements_default_keeps_the_cache(srv):
    """Without fresh, the accumulating cache (the #19 same-screen union) is left intact."""
    from unittest.mock import AsyncMock, MagicMock
    from interact.desktop import DesktopElement, DesktopWindow

    win = MagicMock(spec=DesktopWindow)
    win.wid = 4242
    win.name = "aino"
    with (
        patch.object(srv.targets, "_resolve_target", return_value=(win, None, None)),
        patch.object(DesktopElement, "invalidate") as inval,
        patch.object(srv.capture, "_annotate_desktop", new=AsyncMock(return_value=([], "report"))),
        patch.object(srv.core, "_desktop_label", return_value="aino"),
    ):
        await srv.get_interactive_elements(target="aino")
    inval.assert_not_called()
