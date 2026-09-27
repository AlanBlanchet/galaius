from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from interact.config import LOG_MAXLEN
from interact.state import InteractiveElement, PageState, _visible_text, ref_locator
from tests.support import browser_manager, interactive_element

_ANNOTATE_JS = (
    Path(__file__).parents[1]
    / "src" / "interact" / "js" / "annotate_elements.js"
).read_text()


def _el(index: int, ref: str | None = None) -> InteractiveElement:
    return interactive_element(index, ref, name=f"btn{index}")


# --- BrowserManager element map ---


def test_element_map_per_tab_isolation():
    mgr = browser_manager()
    mgr.set_element_map(0, [_el(1), _el(2)])
    mgr.set_element_map(1, [_el(3)])

    assert mgr.get_element(1, tab=0) is not None
    assert mgr.get_element(2, tab=0) is not None
    assert mgr.get_element(3, tab=0) is None
    assert mgr.get_element(3, tab=1) is not None
    assert mgr.get_element(1, tab=1) is None


def test_element_map_tab_overwrite():
    mgr = browser_manager()
    mgr.set_element_map(0, [_el(1)])
    mgr.set_element_map(0, [_el(2)])
    assert mgr.get_element(1, tab=0) is None
    assert mgr.get_element(2, tab=0) is not None


def test_get_element_missing_tab():
    mgr = browser_manager()
    assert mgr.get_element(1, tab=5) is None


def test_get_element_default_tab():
    mgr = browser_manager()
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
    mgr = browser_manager()
    mgr._network_log.append({"method": "GET", "url": "https://example.com"})
    mgr._network_log.append({"method": "POST", "url": "https://example.com/api"})
    entries = mgr.drain_network_log()
    assert len(entries) == 2
    assert entries[0]["method"] == "GET"
    assert len(mgr._network_log) == 2  # not cleared


def test_drain_network_log_clear():
    mgr = browser_manager()
    mgr._network_log.append({"method": "GET", "url": "https://example.com"})
    entries = mgr.drain_network_log(clear=True)
    assert len(entries) == 1
    assert len(mgr._network_log) == 0


def test_drain_console_log_returns_entries():
    mgr = browser_manager()
    mgr._console_log.append({"level": "log", "text": "hello"})
    entries = mgr.drain_console_log()
    assert len(entries) == 1
    assert entries[0]["text"] == "hello"
    assert len(mgr._console_log) == 1  # not cleared


def test_drain_console_log_clear():
    mgr = browser_manager()
    mgr._console_log.append({"level": "error", "text": "oops"})
    entries = mgr.drain_console_log(clear=True)
    assert len(entries) == 1
    assert len(mgr._console_log) == 0


def test_log_deque_maxlen():
    mgr = browser_manager()
    assert mgr._network_log.maxlen == LOG_MAXLEN
    assert mgr._console_log.maxlen == LOG_MAXLEN


def test_is_recording_default_false():
    mgr = browser_manager()
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
<div style="opacity:0"><button>InheritedTransparent</button></div>
<button id="covered" style="position:absolute;top:120px;left:0">Covered</button>
<div style="position:absolute;top:120px;left:0;width:160px;height:30px;z-index:9;background:#fff"></div>
<button style="position:absolute;top:3000px;left:0">BelowFold</button>
"""


@asynccontextmanager
async def _headless_page():
    """A real headless Chromium page — pure DOM logic, no VLM/API key."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # no browser provisioned (bare CI) → not this test's concern
            pytest.skip(f"no launchable chromium: {exc}")
        try:
            yield await browser.new_page(viewport={"width": 1280, "height": 720})
        finally:
            await browser.close()


async def _scan(html: str, limit: int = 50):
    async with _headless_page() as page:
        await page.set_content(html)
        args = {"scope": None, "limit": limit, "nextRef": 0}
        result = await page.evaluate(_ANNOTATE_JS, args)
        return result["elements"]  # JS returns {elements, nextRef}: the session ref counter (#35)


