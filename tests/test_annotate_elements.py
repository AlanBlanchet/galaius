"""The DOM ref scan against the shapes real pages take.

Two reported gaps: a `<details>` disclosure's `<summary>` — the thing you click to open it — was
invisible to the scan because the tag list only knew about buttons and roles (an agent driving a
disclosure-triggered panel could not find the trigger at all); and a raw non-HTML document (a
`.svg` navigated to directly, which Chromium renders in its standalone image viewer) has a NULL
`document.body`, so the scan threw `Cannot read properties of null` on every call after the
first (#108).
"""

import pytest

from interact.browser import BrowserManager
from interact.config import Config
from interact.server import _scan_elements


def _mgr() -> BrowserManager:
    return BrowserManager(Config(headless=True, browser_type="chromium"))


async def _ready(mgr: BrowserManager):
    try:
        await mgr.ensure_ready()
    except Exception as exc:  # no browser provisioned (bare CI)
        pytest.skip(f"no launchable chromium: {exc}")


@pytest.mark.asyncio
async def test_a_details_summary_is_a_detected_trigger():
    mgr = _mgr()
    try:
        await _ready(mgr)
        page = await mgr.get_page()
        await page.set_content(
            "<details><summary>Art direction</summary><p>panel body</p></details>"
        )
        els = await _scan_elements(mgr)
        assert any("Art direction" in (e.name or "") for e in els), (
            f"the disclosure trigger was not detected: {[e.name for e in els]}"
        )
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_scanning_a_raw_image_document_does_not_throw():
    # Chromium renders a navigated .svg in its standalone image viewer: no <body> at all. The scan
    # must degrade to "nothing to act on" rather than raising (#108).
    mgr = _mgr()
    try:
        await _ready(mgr)
        page = await mgr.get_page()
        await page.goto(
            "data:image/svg+xml,"
            "%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20width='40'%20height='40'%3E"
            "%3Crect%20width='40'%20height='40'/%3E%3C/svg%3E"
        )
        assert await page.evaluate("() => document.body === null"), "not an image document"
        assert await _scan_elements(mgr) == []  # no throw, no elements
    finally:
        await mgr.close()
