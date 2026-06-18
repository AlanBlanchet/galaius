"""Browser fixes against real Chromium (self-skip in bare CI; no VLM/key):

- #29: data-interact-ref is unique per snapshot even after an SPA re-render (stale refs cleared).
- #29: a selector that matches several nodes clicks the first VISIBLE one, not a hidden first.
- #30: tab-less tool captures follow the session's active tab after new_tab / switch_tab.
"""

import pytest

from interact.actions import ClickAction
from interact.browser import BrowserManager
from interact.config import Config


def _mgr() -> BrowserManager:
    return BrowserManager(Config(headless=True, browser_type="chromium"))


async def _ready(mgr: BrowserManager) -> None:
    try:
        await mgr.ensure_ready()
    except Exception as exc:  # no browser provisioned (bare CI)
        pytest.skip(f"no launchable chromium: {exc}")


@pytest.mark.asyncio
async def test_refs_unique_across_rerender():
    from interact.server import _scan_elements

    mgr = _mgr()
    await _ready(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content("<a href='#'>Companies</a><button>Scaleway</button>")
        await _scan_elements(mgr)
        # SPA-style: keep the old nodes (with their refs) and prepend new ones, then rescan.
        await page.evaluate(
            "() => { const d = document.createElement('div');"
            " d.innerHTML = '<button>N1</button><button>N2</button>';"
            " document.body.prepend(...d.childNodes); }"
        )
        await _scan_elements(mgr)
        duplicate_refs = await page.evaluate(
            "() => { const c = {}; let dup = 0;"
            " document.querySelectorAll('[data-interact-ref]').forEach(e => {"
            "  const r = e.getAttribute('data-interact-ref'); c[r] = (c[r]||0)+1;"
            "  if (c[r] > 1) dup++; }); return dup; }"
        )
        assert duplicate_refs == 0
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_selector_click_prefers_visible_match():
    mgr = _mgr()
    await _ready(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            "<button style='display:none' onclick=\"window.h='HIDDEN'\">Annuler</button>"
            "<button onclick=\"window.h='VISIBLE'\">Annuler</button>"
        )
        await ClickAction(selector="button:has-text('Annuler')").execute(page)
        assert await page.evaluate("() => window.h") == "VISIBLE"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_active_tab_follows_new_and_switch():
    from interact.server import _capture

    mgr = _mgr()
    await _ready(mgr)
    try:
        page = await mgr.get_page()
        await page.goto("data:text/html,<title>AAA</title>A")
        await mgr.new_tab("data:text/html,<title>BBB</title>B")
        assert mgr.active_tab == 1
        assert (await _capture(mgr)).title == "BBB"  # tab-less capture → active tab (#30)
        await mgr.switch_tab(0)
        assert mgr.active_tab == 0
        assert (await _capture(mgr)).title == "AAA"
    finally:
        await mgr.close()
