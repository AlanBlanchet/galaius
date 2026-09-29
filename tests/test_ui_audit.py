"""audit_ui / ui_audit — deterministic component state-machine instrumentation, no VLM.

The owner's own words: he keeps hitting component-level UI defects (dead hover after a route
change, a tray covering its own trigger, lost focus, unrequested movement) that a screenshot +
vision-model critique walks straight past — a still frame cannot show a STATE TRANSITION. This
drives each interactive element through hover / focus / press / open / close and MEASURES what
actually happened (geometry, hit-testing, focus, accessible name, text fit, settle time), never
asks a model's opinion.

Each test below is a MINIMAL fixture reproducing exactly one defect class the owner named, built
with ``page.set_content`` so the defect is deterministic and the assertion is unambiguous — a real
corpus proof (a production app, pre-fix commit) is exercised separately and reported, but
these are the fast, hermetic ground-truth cases for every category the detector claims.
"""

import pytest

from interact.server import _scan_elements
from interact.ui_audit import audit_element, audit_tab_groups

from tests.support import browser_manager, ready_or_skip


async def _ref_for(mgr, selector_hint: str) -> str:
    """The ref of the first scanned element whose name/role contains ``selector_hint`` — tests
    drive real refs through the real scanner (no parallel addressing scheme)."""
    els = await _scan_elements(mgr)
    for el in els:
        if selector_hint.lower() in (el.name or "").lower():
            return el.ref
    raise AssertionError(f"no scanned element matched {selector_hint!r}: {[e.name for e in els]}")


def _categories(findings) -> set[str]:
    return {f.category for f in findings}


