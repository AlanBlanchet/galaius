"""audit_ui — deterministic component state-machine instrumentation, no VLM.

Mining real complaints showed the same shape every time: a still screenshot fed to a vision model
cannot show that a menu closed the instant the pointer moved toward it, that a tray covers its own
trigger, that focus vanished, that the page shifted 12px when something opened. A model's opinion
on a frozen frame is not evidence for a STATE TRANSITION defect. This drives each interactive
element through its real states (hover, focus, press, open, close) with the actual browser engine
— Playwright dispatching real events, real hit-testing, real CSSOM — and reports only what it
MEASURED: geometry before/after, `elementFromPoint` agreement, `document.activeElement`, computed
accessible name vs the text actually painted, text-box fit, and whether animation/layout ever
stops. A vision model may corroborate a finding's severity; it never gets to invent one.

Reuses the ref scan every other browser tool already runs (`interact.server.capture._scan_
elements`, `data-interact-ref`) — one addressing scheme, not a parallel one — and the existing
bounded settle wait (`interact.settle.settle_page`) to decide "did this ever stop moving".

Scope (named, not silent): browser sessions only. A desktop/AT-SPI equivalent is a real future
capability (DesktopElement already carries geometry) but has no second caller yet — right-altitude
says build it when one exists, not speculatively here.
"""

from __future__ import annotations

import re
from typing import Literal

from playwright.async_api import Page
from pydantic import BaseModel

from interact.settle import settle_page
from interact.state import ref_locator

Severity = Literal["critical", "major", "minor"]

# One page.evaluate per phase: locate the element by its stable ref, then read everything a state
# comparison needs in ONE round trip (rect, ancestor chain + scroll state, siblings, hit-test,
# focus, accessible name, own painted text, text-box fit). Kept as one function so before/after
# reads are the same shape and a diff is a plain dict walk, not a schema reconciliation.
_SNAPSHOT_JS = r"""
({ ref }) => {
  const el = document.querySelector(`[data-interact-ref="${ref}"]`);
  if (!el) return { found: false };
  const vw = window.innerWidth, vh = window.innerHeight;
  const rectOf = (n) => { const r = n.getBoundingClientRect(); return { x:r.x, y:r.y, width:r.width, height:r.height }; };
  const accessibleName = (n) => {
    const img = n.querySelector && n.querySelector('img');
    return (
      n.getAttribute('aria-label') || n.value || n.getAttribute('placeholder') ||
      n.getAttribute('title') || n.textContent ||
      (img && (img.getAttribute('alt') || img.getAttribute('aria-label'))) || ''
    ).trim().replace(/[​-‍⁠﻿]/g, '').replace(/\s+/g, ' ').slice(0, 300);
  };
  let ownVisibleText = '';
  try { ownVisibleText = (el.innerText ?? el.value ?? '').trim().replace(/\s+/g, ' ').slice(0, 300); }
  catch (e) { ownVisibleText = ''; }
  const ancestors = [];
  for (let n = el.parentElement, depth = 0; n && depth < 10; n = n.parentElement, depth++) {
    const s = getComputedStyle(n), r = rectOf(n);
    ancestors.push({ tag: n.tagName.toLowerCase(), id: n.id || '', ...r,
      overflowX: s.overflowX, overflowY: s.overflowY,
      scrollLeft: n.scrollLeft, scrollTop: n.scrollTop,
      scrollWidth: n.scrollWidth, scrollHeight: n.scrollHeight });
    if (n === document.body) break;
  }
  const parent = el.parentElement;
  const siblings = parent ? Array.from(parent.children).filter((c) => c !== el).slice(0, 40)
    .map((c) => ({ tag: c.tagName.toLowerCase(), ref: c.getAttribute('data-interact-ref') || '', ...rectOf(c) })) : [];
  const rect = rectOf(el);
  const cx = rect.x + rect.width / 2, cy = rect.y + rect.height / 2;
  // rectOf() strips DOMRect down to {x,y,width,height} (a plain, JSON-serialisable shape) — it has
  // no .top/.right/.bottom/.left, so those must be derived here rather than read off `rect` again.
  const inViewport = rect.y + rect.height > 0 && rect.x + rect.width > 0 && rect.y < vh && rect.x < vw;
  let hit = null;
  if (inViewport && cx >= 0 && cy >= 0 && cx <= vw && cy <= vh) {
    const h = document.elementFromPoint(cx, cy);
    hit = { matches: !!h && (h === el || el.contains(h) || h.contains(el)), tag: h ? h.tagName.toLowerCase() : null };
  }
  const s = getComputedStyle(el);
  const nativelyFocusable = /^(a|button|input|select|textarea|summary)$/.test(el.tagName.toLowerCase());
  const keyboardReachable = nativelyFocusable ? el.tabIndex !== -1 : el.tabIndex >= 0;
  return {
    found: true, rect, inViewport, ancestors, siblings,
    page: { scrollX: window.scrollX, scrollY: window.scrollY,
      docWidth: document.documentElement.scrollWidth, docHeight: document.documentElement.scrollHeight, vw, vh },
    hit, focused: document.activeElement === el, tabIndex: el.tabIndex, keyboardReachable,
    role: el.getAttribute('role') || el.tagName.toLowerCase(),
    target: el.getAttribute('target') || '',
    name: accessibleName(el), ownVisibleText,
    ariaExpanded: el.getAttribute('aria-expanded'), ariaControls: el.getAttribute('aria-controls') || '',
    textFit: { clientWidth: el.clientWidth, scrollWidth: el.scrollWidth, clientHeight: el.clientHeight, scrollHeight: el.scrollHeight },
    pointerEventsNone: s.pointerEvents === 'none',
  };
}
"""

