from pathlib import Path

import pytest

from interact.browser import BrowserManager
from interact.config import LOG_MAXLEN, Config
from interact.state import InteractiveElement, ref_locator

_ANNOTATE_JS = (
    Path(__file__).parents[1] / "src" / "interact" / "js" / "annotate_elements.js"
).read_text()


def _el(index: int, ref: str | None = None) -> InteractiveElement:
    return InteractiveElement(
        index=index,
        ref=ref,
        role="button",
        name=f"btn{index}",
        x=0,
        y=0,
        width=10,
        height=10,
    )


# --- BrowserManager element map ---


def test_element_map_per_tab_isolation():
    mgr = BrowserManager(Config())
    mgr.set_element_map(0, [_el(1), _el(2)])
    mgr.set_element_map(1, [_el(3)])

    assert mgr.get_element(1, tab=0) is not None
    assert mgr.get_element(2, tab=0) is not None
    assert mgr.get_element(3, tab=0) is None
    assert mgr.get_element(3, tab=1) is not None
    assert mgr.get_element(1, tab=1) is None


def test_element_map_tab_overwrite():
    mgr = BrowserManager(Config())
    mgr.set_element_map(0, [_el(1)])
    mgr.set_element_map(0, [_el(2)])
    assert mgr.get_element(1, tab=0) is None
    assert mgr.get_element(2, tab=0) is not None


def test_get_element_missing_tab():
    mgr = BrowserManager(Config())
    assert mgr.get_element(1, tab=5) is None


def test_get_element_default_tab():
    mgr = BrowserManager(Config())
    mgr.set_element_map(0, [_el(7)])
    assert mgr.get_element(7) is not None


# --- InteractiveElement ref ---


def test_element_ref_none_by_default():
    el = InteractiveElement(
        index=1, role="button", name="x", x=0, y=0, width=10, height=10
    )
    assert el.ref is None


def test_element_playwright_ref():
    el = InteractiveElement(
        index=1, ref="e42", role="button", name="x", x=0, y=0, width=10, height=10
    )
    assert el.playwright_ref == '[data-interact-ref="e42"]'


def test_element_center_coords():
    el = InteractiveElement(
        index=1, role="button", name="x", x=10, y=20, width=80, height=40
    )
    assert el.center_x == 50
    assert el.center_y == 40


# --- ref_locator ---


def test_ref_locator():
    assert ref_locator("e5") == '[data-interact-ref="e5"]'


# --- drain_network_log / drain_console_log ---


def test_drain_network_log_returns_entries():
    mgr = BrowserManager(Config())
    mgr._network_log.append({"method": "GET", "url": "https://example.com"})
    mgr._network_log.append({"method": "POST", "url": "https://example.com/api"})
    entries = mgr.drain_network_log()
    assert len(entries) == 2
    assert entries[0]["method"] == "GET"
    assert len(mgr._network_log) == 2  # not cleared


def test_drain_network_log_clear():
    mgr = BrowserManager(Config())
    mgr._network_log.append({"method": "GET", "url": "https://example.com"})
    entries = mgr.drain_network_log(clear=True)
    assert len(entries) == 1
    assert len(mgr._network_log) == 0


def test_drain_console_log_returns_entries():
    mgr = BrowserManager(Config())
    mgr._console_log.append({"level": "log", "text": "hello"})
    entries = mgr.drain_console_log()
    assert len(entries) == 1
    assert entries[0]["text"] == "hello"
    assert len(mgr._console_log) == 1  # not cleared


def test_drain_console_log_clear():
    mgr = BrowserManager(Config())
    mgr._console_log.append({"level": "error", "text": "oops"})
    entries = mgr.drain_console_log(clear=True)
    assert len(entries) == 1
    assert len(mgr._console_log) == 0


def test_log_deque_maxlen():
    mgr = BrowserManager(Config())
    assert mgr._network_log.maxlen == LOG_MAXLEN
    assert mgr._console_log.maxlen == LOG_MAXLEN


def test_is_recording_default_false():
    mgr = BrowserManager(Config())
    assert mgr.is_recording is False


# --- annotate_elements.js scan: actionability filter + visibility/occlusion ranking ---