@pytest.mark.asyncio
async def test_scan_filters_and_ranks():
    boxes = await _scan(_SCAN_PAGE)
    names = [b["name"] for b in boxes]

    # Non-actionable elements never surface as refs.
    dropped_names = ("Disabled", "A11yHidden", "Invisible", "Transparent", "InheritedTransparent")
    for dropped in dropped_names:
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


# --- #128: the text summary must not read opacity:0 text as "visible at rest" -----------------

# Playwright's innerText drops display:none and visibility:hidden text but NOT opacity:0 — own or
# inherited — so a critic reading the summary took a transparent element for text visible at rest.
# visibility is per text node on its parent (a visibility:visible child inside a hidden parent DOES
# show, as innerText has it); a closed <details> body and non-rendered tags never show.
_TEXT_PAGE = """
<div id="main">
  <p>Shown paragraph</p>
  <p style="opacity:0">Ghost</p>
  <div style="opacity:0"><p>Inherited ghost</p></div>
  <p style="visibility:hidden">Hidden <span style="visibility:visible">Peek</span></p>
  <div style="display:none">Collapsed block</div>
  <details><summary>More</summary><p>Folded</p></details>
  <select><option>Opt A</option><option selected>Opt B</option></select>
  <script>var leaked = "ScriptText";</script>
</div>
<p>Outside scope</p>
"""
_AT_REST = ("Shown paragraph", "Peek", "More", "Opt B")
# a closed dropdown shows only its selected option; innerText listed every option
_NOT_AT_REST = (
    "Ghost", "Inherited ghost", "Hidden", "Folded", "Collapsed block", "ScriptText", "Opt A"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [None, "#main"])
async def test_visible_text_omits_transparent_hidden_and_unrendered_text(scope):
    async with _headless_page() as page:
        await page.set_content(_TEXT_PAGE)
        state = await PageState.capture(page, scope=scope)

    for text in (state.visible_text, state.text_summary()):
        for shown in _AT_REST:
            assert shown in text
        for absent in _NOT_AT_REST:
            assert absent not in text, f"{absent!r} is not visible at rest, yet the summary has it"
    assert ("Outside scope" in state.visible_text) is (scope is None)  # scope= bounds the read


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_visible_text_falls_back_to_inner_text_when_the_walker_throws(scoped):
    """A hostile page (a patched getComputedStyle, a detached frame) must not take the tool down:
    the summary drops back to the old inner_text read, silently."""
    page = MagicMock()
    page.evaluate = AsyncMock(side_effect=Exception("hostile page"))
    page.inner_text = AsyncMock(return_value="body text " * 400)  # over the cap → still capped
    locator = MagicMock()
    locator.evaluate = AsyncMock(side_effect=Exception("hostile page"))
    locator.inner_text = AsyncMock(return_value="scoped text")

    text = await _visible_text(page, locator if scoped else None)

    expected = "scoped text" if scoped else ("body text " * 400)[:2000]
    assert text == expected
    (locator if scoped else page).inner_text.assert_awaited_once()


# --- #70: HTTP Basic-auth via Playwright httpCredentials (no native dialog to type into) --------


def test_http_credentials_fold_into_context_kwargs():
    """#70: a native Basic-auth 'Sign in' dialog can't be typed into reliably (keystrokes leak as
    browser accelerators). Setting credentials on the session makes Playwright authenticate at the
    context level — the dialog never appears."""
    mgr = browser_manager()
    assert "http_credentials" not in mgr._context_kwargs()  # off by default
    mgr.set_http_credentials("alice", "s3cret")
    kw = mgr._context_kwargs()
    assert kw["http_credentials"] == {"username": "alice", "password": "s3cret"}


def test_http_credentials_parse_user_colon_pass():
    mgr = browser_manager()
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
