"""Browser fixes against real Chromium (self-skip in bare CI; no VLM/key):

- #29: data-interact-ref is unique per snapshot even after an SPA re-render (stale refs cleared).
- #29: a selector that matches several nodes clicks the first VISIBLE one, not a hidden first.
- #30: tab-less tool captures follow the session's active tab after new_tab / switch_tab.
- #34: a ref from one tool call survives into the next — the element map keys on the active tab
  (not None vs 0), and a ref also resolves via the live data-interact-ref attribute.
"""

import pytest

from interact.actions import ClickAction
from interact.browser import BrowserManager
from interact.config import Config
from interact.state import InteractiveElement


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


def test_element_map_shared_between_tabless_scan_and_active_tab_lookup():
    """#34 root cause: a tab-less scan (get_page_state / screenshot / get_interactive_elements)
    stores the element map, and a later run_actions reads it back. The store key (None → active
    tab) and the read key (the active-tab int) must be the SAME bucket — otherwise every ref is
    lost the instant the scan and the click land in separate tool calls. No browser needed."""
    mgr = _mgr()
    els = [InteractiveElement(index=1, ref="e1", role="button", name="Go", x=0, y=0, width=4, height=4)]
    mgr.set_element_map(None, els)  # how every tab-less scan registers its refs
    assert mgr.get_element(1, 0) is els[0]  # how run_actions (current_tab = active tab) reads them
    assert mgr.get_element(1, None) is els[0]
    assert mgr.get_element(1) is els[0]


@pytest.mark.asyncio
async def test_ref_from_prior_scan_clicks_in_a_later_run_actions():
    """#34 end-to-end: a ref handed out by a tab-less scan clicks in a SEPARATE later run_actions
    call, through the real dispatch + Chromium (the exact two-call sequence agents reported)."""
    from interact.dispatch import _run_actions_browser
    from interact.server import _scan_elements

    mgr = _mgr()
    await _ready(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content("<button onclick=\"window.__hit=(window.__hit||0)+1\">Go</button>")
        els = await _scan_elements(mgr)  # the prior tool call
        assert els and els[0].ref
        await _run_actions_browser(mgr, [ClickAction(ref=els[0].ref)], None, None, None, "default")
        assert await page.evaluate("() => window.__hit") == 1  # the ref still resolved
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_ref_clicks_via_live_dom_when_element_map_lost():
    """#34 resilience: even with the server-side element map gone, a ref still clicks via the live
    data-interact-ref attribute — it survives across calls until the next scan."""
    from interact.dispatch import _run_actions_browser
    from interact.server import _scan_elements

    mgr = _mgr()
    await _ready(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content("<button onclick=\"window.__hit=1\">Go</button>")
        ref = (await _scan_elements(mgr))[0].ref
        mgr._element_map.clear()  # map lost; the DOM attribute remains
        await _run_actions_browser(mgr, [ClickAction(ref=ref)], None, None, None, "default")
        assert await page.evaluate("() => window.__hit") == 1
    finally:
        await mgr.close()