# ARIA-standard OVERLAY roles only (never this app's own CSS class names) — a generic detector
# that only fires on THIS project's naming would be worthless the moment a component is renamed.
# Deliberately excludes plain <details> content: an inline disclosure is normal flow content, not
# a floating surface, and does not owe Escape-to-close or viewport-containment semantics.
_SURFACES_JS = r"""
() => {
  const sel = '[role=menu],[role=listbox],[role=dialog],[role=tooltip],[role=alertdialog],'
            + '[popover],[aria-modal="true"],dialog[open]';
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  for (const n of document.querySelectorAll(sel)) {
    const r = n.getBoundingClientRect();
    const s = getComputedStyle(n);
    if (r.width <= 0 || r.height <= 0 || s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) continue;
    const focusable = n.querySelectorAll('button,a[href],input,select,textarea,[tabindex]:not([tabindex="-1"])').length;
    // Ancestor-clip: walk up; an ancestor with overflow hidden/clip/scroll/auto whose own box does
    // not fully contain this surface's rect is clipping it.
    let clippedBy = null;
    for (let a = n.parentElement; a && a !== document.body; a = a.parentElement) {
      const as = getComputedStyle(a);
      if (!/(hidden|clip|scroll|auto)/.test(as.overflowX + as.overflowY)) continue;
      const ar = a.getBoundingClientRect();
      if (r.left < ar.left - 0.5 || r.top < ar.top - 0.5 || r.right > ar.right + 0.5 || r.bottom > ar.bottom + 0.5) {
        clippedBy = a.tagName.toLowerCase() + (a.id ? '#' + a.id : '');
        break;
      }
    }
    out.push({
      tag: n.tagName.toLowerCase(), role: n.getAttribute('role') || n.tagName.toLowerCase(),
      rect: { x: r.x, y: r.y, width: r.width, height: r.height },
      outsideViewport: r.left < -0.5 || r.top < -0.5 || r.right > vw + 0.5 || r.bottom > vh + 0.5,
      clippedBy, focusableCount: focusable,
    });
  }
  return out;
}
"""

# Is THIS element (or something inside it) still animating, right now — scoped to the interacted
# component, never the whole document (an unrelated decorative spinner elsewhere must not flag
# every control on the page). Unlike ``settle_page``'s own wait, this deliberately does NOT exclude
# infinite-iteration animations: settle_page ignores them so a real spinner can't hang a capture
# forever, but here the question is the opposite — after settle_page's bounded wait already ran,
# is this control STILL moving. An infinite spin still going on a plain button post-interaction is
# exactly "state that never settles", not a decorative loader.
_ANIMATING_JS = """
(ref) => {
  const el = document.querySelector(`[data-interact-ref="${ref}"]`);
  if (!el) return false;
  try { return el.getAnimations({ subtree: true }).some((a) => a.playState === 'running'); }
  catch (e) { return false; }
}
"""

