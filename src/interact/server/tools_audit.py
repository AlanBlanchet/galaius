"""audit_ui — the deterministic component state-machine MCP surface. Thin: resolves the target and
delegates to interact.ui_audit, which owns the actual instrumentation."""

from interact.debug_utils import Debug
from interact.server import targets
from interact.server.core import _AUTO_SESSION, _session_response, instrumented, mcp
from interact.ui_audit import audit_page, format_audit


@mcp.tool(category="vision")
@instrumented
async def audit_ui(
    scope: str | None = None,
    limit: int = 20,
    tab: int | None = None,
    target: str | None = None,
    session: str = _AUTO_SESSION,
) -> str:
    """Drive every interactive element through its own states and report MEASURED anomalies — no
    vision model, no opinion. A still screenshot cannot show a menu that closed the instant the
    pointer moved toward it, a tray covering its own trigger, focus that vanished, or a page that
    shifted when something opened; this catches exactly that class by actually hovering, focusing,
    pressing, opening and closing each control and comparing real geometry / hit-testing / focus /
    accessible-name / text-fit / settle-time before and after.

    Defect classes it measures, each with the geometry/DOM evidence that proves it:
    - unrequested_movement: an ancestor or sibling's box, scroll extent, or the page's own
      scrollWidth/Height changed on hover/focus/close when nothing asked for it.
    - outside_viewport / clipped_by_ancestor / covers_trigger: where an opened surface (menu,
      listbox, dialog, tooltip, popover — by ARIA role, not this app's CSS names) actually renders.
    - hit_test_mismatch: elementFromPoint at the control's own centre does not resolve to it — the
      exact "every hover dead after a route change" shape (hit-testing disagreeing with geometry).
    - focus_not_landed / focus_not_returned / escape_does_not_close / keyboard_unreachable: the
      full open/close focus lifecycle, and whether every control has a keyboard path at all.
    - name_leak: the accessible name is long, sentence-shaped, and shares little with the text the
      control itself paints — the measurable shape of a name pulled from a hint bubble or a hidden
      native control behind the one the user actually operates.
    - text_overflow / text_wrap_degenerate: content wider than its box, or wrapped to near one
      character per line (measured via the browser's own line-fragment rects, not a guess).
    - never_settles: animation or layout still running well past the bounded settle wait.

    Scope: BROWSER sessions only (errors on a desktop `target` for now — no AT-SPI state walker
    yet). Checks the page AT ITS CURRENT viewport/locale; re-run after `emulate_device` (narrower
    width) or a locale switch via run_actions to cover those — this tool composes with the existing
    primitives rather than reimplementing viewport/locale control.

    scope: CSS selector to audit one component instead of the whole page.
    limit: max interactive elements to drive through the full state walk (cost bound on a busy
        page); elements beyond it are named in the reply, never silently dropped.
    tab: which browser tab (default: the session's active tab).
    """
    inv = Debug.inv()
    Debug.dump_input(inv, {"tool": "audit_ui", "scope": scope, "limit": limit, "tab": tab,
                           "target": target, "session": session})
    win, mgr, err = targets._resolve_target(target, session)
    if err:
        return err
    if win:
        return (
            "ERROR: audit_ui is browser-only for now — target a web page (omit `target`, or "
            "target=\"browser\"). Desktop windows have real geometry via DesktopElement but no "
            "state-machine walker yet."
        )
    report = await audit_page(mgr, tab, scope, limit)
    return _session_response(session, format_audit(report))
