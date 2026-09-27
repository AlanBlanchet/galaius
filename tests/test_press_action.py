"""A held press (#103): mousedown, wait, mouseup — so the browser's GENUINE `:active` UA state
engages and can be screenshotted or recorded.

Neither workaround reached it: a JS-dispatched PointerEvent is untrusted so Chromium never
applies `:active`, and a zero-distance `drag` is treated as an ordinary click the instant mouseup
lands (it even follows an <a href>). Only a real CDP mousedown held open does it.
"""

import asyncio

import pytest
from pydantic import ValidationError

from interact.actions import PressAction
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