# The shipped "every hover dead after a route change" bug, generalised: a View Transition (or any
# DOM replacement) swaps content while the pointer sits still. `elementFromPoint` always performs a
# FRESH hit test regardless of event history, so it correctly finds whatever now sits at (x, y) —
# that part is native and never wrong. What breaks is a JS-driven hover-reveal wired only to a
# `pointerenter` EVENT, which never re-fires for a pointer that never moved: real browsers do NOT
# retroactively recompute `:hover` just because the DOM under a stationary cursor changed (measured
# directly — `:hover` stayed false here even though elementFromPoint at the same coordinates finds
# the new element). So the deterministic signal is elementFromPoint plus the reveal's own shown/
# hidden state, not `:hover` matching.
_HOVER_STUCK_JS = r"""
({ x, y }) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return null;
  const isRevealed = (n) => {
    const s = getComputedStyle(n);
    if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
    if (n.hasAttribute('popover')) return n.matches(':popover-open') || s.display !== 'none';
    return true;
  };
  // Self, or a shallow descendant (the reveal is usually nested one or two levels inside its
  // trigger) that carries a popover attribute.
  const candidates = [el, ...el.querySelectorAll('[popover]')].filter((n) => n.hasAttribute('popover'));
  if (!candidates.length) return null;
  return { tag: el.tagName.toLowerCase(), hasReveal: true, revealed: candidates.some(isRevealed) };
}
"""

_EPS = 1.0  # px tolerance for subpixel rounding noise, same order as InteractiveElement's own round()


class Finding(BaseModel):
    category: str
    severity: Severity
    state: str  # which transition surfaced it: hover / focus / open / close / static
    detail: str
    evidence: dict = {}


def _rect_moved(a: dict, b: dict) -> bool:
    return any(abs(a[k] - b[k]) > _EPS for k in ("x", "y", "width", "height"))


def _geometry_findings(before: dict, after: dict, state: str, *, expect_revert: bool) -> list[Finding]:
    """Unrequested movement: every ancestor and sibling's own rect + every ancestor's scroll
    extent, read before and after. On hover/focus (``expect_revert=True``) NOTHING should move —
    those are observation states, not user-requested changes."""
    out: list[Finding] = []
    if not expect_revert:
        return out
    # Positional pairing, not keyed by (tag, id): most ancestors are anonymous divs with id="" —
    # keying by (tag, id) collapsed every anonymous ancestor onto the SAME dict slot and diffed
    # unrelated divs at different depths against each other. The parentElement walk order is
    # deterministic for the SAME ref across two calls (same DOM), so position is the right key;
    # a tag mismatch at a position means the ancestor chain itself changed shape, which this
    # positional diff correctly stops comparing rather than reporting as movement.
    for prior, a in zip(before["ancestors"], after["ancestors"]):
        if prior["tag"] != a["tag"]:
            continue
        if _rect_moved(prior, a):
            out.append(Finding(
                category="unrequested_movement", severity="major", state=state,
                detail=f"ancestor <{a['tag']}{'#' + a['id'] if a['id'] else ''}> moved/resized on {state}: "
                       f"{prior['x']:.0f},{prior['y']:.0f} {prior['width']:.0f}x{prior['height']:.0f} -> "
                       f"{a['x']:.0f},{a['y']:.0f} {a['width']:.0f}x{a['height']:.0f}",
                evidence={"before": prior, "after": a},
            ))
        for axis in ("scrollWidth", "scrollHeight"):
            if abs(prior[axis] - a[axis]) > _EPS:
                out.append(Finding(
                    category="unrequested_movement", severity="minor", state=state,
                    detail=f"ancestor <{a['tag']}> {axis} changed on {state}: {prior[axis]} -> {a[axis]}",
                    evidence={"before": prior[axis], "after": a[axis]},
                ))
    # Same positional-pairing fix as ancestors: keying a MOVED sibling by its own (tag, x, y)
    # guarantees it never matches its prior self (the coordinates are the very thing that changed),
    # so the old before/after-dict-by-position-derived-key approach silently missed every real
    # move. Prefer matching by `ref` (stable identity for a scanned node); fall back to DOM position
    # for an un-refed sibling (plain text nodes' wrapper, decorative spans).
    b_sib_by_ref = {s["ref"]: s for s in before["siblings"] if s["ref"]}
    b_positional = [s for s in before["siblings"] if not s["ref"]]
    a_positional_idx = 0
    for s in after["siblings"]:
        if s["ref"]:
            prior = b_sib_by_ref.get(s["ref"])
        else:
            prior = b_positional[a_positional_idx] if a_positional_idx < len(b_positional) else None
            a_positional_idx += 1
        if prior is None or prior["tag"] != s["tag"]:
            continue
        if _rect_moved(prior, s):
            out.append(Finding(
                category="unrequested_movement", severity="major", state=state,
                detail=f"sibling <{s['tag']}> moved on {state}: "
                       f"{prior['x']:.0f},{prior['y']:.0f} -> {s['x']:.0f},{s['y']:.0f}",
                evidence={"before": prior, "after": s},
            ))
    if abs(before["page"]["docWidth"] - after["page"]["docWidth"]) > _EPS or \
       abs(before["page"]["docHeight"] - after["page"]["docHeight"]) > _EPS:
        out.append(Finding(
            category="unrequested_movement", severity="major", state=state,
            detail=f"page grew/shrank on {state}: "
                   f"{before['page']['docWidth']}x{before['page']['docHeight']} -> "
                   f"{after['page']['docWidth']}x{after['page']['docHeight']}",
            evidence={"before": before["page"], "after": after["page"]},
        ))
    return out


