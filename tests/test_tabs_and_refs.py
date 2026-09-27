"""Tab and ref resolution against real Chromium (self-skip in bare CI; no VLM / key):

- #35 / #29: data-interact-ref is STABLE across scans (a node keeps its ref; only new nodes get a
  fresh one from a session-monotonic counter) and never collides — uniqueness without clearing.
- #29: a selector that matches several nodes clicks the first VISIBLE one, not a hidden first.
- #30: tab-less tool captures follow the session's active tab after new_tab / switch_tab.
- #34: a ref from one tool call survives into the next — the element map keys on the active tab
  (not None vs 0), and a ref also resolves via the live data-interact-ref attribute.
- #108: the DOM ref scan sees a `<details>` disclosure's `<summary>` (the click target), and a
  raw non-HTML document (`.svg` navigated to directly, no `<body>`) degrades to "nothing to act
  on" instead of throwing.
- ``InteractiveElement`` resolves sub-pixel DOM coords (``getBoundingClientRect`` floats) to ints
  at construction, rather than rejecting them.
- Ambiguous targeting (a selector+name conflict, or a name/role match of 0 or many elements)
  fails with an actionable, ref-nudging message — never an opaque Playwright strict-mode dump.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from interact.actions import ClickAction, HoverAction
from interact.actions.dispatch import _named_locator
from interact.state import InteractiveElement

from tests.support import browser_manager, ready_or_skip


@pytest.mark.asyncio
async def test_refs_stable_and_unique_across_rerender():
    """#35: a node keeps its ref across rescans (no renumber); only NEW nodes get a fresh ref from
    the session's monotonic counter, which never resets mid-session and never reuses a number — so
    refs also stay unique (#29) without the old clear-every-scan."""
    from interact.server import _scan_elements

    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content("<a href='#'>Companies</a><button>Scaleway</button>")
        first = {e.name: e.ref for e in await _scan_elements(mgr)}
        assert mgr._ref_counter == 2  # e1, e2 assigned this session

        # SPA-style: keep the old nodes (with their refs) and prepend new ones, then rescan.
        await page.evaluate(
            "() => { const d = document.createElement('div');"
            " d.innerHTML = '<button>N1</button><button>N2</button>';"
            " document.body.prepend(...d.childNodes); }"
        )
        second = {e.name: e.ref for e in await _scan_elements(mgr)}

        # Surviving nodes KEPT their refs (stable — not renumbered by the rescan).
        assert second["Companies"] == first["Companies"]
        assert second["Scaleway"] == first["Scaleway"]
        # New nodes got fresh, higher refs — never reusing e1/e2.
        assert second["N1"] not in first.values() and second["N2"] not in first.values()
        assert mgr._ref_counter == 4

        duplicate_refs = await page.evaluate(
            "() => { const c = {}; let dup = 0;"
            " document.querySelectorAll('[data-interact-ref]').forEach(e => {"
            "  const r = e.getAttribute('data-interact-ref'); c[r] = (c[r]||0)+1;"
            "  if (c[r] > 1) dup++; }); return dup; }"
        )
        assert duplicate_refs == 0  # uniqueness holds via the monotonic counter
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_selector_click_prefers_visible_match():
    mgr = browser_manager()
    await ready_or_skip(mgr)
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

    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.goto("data:text/html,<title>AAA</title>A")
        await mgr.new_tab("data:text/html,<title>BBB</title>B")
        assert mgr.active_tab == 1
        # The tab list is in the message because this fails in the full suite and not alone:
        # a page neither line here opened answers as the active tab (title "Example").
        assert (await _capture(mgr)).title == "BBB", [p.url for p in mgr._context.pages]
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
    mgr = browser_manager()
    els = [InteractiveElement(index=1, ref="e1", role="button", name="Go", x=0, y=0, width=4, height=4)]
    mgr.set_element_map(None, els)  # how every tab-less scan registers its refs
    assert mgr.get_element(1, 0) is els[0]  # how run_actions (current_tab = active tab) reads them
    assert mgr.get_element(1, None) is els[0]
    assert mgr.get_element(1) is els[0]


@pytest.mark.asyncio
async def test_ref_from_prior_scan_clicks_in_a_later_run_actions():
    """#34 end-to-end: a ref handed out by a tab-less scan clicks in a SEPARATE later run_actions
    call, through the real dispatch + Chromium (the exact two-call sequence agents reported)."""
    from interact.actions.dispatch import _run_actions_browser
    from interact.server import _scan_elements

    mgr = browser_manager()
    await ready_or_skip(mgr)
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
    from interact.actions.dispatch import _run_actions_browser
    from interact.server import _scan_elements

    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content("<button onclick=\"window.__hit=1\">Go</button>")
        ref = (await _scan_elements(mgr))[0].ref
        mgr._element_map.clear()  # map lost; the DOM attribute remains
        await _run_actions_browser(mgr, [ClickAction(ref=ref)], None, None, None, "default")
        assert await page.evaluate("() => window.__hit") == 1
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_a_cloned_annotated_node_is_healed_to_a_unique_ref():
    """A framework that clones an annotated node (cloneNode/template stamping/portal duplication)
    copies its data-interact-ref — from then on clicks by that ref throw a strict-mode violation
    ('resolved to N elements', 9x in client logs), and re-scanning never healed it because both
    nodes kept the ref. The scan now strips duplicates (first in document order wins), so a
    re-scan returns unique refs again."""
    import interact.server as srv

    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content("<div id='root'><button id='orig'>Buy</button></div>")
        await srv._scan_elements(mgr)
        # an SPA clones the annotated button (attribute and all)
        await page.evaluate(
            "() => { const b = document.getElementById('orig');"
            " const c = b.cloneNode(true); c.id = 'clone'; b.parentNode.appendChild(c); }"
        )
        elements = await srv._scan_elements(mgr)
        for el in elements:
            n = await page.evaluate(
                "(ref) => document.querySelectorAll(`[data-interact-ref='${ref}']`).length",
                el.ref,
            )
            assert n == 1, f"{el.ref} still resolves to {n} nodes"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_name_click_resolves_to_the_only_visible_match():
    """Agents name what they SEE: 'N elements match name=…' fired 11x+ in client logs when the
    same label existed hidden elsewhere (closed menu, template). One visible match → click it."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            "<button style='display:none' onclick=\"window.h='HIDDEN'\">Connexion</button>"
            "<button onclick=\"window.h='VISIBLE'\">Connexion</button>"
        )
        locator = await _named_locator(page, ClickAction(name="Connexion"))
        await locator.click()
        assert await page.evaluate("() => window.h") == "VISIBLE"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_name_click_prefers_the_exact_text_match():
    """name='Connexion' matching both 'Connexion' and 'Connexion aide' (substring) picks the
    exact one instead of erroring ambiguous."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            "<button onclick=\"window.h='EXACT'\">Connexion</button>"
            "<button onclick=\"window.h='LONGER'\">Connexion aide</button>"
        )
        locator = await _named_locator(page, ClickAction(name="Connexion"))
        await locator.click()
        assert await page.evaluate("() => window.h") == "EXACT"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_truly_ambiguous_name_error_lists_the_matches():
    """Two identical visible buttons stay ambiguous — but the error now DESCRIBES the matches so
    the agent can refine without a scan round-trip."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            "<nav><button>Valider</button></nav><footer><button>Valider</button></footer>"
        )
        with pytest.raises(ValueError) as e:
            await _named_locator(page, ClickAction(name="Valider"))
        msg = str(e.value)
        assert "ambiguous" in msg and "button" in msg  # candidates described, not just counted
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_final_state_reflects_the_last_action_in_the_batch():
    """#65 (minor): 'Final state' was captured right after a mid-batch click and never refreshed,
    so a later action's effect (a login redirect, an evaluate_js mutation) was missing — the
    summary showed the page as it was mid-batch. It must reflect the state AFTER the whole batch."""
    from interact.actions import EvaluateJsAction
    from interact.actions.dispatch import _run_actions_browser

    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content("<title>BEFORE</title><button onclick=\"1\">Go</button>")
        out = await _run_actions_browser(
            mgr,
            [
                ClickAction(selector="button"),  # sets `final` mid-batch (title BEFORE)
                EvaluateJsAction(script="document.title = 'AFTER'"),
            ],
            None, None, None, "default",
        )
        final = out.split("Final state:")[1]
        assert "AFTER" in final and "BEFORE" not in final
    finally:
        await mgr.close()


# =============================================================================================
# DOM ref scan against the shapes real pages take (#108)
# =============================================================================================


@pytest.mark.asyncio
async def test_a_details_summary_is_a_detected_trigger():
    from interact.server import _scan_elements

    mgr = browser_manager()
    try:
        await ready_or_skip(mgr)
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
async def test_a_tabindex_only_hover_marker_is_a_detected_trigger():
    """A hover/focus explanation marker with no button/link/[role=button] — just `tabindex="0"`
    and a non-interactive-sounding ARIA role like `role="note"` — is a real shipped shape (the
    server hint-spot marker: `<span tabindex="0" role="note">?</span>`, opened on hover
    AND keyboard focus). Before this it was invisible to every scan-based tool (get_interactive_
    elements, run_actions by ref, audit_ui): a tag/role allowlist with no bare-tabindex fallback
    means a real keyboard-operable control is silently never offered as a ref."""
    from interact.server import _scan_elements

    mgr = browser_manager()
    try:
        await ready_or_skip(mgr)
        page = await mgr.get_page()
        await page.set_content(
            '<span tabindex="0" role="note" aria-label="Explanation">?'
            '<span class="hint-pop" popover="manual">Long prose here</span></span>'
        )
        els = await _scan_elements(mgr)
        assert any("Explanation" in (e.name or "") for e in els), (
            f"the tabindex-only marker was not detected: {[e.name for e in els]}"
        )
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_scanning_a_raw_image_document_does_not_throw():
    # Chromium renders a navigated .svg in its standalone image viewer: no <body> at all. The scan
    # must degrade to "nothing to act on" rather than raising (#108).
    from interact.server import _scan_elements

    mgr = browser_manager()
    try:
        await ready_or_skip(mgr)
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


# =============================================================================================
# InteractiveElement: sub-pixel DOM coords resolved at construction
# =============================================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"x": 364.390625, "y": 10.5, "width": 20.2, "height": 5.7}, (364, 10, 20, 6)),
        ({"x": 0.4, "y": 0.6, "w": 100.0, "h": 50.0}, (0, 1, 100, 50)),
    ],
)
def test_interactive_element_rounds_fractional_coords(raw, expected):
    """getBoundingClientRect returns fractional px; the model rounds at construction rather than
    rejecting (the live crash: 'Input should be a valid integer, got 364.390625')."""
    el = InteractiveElement(role="button", name="x", ref="e1", **raw)
    assert (el.x, el.y, el.w, el.h) == expected


# =============================================================================================
# Ambiguous targeting: actionable, ref-nudging error
# =============================================================================================


def test_conflicting_targets_error_names_fields_and_nudges_to_ref():
    with pytest.raises(ValueError) as exc:
        ClickAction(selector="button.x", name="Submit")
    msg = str(exc.value)
    assert "selector" in msg and "name" in msg
    assert "ref" in msg  # nudge toward the stable, unique handle


def _page_with_match_count(n: int) -> MagicMock:
    page = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=n)
    nth = MagicMock()
    nth.is_visible = AsyncMock(return_value=True)  # every match visible → genuinely ambiguous
    nth.evaluate = AsyncMock(return_value="button")
    nth.inner_text = AsyncMock(return_value="Access")
    locator.nth = MagicMock(return_value=nth)
    page.get_by_role = MagicMock(return_value=locator)
    page.get_by_text = MagicMock(return_value=locator)
    return page


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 17])
async def test_named_locator_rejects_ambiguous_with_ref_nudge(count):
    """A name/role target that matches 0 or many elements fails with an actionable message
    (the real run hit a 17-match Playwright strict-mode dump) — and points at get_interactive_
    elements + `ref`, the unique-by-construction recovery."""
    page = _page_with_match_count(count)
    with pytest.raises(ValueError) as exc:
        await _named_locator(page, HoverAction(name="Access", role="button"))
    msg = str(exc.value)
    assert "get_interactive_elements" in msg and "ref" in msg
    if count > 1:
        assert str(count) in msg


@pytest.mark.asyncio
async def test_named_locator_returns_locator_when_unique():
    page = _page_with_match_count(1)
    locator = await _named_locator(page, HoverAction(name="Access", role="button"))
    assert locator is page.get_by_role.return_value
