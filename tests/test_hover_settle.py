"""#49: a CSS `:hover` effect DOES latch into a capture (verified live — the hover state applies and
persists across calls); the reporter's "transform: none" was a capture taken MID-transition. So
`hover` now waits for finite CSS transitions/animations to settle before returning, so an immediate
screenshot shows the final hovered state — without blocking on infinite (spinner) animations."""

import time

import pytest

from interact.actions import HoverAction
from interact.browser import BrowserManager
from interact.config import Config


def _mgr() -> BrowserManager:
    return BrowserManager(Config(headless=True, browser_type="chromium"))


async def _ready(mgr: BrowserManager) -> None:
    try:
        await mgr.ensure_ready()
    except Exception as exc:  # no launchable chromium (bare CI)
        pytest.skip(f"no launchable chromium: {exc}")


@pytest.mark.asyncio
async def test_hover_settles_transition_so_an_immediate_capture_sees_the_final_state():
    mgr = _mgr()
    await _ready(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            "<style>#b{display:inline-block;width:50px;height:50px;background:#39c;"
            "transition:transform .5s} #b:hover{transform:scale(1.2)}</style><div id=b></div>"
        )
        await HoverAction(selector="#b").execute(page)
        # No sleep: settle_animations already waited for the .5s transition to finish.
        t = await page.evaluate("() => getComputedStyle(document.getElementById('b')).transform")
        assert t == "matrix(1.2, 0, 0, 1.2, 0, 0)"  # the FINAL scale, not a mid-transition matrix
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_hover_does_not_block_on_an_infinite_animation():
    """A spinner (infinite animation) must not hold hover for the full settle timeout — infinite
    animations are filtered out, so the settle returns promptly."""
    mgr = _mgr()
    await _ready(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            "<style>@keyframes s{to{transform:rotate(360deg)}}"
            "#b{width:30px;height:30px;background:#c33;animation:s 1s linear infinite}</style>"
            "<div id=b></div>"
        )
        t0 = time.monotonic()
        await HoverAction(x=10, y=10).execute(page)
        assert time.monotonic() - t0 < 0.7  # did NOT wait the ~1s settle timeout on the spinner
    finally:
        await mgr.close()