_WORD_RE = re.compile(r"[a-zA-Z0-9']+")


def _name_leak_finding(snap: dict, state: str) -> Finding | None:
    """Accessible name bloated with text the control does not itself paint — the measurable shape
    of 'a choice control took its accessible name from a hint bubble' and 'accessible name taken
    from a hidden native control behind it': the name is long, sentence-shaped, and the control's
    own visible text is a small fragment of it (or absent)."""
    name, own = snap["name"], snap["ownVisibleText"]
    if not name or len(name) < 40:
        return None
    name_words = _WORD_RE.findall(name.lower())
    own_words = set(_WORD_RE.findall(own.lower()))
    if len(name_words) < 6:
        return None
    overlap = sum(1 for w in name_words if w in own_words) / len(name_words)
    sentence_shaped = bool(re.search(r"[.?;:]\s+\w", name))
    if overlap < 0.5 and (sentence_shaped or len(name_words) > 12):
        return Finding(
            category="name_leak", severity="major", state=state,
            detail=f"accessible name ({len(name)} chars, {len(name_words)} words) shares only "
                   f"{overlap:.0%} of its words with the control's own visible text {own!r} — "
                   f"looks pulled in from elsewhere: {name!r}",
            evidence={"name": name, "own_visible_text": own},
        )
    return None


def _text_fit_findings(snap: dict, state: str, page_text_lines: int | None = None) -> list[Finding]:
    out = []
    tf = snap["textFit"]
    if tf["scrollWidth"] - tf["clientWidth"] > 2 and tf["clientWidth"] > 0:
        out.append(Finding(
            category="text_overflow", severity="minor", state=state,
            detail=f"text overflows its box horizontally: content {tf['scrollWidth']}px in a "
                   f"{tf['clientWidth']}px box",
            evidence=tf,
        ))
    if page_text_lines is not None and page_text_lines > 0:
        avg_chars = len(snap["ownVisibleText"]) / page_text_lines if page_text_lines else 0
        if page_text_lines >= 3 and avg_chars < 2.5 and len(snap["ownVisibleText"]) > 6:
            out.append(Finding(
                category="text_wrap_degenerate", severity="major", state=state,
                detail=f"text wraps to ~{avg_chars:.1f} chars/line over {page_text_lines} lines — "
                       f"reads as one glyph per line: {snap['ownVisibleText']!r}",
                evidence={"lines": page_text_lines, "chars_per_line": avg_chars},
            ))
    return out


async def _snapshot(page: Page, ref: str) -> dict:
    return await page.evaluate(_SNAPSHOT_JS, {"ref": ref})


