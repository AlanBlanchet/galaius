"""Device and media emulation — the browser context config a session can be reconfigured to.

- Device / viewport emulation (#21): a session can be reconfigured to true device metrics —
  pure tests over the kwargs builder + the action validator, plus a live round-trip.
- Media emulation (#107): reduced_motion / color_scheme are first-class, and an unknown field
  is a loud ValidationError instead of a silent drop.
"""

import pytest
from pydantic import ValidationError

from interact.actions import EmulateDeviceAction
from interact.browser import BrowserManager

from tests.support import browser_manager, ready_or_skip


# =============================================================================================
# Device / viewport emulation (#21)
# =============================================================================================


def test_default_context_pins_dpr_one_and_no_emulation():
    kw = browser_manager()._context_kwargs()
    assert kw["viewport"] == {"width": 1280, "height": 720}
    assert kw["device_scale_factor"] == 1.0
    assert "is_mobile" not in kw and "has_touch" not in kw


def test_device_override_drives_viewport_dpr_touch():
    mgr = browser_manager()
    mgr._device_override = {
        "width": 390,
        "height": 844,
        "device_scale_factor": 3,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": "iPhone",
    }
    kw = mgr._context_kwargs()
    assert kw["viewport"] == {"width": 390, "height": 844}
    assert kw["device_scale_factor"] == 3.0
    assert kw["is_mobile"] is True
    assert kw["has_touch"] is True
    assert kw["user_agent"] == "iPhone"


def test_is_mobile_is_chromium_only():
    # Firefox/WebKit reject is_mobile — it must be dropped so the context still builds.
    mgr = browser_manager(browser_type="firefox")
    mgr._device_override = {"width": 390, "height": 844, "is_mobile": True, "has_touch": True}
    kw = mgr._context_kwargs()
    assert "is_mobile" not in kw
    assert kw["has_touch"] is True  # touch is fine everywhere


def test_describe_device_flags_dpr_offset_caveat():
    desc = BrowserManager._describe_device(
        {"width": 390, "height": 844, "device_scale_factor": 3, "is_mobile": True, "has_touch": True}
    )
    assert "390x844" in desc and "DPR 3" in desc and "mobile" in desc and "touch" in desc
    assert "offset" in desc  # warns refs can drift at DPR≠1
    assert "offset" not in BrowserManager._describe_device({"width": 800, "height": 600})


@pytest.mark.parametrize(
    "kwargs,ok",
    [
        ({"device": "iPhone 13"}, True),
        ({"width": 390, "height": 844}, True),
        ({"reset": True}, True),
        ({"width": 390}, False),  # height missing
        ({"height": 844}, False),  # width missing
        ({"is_mobile": True}, False),  # no size, no device, no reset
        ({}, False),
    ],
)
def test_emulate_action_validation(kwargs, ok):
    if ok:
        EmulateDeviceAction(**kwargs)
    else:
        with pytest.raises(ValueError):
            EmulateDeviceAction(**kwargs)


@pytest.mark.asyncio
async def test_emulate_device_applies_live():
    """End-to-end against real Chromium: a named device + explicit size actually change the CSS
    viewport, and reset restores the default. No VLM/key; self-skips in bare CI."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        want = mgr._playwright.devices["iPhone 13"]["viewport"]["width"]
        await mgr.emulate_device(device="iPhone 13")
        page = await mgr.get_page(0)
        await page.set_content('<meta name="viewport" content="width=device-width, initial-scale=1">')
        assert await page.evaluate("() => window.innerWidth") == want
        assert await page.evaluate("() => navigator.maxTouchPoints > 0") is True

        await mgr.emulate_device(width=360, height=640)
        page = await mgr.get_page(0)
        assert await page.evaluate("() => window.innerWidth") == 360

        await mgr.emulate_device(reset=True)
        page = await mgr.get_page(0)
        assert await page.evaluate("() => window.innerWidth") == 1280

        with pytest.raises(ValueError):
            await mgr.emulate_device(device="NoSuchPhone 99")
    finally:
        await mgr.close()


# =============================================================================================
# Media emulation (#107)
# =============================================================================================


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
    mgr = browser_manager()
    try:
        await ready_or_skip(mgr)
        page = await mgr.get_page()
        await page.set_content("<p>x</p>")
        q = "() => matchMedia('(prefers-reduced-motion: reduce)').matches"
        assert await page.evaluate(q) is False
        await mgr.apply_media(reduced_motion="reduce")
        assert await page.evaluate(q) is True, "the media override never reached the page"
    finally:
        await mgr.close()
