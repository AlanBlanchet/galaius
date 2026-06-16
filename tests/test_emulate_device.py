"""Device / viewport emulation (#21): a session can be reconfigured to true device metrics.

Pure tests over the context-kwargs builder + the action validator (no browser launched). The live
round-trip (innerWidth at 390, touch present) is an integration test.
"""

import pytest

from interact.actions import EmulateDeviceAction
from interact.browser import BrowserManager
from interact.config import Config


def _mgr(**cfg) -> BrowserManager:
    return BrowserManager(Config(**cfg))


def test_default_context_pins_dpr_one_and_no_emulation():
    kw = _mgr()._context_kwargs()
    assert kw["viewport"] == {"width": 1280, "height": 720}
    assert kw["device_scale_factor"] == 1.0
    assert "is_mobile" not in kw and "has_touch" not in kw


def test_device_override_drives_viewport_dpr_touch():
    mgr = _mgr(browser_type="chromium")
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
    mgr = _mgr(browser_type="firefox")
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
    mgr = _mgr(headless=True, browser_type="chromium")
    try:
        await mgr.ensure_ready()
    except Exception as exc:  # no browser provisioned (bare CI)
        pytest.skip(f"no launchable chromium: {exc}")
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