async def _line_count(page: Page, ref: str) -> int | None:
    """Visual line-fragment count for the element's own text — a Range over its text nodes yields
    one client rect per wrapped line, independent of character width or locale."""
    try:
        return await page.evaluate(
            """(ref) => {
                const el = document.querySelector(`[data-interact-ref="${ref}"]`);
                if (!el) return null;
                const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                const range = document.createRange();
                let any = false;
                range.setStart(el, 0);
                let last = el, lastOffset = el.childNodes.length;
                let n;
                while ((n = walker.nextNode())) { last = n; lastOffset = n.textContent.length; any = true; }
                if (!any) return 0;
                range.setEnd(last, lastOffset);
                return range.getClientRects().length;
            }""",
            ref,
        )
    except Exception:
        return None


async def audit_element(page: Page, ref: str, *, click: bool = True) -> list[Finding]:
    """Drive ONE already-scanned element (by its stable ``data-interact-ref``) through hover,
    focus, and — unless it looks like it would navigate or submit — a press/open/close cycle,
    measuring every defect class named in the ask. Returns findings only; a clean control returns
    an empty list (see the negative-control test)."""
    findings: list[Finding] = []
    locator = page.locator(ref_locator(ref))
    # A page-scan ranks off-screen elements last but still returns them (tier 2 in the shared DOM
    # scan) — without scrolling first, every one of them read inViewport=False and was silently
    # never hovered, focused, or clicked. "Finds every interactive element" means below-the-fold
    # ones too; best-effort, since a genuinely unreachable node (display:none ancestor) just stays
    # off-screen and the rest of this function already tolerates that.
    try:
        await locator.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    baseline = await _snapshot(page, ref)
    if not baseline["found"]:
        return findings

    # Own accessible-name / text-fit checks don't need any interaction — run once on the resting
    # state (also runs again after open, since some names are computed only once the surface exists).
    if f := _name_leak_finding(baseline, "static"):
        findings.append(f)
    lines = await _line_count(page, ref)
    findings.extend(_text_fit_findings(baseline, "static", lines))
    if not baseline["keyboardReachable"]:
        findings.append(Finding(
            category="keyboard_unreachable", severity="critical", state="static",
            detail=f"{baseline['role']} {baseline['name']!r} has no native/tabindex focus path — "
                   f"reachable by pointer only",
            evidence={"tabIndex": baseline["tabIndex"], "role": baseline["role"]},
        ))

    # Static hit-test: read straight off the baseline snapshot, BEFORE any interaction attempt.
    # This matters because Playwright's own `.hover()`/`.click()` refuse to act on an element that
    # fails their actionability check (something else is receiving pointer events there) — using
    # those APIs to drive the probe would silently SKIP exactly the pages this tool exists to catch.
    if baseline["hit"] is not None and not baseline["hit"]["matches"]:
        findings.append(Finding(
            category="hit_test_mismatch", severity="critical", state="static",
            detail=f"elementFromPoint at the control's own centre returns <{baseline['hit']['tag']}>, "
                   f"not the control — a real pointer here would miss it",
            evidence=baseline["hit"],
        ))

    # --- hover --- raw mouse.move to the measured centre, NOT locator.hover(): Playwright's
    # hover() enforces actionability first and times out (silently, if caught) on exactly the
    # covered-element case this tool must observe, not skip.
    cx = baseline["rect"]["x"] + baseline["rect"]["width"] / 2
    cy = baseline["rect"]["y"] + baseline["rect"]["height"] / 2
    if baseline["inViewport"]:
        await page.mouse.move(cx, cy)
        await settle_page(page)
        hovered = await _snapshot(page, ref)
        findings.extend(_geometry_findings(baseline, hovered, "hover", expect_revert=True))
        if hovered["hit"] is not None and not hovered["hit"]["matches"]:
            findings.append(Finding(
                category="hit_test_mismatch", severity="critical", state="hover",
                detail=f"elementFromPoint at the control's own centre returns <{hovered['hit']['tag']}>, "
                       f"not the control after hovering it — a real pointer here would miss it",
                evidence=hovered["hit"],
            ))
        surfaces = await page.evaluate(_SURFACES_JS)
        findings.extend(_surface_findings(baseline["rect"], surfaces, "hover"))
        await page.mouse.move(0, 0)
        await settle_page(page)
        unhovered = await _snapshot(page, ref)
        findings.extend(_geometry_findings(baseline, unhovered, "hover-leave", expect_revert=True))

    # --- focus --- only meaningful when the control can actually take it; already flagged above.
    if baseline["keyboardReachable"]:
        try:
            await locator.focus(timeout=2000)
            focused = await _snapshot(page, ref)
            if not focused["focused"]:
                findings.append(Finding(
                    category="focus_not_landed", severity="major", state="focus",
                    detail="programmatic focus() did not land on the control (activeElement is elsewhere)",
                    evidence={},
                ))
            findings.extend(_geometry_findings(baseline, focused, "focus", expect_revert=True))
        except Exception:
            pass

    # --- press / open / close ---
    # A same-document SPA link (hash/pushState routing) IS driven: its click is exactly where the
    # shipped "hover dead after a route change" bug lives, and the pointer stays at (cx, cy) across
    # it — the real repro. Only an EXTERNAL link (target="_blank", a genuinely new tab/document) is
    # skipped, since that opens a real cross-site tab this probe has no business visiting.
    skip_click = baseline["role"] == "a" and baseline["target"] == "_blank"
    if click and not skip_click and baseline["inViewport"]:
        # Raw mouse down/up at the measured centre, NOT locator.click(): Playwright's click()
        # first waits for the element's box to be STABLE across animation frames — exactly what a
        # control stuck mid-animation (the never-settles class) never is, so the semantic action
        # times out and the whole probe would silently skip it.
        try:
            before_url = page.url
            before_surfaces = await page.evaluate(_SURFACES_JS)
            await page.mouse.move(cx, cy)
            await page.mouse.down()
            await page.mouse.up()
            await settle_page(page)
            navigated = page.url != before_url
            # The pointer never moved (cx, cy is where it still is): whatever is now under it should
            # be exactly where a real hit test finds it. If a hover-reveal exists there and isn't
            # showing, its trigger fired only on the pointerenter EVENT this DOM swap never dispatched.
            stuck = await page.evaluate(_HOVER_STUCK_JS, {"x": cx, "y": cy})
            if stuck and stuck["hasReveal"] and not stuck["revealed"]:
                findings.append(Finding(
                    category="hover_reveal_stuck_after_navigation", severity="major",
                    state="open" if navigated else "static",
                    detail=f"the pointer rests on <{stuck['tag']}> after this control replaced the "
                           f"page content, but its hover-reveal never opened — nothing re-checks "
                           f"hover state after a DOM swap under a stationary pointer",
                    evidence=stuck,
                ))
            after_click = await _snapshot(page, ref)
            if navigated or not after_click["found"]:
                # Nothing else below is safe to compare: `ref`'s element (gone — navigation, or this
                # very click replaced it, as a DOM-swapping trigger legitimately can), its old
                # ancestors/siblings, and geometry/focus-return all belonged to the prior page state.
                return findings
            after_surfaces = await page.evaluate(_SURFACES_JS)
            opened = _new_surfaces(before_surfaces, after_surfaces)
            if f := _name_leak_finding(after_click, "open"):
                findings.append(f)
            if opened:
                findings.extend(_surface_findings(baseline["rect"], opened, "open"))
                if after_click["focused"] is False and not any(s["focusableCount"] for s in opened):
                    pass  # a menu with no focusable items yet is a design choice, not measured here
                # never-settles: after the click + the bounded settle above, is anything STILL animating?
                if await page.evaluate(_ANIMATING_JS, ref):
                    findings.append(Finding(
                        category="never_settles", severity="minor", state="open",
                        detail="animation/layout still running well past the bounded settle wait",
                        evidence={},
                    ))
                await page.keyboard.press("Escape")
                await settle_page(page)
                closed_surfaces = _new_surfaces(before_surfaces, await page.evaluate(_SURFACES_JS))
                after_close = await _snapshot(page, ref)
                if closed_surfaces:
                    findings.append(Finding(
                        category="escape_does_not_close", severity="critical", state="close",
                        detail=f"{len(closed_surfaces)} surface(s) opened by this control are still "
                               f"present after Escape",
                        evidence={"still_open": closed_surfaces},
                    ))
                else:
                    if not after_close["focused"]:
                        findings.append(Finding(
                            category="focus_not_returned", severity="major", state="close",
                            detail="focus did not return to the trigger after the surface closed",
                            evidence={},
                        ))
                    findings.extend(_geometry_findings(baseline, after_close, "close", expect_revert=True))
            else:
                if await page.evaluate(_ANIMATING_JS, ref):
                    findings.append(Finding(
                        category="never_settles", severity="minor", state="static",
                        detail="animation still running well past the bounded settle wait "
                               "(triggered by interacting with this control)",
                        evidence={},
                    ))
        except Exception:
            pass

    return findings


