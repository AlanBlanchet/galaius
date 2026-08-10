"""Media emulation on a live session (#107), and the silent-drop that hid its absence.

`emulate_device` took an extra `reduced_motion` field, accepted the call, and ignored it — the
worst failure shape for an agent-facing API, because nothing distinguishes "applied" from
"dropped" except the behaviour never changing. So: the media features are first-class AND an
unknown field is now a loud validation error rather than silence.
"""

import pytest
from pydantic import ValidationError

from interact.actions import EmulateDeviceAction
from interact.browser import BrowserManager
from interact.config import Config


def test_media_features_are_first_class():
    a = EmulateDeviceAction(width=800, height=600, reduced_motion="reduce", color_scheme="dark")
    assert a.reduced_motion == "reduce" and a.color_scheme == "dark"


def test_media_features_work_without_a_viewport():
    # Forcing reduced motion shouldn't require inventing a device size.
    a = EmulateDeviceAction(reduced_motion="reduce")
    assert a.reduced_motion == "reduce" and a.width is None


def test_an_unknown_field_is_a_loud_error_not_a_silent_drop():
    with pytest.raises(ValidationError) as exc:
        EmulateDeviceAction(width=800, height=600, reduced_moton="reduce")  # typo
    assert "reduced_moton" in str(exc.value)


@pytest.mark.asyncio
async def test_reduced_motion_actually_reaches_the_page():
    mgr = BrowserManager(Config(headless=True, browser_type="chromium"))
    try:
        try:
            await mgr.ensure_ready()
        except Exception as exc:
            pytest.skip(f"no launchable chromium: {exc}")
        page = await mgr.get_page()
        await page.set_content("<p>x</p>")
        q = "() => matchMedia('(prefers-reduced-motion: reduce)').matches"
        assert await page.evaluate(q) is False
        await mgr.apply_media(reduced_motion="reduce")
        assert await page.evaluate(q) is True, "the media override never reached the page"
    finally:
        await mgr.close()