# One crafted page exercises the whole contract: non-actionable elements are dropped, and
# the survivors are ranked clickable-now → covered → off-screen so a fixed `limit` keeps the
# controls a user can actually reach. No VLM/API key — pure DOM logic in a headless browser.
_SCAN_PAGE = """
<style>button{display:block;width:160px;height:30px}</style>
<button id="vis">Visible</button>
<button disabled>Disabled</button>
<button aria-hidden="true">A11yHidden</button>
<button style="visibility:hidden">Invisible</button>
<button style="opacity:0">Transparent</button>
<button id="covered" style="position:absolute;top:120px;left:0">Covered</button>
<div style="position:absolute;top:120px;left:0;width:160px;height:30px;z-index:9;background:#fff"></div>
<button style="position:absolute;top:3000px;left:0">BelowFold</button>
"""


async def _scan(html: str, limit: int = 50):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # no browser provisioned (bare CI) → not this test's concern
            pytest.skip(f"no launchable chromium: {exc}")
        page = await browser.new_page(viewport={"width": 1280, "height": 720})
        await page.set_content(html)
        result = await page.evaluate(_ANNOTATE_JS, {"scope": None, "limit": limit, "nextRef": 0})
        await browser.close()
        return result["elements"]  # JS returns {elements, nextRef} for the session ref counter (#35)


@pytest.mark.asyncio
async def test_scan_filters_and_ranks():
    boxes = await _scan(_SCAN_PAGE)
    names = [b["name"] for b in boxes]

    # Non-actionable elements never surface as refs.
    for dropped in ("Disabled", "A11yHidden", "Invisible", "Transparent"):
        assert dropped not in names, f"{dropped} should be filtered out"

    # Survivors ranked: clickable-now before covered before off-screen.
    assert names == ["Visible", "Covered", "BelowFold"]
    # Refs are sequential and match returned order (Python trusts the JS ref == index N).
    assert [b["ref"] for b in boxes] == ["e1", "e2", "e3"]


@pytest.mark.asyncio
async def test_scan_limit_keeps_highest_ranked():
    # limit=1 must keep the clickable-now control, not whatever came first in document order.
    boxes = await _scan(_SCAN_PAGE, limit=1)
    assert [b["name"] for b in boxes] == ["Visible"]


# --- #70: HTTP Basic-auth via Playwright httpCredentials (no native dialog to type into) --------


def test_http_credentials_fold_into_context_kwargs():
    """#70: a native Basic-auth 'Sign in' dialog can't be typed into reliably (keystrokes leak as
    browser accelerators). Setting credentials on the session makes Playwright authenticate at the
    context level — the dialog never appears."""
    mgr = BrowserManager(Config())
    assert "http_credentials" not in mgr._context_kwargs()  # off by default
    mgr.set_http_credentials("alice", "s3cret")
    kw = mgr._context_kwargs()
    assert kw["http_credentials"] == {"username": "alice", "password": "s3cret"}


def test_http_credentials_parse_user_colon_pass():
    mgr = BrowserManager(Config())
    mgr.set_http_credentials_spec("bob:hunter2")
    assert mgr._context_kwargs()["http_credentials"] == {"username": "bob", "password": "hunter2"}
    mgr.set_http_credentials_spec(None)  # clearing removes it
    assert "http_credentials" not in mgr._context_kwargs()


# --- #69: reduce automation fingerprint so ordinary bot-checks don't flag the QA browser ---------


def test_chromium_launch_hides_automation_signals():
    """#69: Cloudflare & friends fingerprint the default automation flags. A chromium launch now
    drops --enable-automation and disables the AutomationControlled blink feature (navigator.webdriver)
    so a legitimate QA browse is less likely to be flagged. Non-chromium engines are untouched."""
    from interact.browser import chromium_launch_kwargs

    kw = chromium_launch_kwargs("chromium", headless=True, slow_mo=0)
    assert "--disable-blink-features=AutomationControlled" in kw["args"]
    assert kw["ignore_default_args"] == ["--enable-automation"]

    assert chromium_launch_kwargs("firefox", headless=True, slow_mo=0) == {
        "headless": True, "slow_mo": 0
    }