def _new_surfaces(before: list[dict], after: list[dict]) -> list[dict]:
    def key(s: dict) -> tuple:
        r = s["rect"]
        return (s["tag"], round(r["x"]), round(r["y"]), round(r["width"]), round(r["height"]))

    before_keys = {key(s) for s in before}
    return [s for s in after if key(s) not in before_keys]


def _rects_overlap(a: dict, b: dict) -> bool:
    return not (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
                or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])


def _surface_findings(trigger_rect: dict, surfaces: list[dict], state: str) -> list[Finding]:
    out = []
    for s in surfaces:
        if s["outsideViewport"]:
            out.append(Finding(
                category="outside_viewport", severity="critical", state=state,
                detail=f"<{s['tag']} role={s['role']}> opens partly/fully outside the viewport: "
                       f"{s['rect']}",
                evidence=s,
            ))
        if s["clippedBy"]:
            out.append(Finding(
                category="clipped_by_ancestor", severity="critical", state=state,
                detail=f"<{s['tag']} role={s['role']}> is clipped by ancestor {s['clippedBy']} "
                       f"(overflow hidden/clip/scroll)",
                evidence=s,
            ))
        if _rects_overlap(trigger_rect, s["rect"]):
            out.append(Finding(
                category="covers_trigger", severity="major", state=state,
                detail=f"<{s['tag']} role={s['role']}> renders on top of its own trigger",
                evidence={"trigger": trigger_rect, "surface": s["rect"]},
            ))
    return out