@pytest.mark.asyncio
async def test_clean_disclosure_has_no_findings():
    """Negative control: a native <details>/<summary> disclosure with no CSS tricks must come back
    CLEAN — a detector that fires on a well-behaved control is useless (false-positive noise is
    exactly what made agents ignore critiques before)."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            "<details><summary>Art direction</summary><p>Body text, nothing tricky.</p></details>"
        )
        ref = await _ref_for(mgr, "Art direction")
        findings = await audit_element(page, ref)
        assert findings == [], [f.detail for f in findings]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_hover_surface_covers_its_own_trigger():
    """A hover tooltip that renders directly ON TOP of the marker that opened it — 'a tray covers
    its own trigger', named verbatim in the ask."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            """
            <style>
              .marker { position:relative; width:20px; height:20px; }
              .hint { display:none; position:absolute; left:-4px; top:-4px; width:28px; height:28px;
                      background:#fff; role:tooltip; }
              .marker:hover .hint { display:block; }
            </style>
            <button class="marker" aria-label="Info">?
              <span class="hint" role="tooltip">covers the marker</span>
            </button>
            """
        )
        ref = await _ref_for(mgr, "Info")
        findings = await audit_element(page, ref)
        assert "covers_trigger" in _categories(findings), [f.detail for f in findings]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_menu_opens_outside_viewport():
    ref_wrap = """
        <div style="position:fixed; left:10px; top:10px;">
          <button aria-expanded="false" aria-controls="m1" onclick="
            const m=document.getElementById('m1');
            const open = this.getAttribute('aria-expanded')==='true';
            this.setAttribute('aria-expanded', open ? 'false':'true');
            m.style.display = open ? 'none':'block';
          ">Open menu</button>
          <div id="m1" role="menu" style="display:none; position:fixed; left:99999px; top:10px; width:120px; height:60px;">
            <div role="menuitem" tabindex="0">Item</div>
          </div>
        </div>
    """
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(ref_wrap)
        ref = await _ref_for(mgr, "Open menu")
        findings = await audit_element(page, ref)
        assert "outside_viewport" in _categories(findings), [f.detail for f in findings]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_menu_clipped_by_ancestor_overflow():
    html = """
        <div style="width:200px; height:40px; overflow:hidden; position:relative;">
          <button aria-expanded="false" aria-controls="m2" onclick="
            const m=document.getElementById('m2');
            const open=this.getAttribute('aria-expanded')==='true';
            this.setAttribute('aria-expanded', open?'false':'true');
            m.style.display = open?'none':'block';
          ">Filters</button>
          <div id="m2" role="menu" style="display:none; position:absolute; top:30px; left:0; width:150px; height:150px; background:#eee;">
            <div role="menuitem" tabindex="0">A</div>
          </div>
        </div>
    """
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(html)
        ref = await _ref_for(mgr, "Filters")
        findings = await audit_element(page, ref)
        assert "clipped_by_ancestor" in _categories(findings), [f.detail for f in findings]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_hit_test_disagrees_with_geometry():
    """The exact bug class that shipped: a full-bleed overlay (a view-transition pseudo-tree, in
    the real bug) sits on top so a real pointer at the control's own centre never reaches it, even
    though its bounding box is exactly where it always was."""
    html = """
        <button id="btn" style="position:fixed; left:20px; top:20px; width:80px; height:30px;">Click me</button>
        <div id="veil" style="position:fixed; inset:0; background:transparent;"></div>
    """
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(html)
        ref = await _ref_for(mgr, "Click me")
        findings = await audit_element(page, ref)
        assert "hit_test_mismatch" in _categories(findings), [f.detail for f in findings]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_escape_does_not_close_the_surface():
    html = """
        <button aria-expanded="false" aria-controls="m3" onclick="
          const m=document.getElementById('m3');
          this.setAttribute('aria-expanded','true');
          m.style.display='block';
        ">Options</button>
        <div id="m3" role="menu" style="display:none; position:absolute; top:30px; left:0; width:100px; height:60px;">
          <div role="menuitem" tabindex="0">A</div>
        </div>
    """
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(html)
        ref = await _ref_for(mgr, "Options")
        findings = await audit_element(page, ref)
        assert "escape_does_not_close" in _categories(findings), [f.detail for f in findings]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_focus_not_returned_to_trigger_on_close():
    html = """
        <button aria-expanded="false" aria-controls="m4" onclick="
          const m=document.getElementById('m4');
          const open=this.getAttribute('aria-expanded')==='true';
          this.setAttribute('aria-expanded', open?'false':'true');
          m.style.display = open?'none':'block';
          if(!open){ document.getElementById('itm').focus(); }
        ">Actions</button>
        <div id="m4" role="menu" style="display:none; position:absolute; top:30px; left:0; width:100px; height:60px;">
          <div id="itm" role="menuitem" tabindex="0" onkeydown="if(event.key==='Escape'){
            document.querySelector('[aria-controls=m4]').setAttribute('aria-expanded','false');
            document.getElementById('m4').style.display='none';
            document.body.focus();
          }">A</div>
        </div>
    """
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(html)
        ref = await _ref_for(mgr, "Actions")
        findings = await audit_element(page, ref)
        assert "focus_not_returned" in _categories(findings), [f.detail for f in findings]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_keyboard_unreachable_control_is_flagged():
    """A div wired only to onclick/onmouseenter, no role, no tabindex — operable by mouse only."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            '<div role="button" onclick="window.h=1" style="width:80px;height:30px;">Hover-only</div>'
        )
        ref = await _ref_for(mgr, "Hover-only")
        findings = await audit_element(page, ref)
        assert "keyboard_unreachable" in _categories(findings), [f.detail for f in findings]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_accessible_name_leaks_sibling_hint_text():
    """Reproduces the shipped bug shape: a control's computed accessible name pulled in a sibling
    hint bubble's whole explanation, not its own short label — 'accessible name ... not on the
    thing the user operates', named verbatim in the ask."""
    html = """
        <label>
          <span class="value">Balanced</span>
          <button aria-label="Balanced Thinking constraint. Least thinking the model spends before answering; higher costs more but reasons through harder problems." class="choice-control">v</button>
          <span class="hint">Thinking constraint. Least thinking the model spends before answering; higher costs more but reasons through harder problems.</span>
        </label>
    """
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(html)
        ref = await _ref_for(mgr, "Least thinking")
        findings = await audit_element(page, ref)
        assert "name_leak" in _categories(findings), [f.detail for f in findings]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_text_wraps_to_one_glyph_per_line():
    """Reproduces the shipped shape: a flex item squeezed to ~35px wide by a row with no wrap
    guard, so its text wraps to about one glyph per line instead of clipping or reflowing."""
    html = """
        <div style="display:flex; width:60px;">
          <button style="width:24px; overflow:hidden; white-space:normal; word-break:break-all;">
            Connected and ready to answer questions about your account
          </button>
        </div>
    """
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(html)
        ref = await _ref_for(mgr, "Connected")
        findings = await audit_element(page, ref)
        assert "text_wrap_degenerate" in _categories(findings), [f.detail for f in findings]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_hover_reveal_does_not_reengage_after_view_transition_swap():
    """The exact mechanism behind the shipped 'every hover dead after a route change' bug: a real
    View Transition replaces the DOM while the pointer sits still. The browser's OWN :hover state
    recomputes correctly on the new element (no bug there) — the app's JS-driven hover-reveal
    affordance (a popover wired only to `pointerenter`) never gets a fresh pointer EVENT, so it
    never opens, even though the real cursor is sitting right on top of it. A screenshot cannot
    show this; a still frame after the swap looks identical to one where hover just never got
    tried. This needs the real browser engine's View Transition + native :hover recomputation,
    not a synthetic DOM diff."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            """
            <button id="nav" style="position:fixed; left:20px; top:20px; width:80px; height:30px;">Go</button>
            <div id="root"></div>
            <script>
              const root = document.getElementById('root');
              function renderA() {
                root.innerHTML = '<span></span>';
              }
              function renderB() {
                // The new marker lands EXACTLY where the trigger used to be — the pointer, having
                // just clicked "Go" at (60,35), is already resting there when this paints.
                root.innerHTML = '<span id="marker" data-hint style="position:fixed; left:20px; top:20px; width:80px; height:30px; background:#eee;">'
                  + '<span class="hint-pop" popover="manual" style="display:none;">explanation</span></span>';
                document.getElementById('marker').addEventListener('pointerenter', (e) => {
                  e.target.querySelector('.hint-pop').style.display = 'block';
                });
              }
              renderA();
              document.getElementById('nav').addEventListener('click', () => {
                if (document.startViewTransition) {
                  document.startViewTransition(() => { renderB(); document.getElementById('nav').remove(); });
                } else {
                  renderB();
                  document.getElementById('nav').remove();
                }
              });
            </script>
            """
        )
        ref = await _ref_for(mgr, "Go")
        findings = await audit_element(page, ref)
        assert "hover_reveal_stuck_after_navigation" in _categories(findings), [f.detail for f in findings]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_offscreen_element_is_scrolled_into_view_before_auditing():
    """A page scan returns below-the-fold elements too (ranked last, still returned) — without
    scrolling to them first every one reads inViewport=False and is silently never hovered,
    focused, or clicked. 'Finds every interactive element' means this one too."""
    html = """
        <div style="height:2000px;"></div>
        <button aria-label="Balanced Thinking constraint. Least thinking the model spends before answering; higher costs more but reasons through harder problems." style="width:60px;">v</button>
    """
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(html)
        ref = await _ref_for(mgr, "Least thinking")
        findings = await audit_element(page, ref)
        # The name-leak check only fires from a snapshot taken AFTER the element is actually in
        # view — proof the scroll happened, not just that the static check ran off stale geometry.
        assert "name_leak" in _categories(findings), [f.detail for f in findings]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_never_settling_animation_is_flagged():
    html = """
        <style>
          @keyframes spin { from { transform:rotate(0deg); } to { transform:rotate(360deg); } }
          .busy { animation: spin 0.3s linear infinite; }
        </style>
        <button class="busy" onclick="this.classList.add('busy')">Sync</button>
    """
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(html)
        ref = await _ref_for(mgr, "Sync")
        findings = await audit_element(page, ref)
        assert "never_settles" in _categories(findings), [f.detail for f in findings]
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_broken_tab_panel_swallowed_by_markup_is_flagged():
    """Reproduces the shipped shape directly, no ARIA needed: a page-switcher of same-parent
    sibling buttons (this exact app wires its agent-editor sub-nav with a shared `data-agent-
    page` attribute and no `role=tab`/`aria-controls` at all) where ONE target panel renders
    empty — the observable symptom of 'three edits had dropped a </b>, so the parser swallowed
    two whole agent tabs into a hidden form', regardless of what actually broke it. A generic
    sibling-parity check needs no ARIA hooks: it clicks each switcher in the group and flags
    the one whose resulting content is near-empty while its siblings produce real content."""
    html = """
        <nav>
          <button data-page="team">Team</button>
          <button data-page="tools">Tools</button>
          <button data-page="model">Model</button>
        </nav>
        <section id="panel"></section>
        <script>
          const panel = document.getElementById('panel');
          function render(page) {
            if (page === 'team') panel.innerHTML = '<h2>Team</h2><p>Delegation and reporting lines for this agent, with a roster of teammates and their roles.</p>';
            else if (page === 'tools') panel.innerHTML = '';  // the swallowed panel
            else panel.innerHTML = '<h2>Model</h2><p>Pick a provider and a model this agent is allowed to use, with thinking effort and token limits.</p>';
          }
          document.querySelectorAll('[data-page]').forEach((b) => b.addEventListener('click', () => render(b.dataset.page)));
          render('team');
        </script>
    """
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(html)
        els = await _scan_elements(mgr)
        refs = [e.ref for e in els if e.name in ("Team", "Tools", "Model")]
        assert len(refs) == 3
        findings = await audit_tab_groups(page)
        matches = [f for f in findings if f.category == "sibling_content_empty"]
        assert matches, [f.detail for f in findings]
        assert "Tools" in matches[0].detail
    finally:
        await mgr.close()