# A generalisable, ARIA-free catch for "a tab/page-switcher's target panel renders empty" — the
# OBSERVABLE symptom behind the shipped "three edits had dropped a </b>, so the parser swallowed
# two whole agent tabs into a hidden form" bug. That bug's actual mechanism (a browser innerHTML
# parser silently re-nesting content under a mismatched close tag) is NOT a state-transition
# defect — it needs a markup-validity linter, a different instrument, to diagnose the CAUSE. But
# its SYMPTOM — switching to one sibling view produces real content, switching to another produces
# almost none — is exactly the shape a click-and-measure walker CAN catch, generically, with no
# assumption about aria-controls/role=tab (the server app this was found in uses NEITHER:
# its own page-switcher is same-parent sibling buttons that vary only in one data-* attribute).
_TAB_GROUPS_JS = r"""
() => {
  const groups = [];
  const seenParents = new Set();
  for (const el of document.querySelectorAll('[data-interact-ref]')) {
    const parent = el.parentElement;
    if (!parent || seenParents.has(parent)) continue;
    const varyingKeys = (node) => Object.keys(node.dataset).filter((k) => k !== 'interactRef');
    const siblings = Array.from(parent.children).filter((c) => c.hasAttribute('data-interact-ref'));
    if (siblings.length < 2 || siblings.length > 8) continue;
    const keySets = siblings.map(varyingKeys);
    if (keySets.some((k) => k.length !== 1)) continue;  // exactly one shared varying data-* key each
    const key = keySets[0][0];
    if (!keySets.every((k) => k[0] === key)) continue;  // same key across every sibling
    const values = siblings.map((s) => s.dataset[key]);
    if (new Set(values).size !== values.length) continue;  // must all differ — a real switcher, not repeated identical controls
    seenParents.add(parent);
    groups.push(siblings.map((s) => ({
      ref: s.getAttribute('data-interact-ref'),
      name: (s.getAttribute('aria-label') || s.textContent || '').trim().slice(0, 60),
    })));
  }
  return groups;
}
"""

_MAX_TAB_GROUPS = 5
_SIBLING_EMPTY_RATIO = 0.2
_SIBLING_EMPTY_FLOOR = 30  # chars — below this the group's OWN max content is too thin to compare against


async def audit_tab_groups(page: Page) -> list[Finding]:
    """Find same-parent sibling switchers (share one varying `data-*` value, no aria-controls
    needed) and click through each, comparing the resulting page text length. A member whose
    resulting content is a small fraction of its siblings' is flagged — regardless of WHY it's
    thin (markup bug, JS bug, a genuinely broken fetch); the walker measures the symptom."""
    findings: list[Finding] = []
    try:
        groups = await page.evaluate(_TAB_GROUPS_JS)
    except Exception:
        return findings
    for group in groups[:_MAX_TAB_GROUPS]:
        lengths: dict[str, int] = {}
        for member in group:
            loc = page.locator(ref_locator(member["ref"]))
            try:
                box = await loc.bounding_box()
                if not box:
                    continue
                cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                await page.mouse.move(cx, cy)
                await page.mouse.down()
                await page.mouse.up()
                await settle_page(page)
                lengths[member["name"]] = await page.evaluate("() => document.body.innerText.trim().length")
            except Exception:
                continue
        if len(lengths) < 2:
            continue
        top = max(lengths.values())
        if top < _SIBLING_EMPTY_FLOOR:
            continue
        for name, length in lengths.items():
            if length < top * _SIBLING_EMPTY_RATIO:
                findings.append(Finding(
                    category="sibling_content_empty", severity="critical", state="open",
                    detail=f"switching to {name!r} in this tab-like group produced only {length} "
                           f"chars of visible text; its sibling views produce up to {top} — the "
                           f"panel this control activates may be swallowed by a markup or state bug",
                    evidence={"lengths": lengths},
                ))
    return findings


class AuditReport(BaseModel):
    scanned: int
    audited: int
    skipped: list[str] = []
    findings: list[Finding] = []


async def audit_page(mgr, tab: int | None = None, scope: str | None = None, limit: int = 20) -> AuditReport:
    """Scan the page's interactive elements (the same DOM scan every browser tool uses) and drive
    up to ``limit`` of them through :func:`audit_element`, plus one page-wide sweep for sibling
    tab-like switchers (:func:`audit_tab_groups`). ``limit`` bounds cost on a busy page —
    scanned-but-not-audited elements are named in ``skipped``, never silently dropped."""
    from interact.server.capture import _scan_elements  # local import: avoids a server->this cycle

    page = await mgr.get_page(tab)
    elements = await _scan_elements(mgr, tab, scope)
    findings: list[Finding] = []
    skipped: list[str] = []
    audited = 0
    for el in elements[:limit]:
        try:
            el_findings = await audit_element(page, el.ref)
        except Exception as exc:  # one broken control must not abort the whole sweep
            skipped.append(f"{el.role} {el.name!r} (ref={el.ref}): {exc}")
            continue
        for f in el_findings:
            f.evidence = {**f.evidence, "ref": el.ref, "element": f"{el.role} {el.name!r}"}
        findings.extend(el_findings)
        audited += 1
    for el in elements[limit:]:
        skipped.append(f"{el.role} {el.name!r} (ref={el.ref}): over the {limit}-element audit budget")
    try:
        findings.extend(await audit_tab_groups(page))
    except Exception as exc:
        skipped.append(f"tab-group sweep: {exc}")
    return AuditReport(scanned=len(elements), audited=audited, skipped=skipped, findings=findings)


_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}


def format_audit(report: AuditReport) -> str:
    if not report.findings:
        body = "no anomalies measured"
    else:
        ordered = sorted(report.findings, key=lambda f: _SEVERITY_ORDER[f.severity])
        lines = []
        for f in ordered:
            ref = f.evidence.get("ref", "")
            element = f.evidence.get("element", "")
            lines.append(f"[{f.severity}/{f.category}] {element} ref={ref} ({f.state}): {f.detail}")
        body = "\n".join(lines)
    header = f"audited {report.audited}/{report.scanned} interactive elements, {len(report.findings)} finding(s)"
    if report.skipped:
        header += f"\nskipped ({len(report.skipped)}): " + "; ".join(report.skipped[:10])
        if len(report.skipped) > 10:
            header += f"; +{len(report.skipped) - 10} more"
    return f"{header}\n{body}"
