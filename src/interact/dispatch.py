import asyncio
import base64
import json
import logging
import re

from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from interact import desktop
from interact.desktop.atspi import AtSpi
from interact.actions import (
    AnyAction,
    AnnotateAction,
    BROWSER_ONLY_ACTIONS,
    ClickAction,
    ClickElementAction,
    CloseTabAction,
    CompareAction,
    DragAction,
    EmulateDeviceAction,
    EvaluateJsAction,
    HoverAction,
    settle_animations,
    HttpRequestAction,
    KeyPressAction,
    NewTabAction,
    ScreenshotAction,
    ScrollAction,
    SleepAction,
    SwitchTabAction,
    TypeTextAction,
)
from interact.browser import BrowserManager
from interact.debug_utils import Debug
from interact.desktop import DesktopWindow
from interact.detect import _desktop_context
from interact.state import DesktopState, PageState, StateChange, ref_locator

_log = logging.getLogger("interact")

# Typing into a toolkit field after the focusing click: Flutter (GTK) under XTEST doesn't have its
# text-input connection wired up the instant a field focuses, and keystrokes sent into that window
# are silently dropped until it is — non-deterministically, worse under software GL / debug builds.
# The field shows its focus ring (the click landed) yet stays empty (#59). So: settle after the
# focusing click, then verify the keystrokes registered (a band-scoped pixel diff at the focus
# point) and re-type if they didn't.
_TYPE_FOCUS_SETTLE = 0.5   # seconds to let the toolkit wire up text input before the first keys
_TYPE_RENDER = 0.6         # seconds to let the field repaint before judging whether text appeared
_TYPE_RETRIES = 2          # extra type attempts when the keystrokes didn't register
_TYPE_BAND = (230, 30)     # half-(width, height) of the field band the diff inspects, in px
_TYPE_CHANGE_FRAC = 0.012  # min changed fraction of that band that counts as "text appeared"


def _field_changed(before: bytes, after: bytes, cx: int, cy: int) -> bool:
    """Did the field band around the focus point (cx, cy) gain glyphs between two window captures?
    Scoping the diff to the field makes even short typed text a large fraction of the band while a
    caret blink stays tiny — so this reliably tells "text landed" from "nothing happened" without
    ever mistaking a caret for input (which would double-type). Any error → True (assume it
    registered, so the retry loop can't spin forever)."""
    try:
        import io  # noqa: PLC0415

        from PIL import Image, ImageChops  # noqa: PLC0415

        a = Image.open(io.BytesIO(before)).convert("L")
        b = Image.open(io.BytesIO(after)).convert("L")
        if a.size != b.size:
            return True
        hw, hh = _TYPE_BAND
        box = (max(0, cx - hw), max(0, cy - hh), min(a.size[0], cx + hw), min(a.size[1], cy + hh))
        a, b = a.crop(box), b.crop(box)
        diff = ImageChops.difference(a, b)
        changed = sum(c for v, c in enumerate(diff.histogram()) if v > 20)
        return changed > a.size[0] * a.size[1] * _TYPE_CHANGE_FRAC
    except Exception:
        return True


async def _type_desktop(win: "DesktopWindow", text: str, fx: int | None, fy: int | None) -> None:
    """Type ``text`` into a desktop field, re-typing if the keystrokes were dropped during the
    toolkit's post-focus input-connection setup (#59). Only the sandbox/desktop backend path with a
    known focus point (fx, fy) is verified-and-retried — the diff needs both. Safe against
    double-typing: a real type changes the field band far past the threshold, so a landed type is
    detected on the first check and never re-sent; before each retry the field is cleared so a
    partial never accumulates."""
    backend = win._backend
    if not text.strip() or fx is None or fy is None or backend is None:
        await win.type_text(text)
        return
    before = win.capture()
    await win.type_text(text)
    for _ in range(_TYPE_RETRIES):
        await asyncio.sleep(_TYPE_RENDER)
        if _field_changed(before, win.capture(), fx, fy):
            return
        await win.press_key("ctrl+a")
        await win.press_key("Delete")
        await asyncio.sleep(_TYPE_FOCUS_SETTLE)
        await win.type_text(text)


def _element_at(wid: int, x: int, y: int):
    """The smallest already-detected element whose box contains (x, y), or None. Smallest-area
    wins so a click inside a button-within-a-panel snaps to the button, not the panel."""
    hits = [
        el
        for el in (desktop.DesktopElement.cached(wid) or [])
        if el.x <= x <= el.x + el.w and el.y <= y <= el.y + el.h
    ]
    return min(hits, key=lambda el: el.w * el.h) if hits else None


def _resolve_action_coords(action, wid: int, win: DesktopWindow):
    from interact.server import _name_not_found_msg, _not_found, _resolve_desktop_el  # noqa: PLC0415 — circular: server imports dispatch

    x = getattr(action, "x", None)
    y = getattr(action, "y", None)
    if x is not None and y is not None:
        # Snap a raw x,y to an already-detected element whose box contains the point: the
        # action then resolves by ref (stable across re-renders) and the report names the
        # element instead of "raw coordinates". Only when a detection exists — no detection,
        # no snap (and _xy_report nudges the agent to detect first).
        el = _element_at(wid, x, y)
        if el:
            return el.center_x, el.center_y, el, None
        return x, y, None, None
    name = getattr(action, "name", None)
    if name:
        role = getattr(action, "role", None)
        el = AtSpi.find_element(win.name, name=name, role=role)
        if not el:
            return 0, 0, None, _name_not_found_msg(win.name, name)
        return el.center_x, el.center_y, el, None
    element = getattr(action, "element", None)
    if element is not None:
        el = _resolve_desktop_el(wid, win.name, element=element)
        if not el:
            return 0, 0, None, _not_found(f"Element {element}")
        return el.center_x, el.center_y, el, None
    ref = getattr(action, "ref", None)
    if ref:
        el = _resolve_desktop_el(wid, win.name, ref=ref)
        if not el:
            return 0, 0, None, _not_found(f"Element ref={ref!r}")
        return el.center_x, el.center_y, el, None
    selector = getattr(action, "selector", None)
    if selector:
        el = _resolve_desktop_el(wid, win.name, selector=selector)
        if not el:
            return 0, 0, None, f"No desktop element matching '{selector}'"
        return el.center_x, el.center_y, el, None
    return 0, 0, None, "Provide x,y, name, ref, selector, or element for desktop action"


# Actions that target a DOM element — the ones where "use a stable ref instead" is the right
# advice on a timeout / ambiguous-selector failure. navigate/evaluate_js/etc. are NOT here:
# a ref means nothing for them, so their errors pass through with only the dump trimmed.
_TARGETING_TYPES = frozenset({"click", "hover", "type_text", "drag", "double_click", "select_text"})


def _selector_of(action):
    """The selector an action targeted, if any — for a precise timeout message."""
    return getattr(action, "selector", None)


async def _execute_browser_action(action, page):
    """Run a browser action, converting Playwright's opaque 30s "Timeout exceeded" / strict-mode
    dumps into a short message. For element-targeting actions a dead/ambiguous selector is the
    single most common run_actions failure and the recovery is almost always a stable `ref`, so
    we say so; other actions get the same trim without the (irrelevant) ref nudge."""
    targets_element = action.type in _TARGETING_TYPES or bool(_selector_of(action))
    try:
        return await action.execute(page)
    except PlaywrightTimeout:
        sel = _selector_of(action)
        if not targets_element:
            raise ValueError(
                f"{action.type} timed out after the configured wait — check the page state."
            ) from None
        where = f" for selector {sel!r}" if sel else ""
        raise ValueError(
            f"{action.type} timed out{where}: the target never became actionable. "
            "get_interactive_elements / get_page_state return the page's current elements as refs."
        ) from None
    except PlaywrightError as e:
        msg = str(e)
        first = msg.splitlines()[0]  # trim Playwright's multi-line call-log dump
        if "strict mode violation" in msg:
            # A selector (often :has-text) matched several nodes — duplicated link text
            # (breadcrumb mirrors sidebar) or a generic button label. Say so precisely instead of
            # dumping every match, and point at the unambiguous recoveries (#29).
            sel = _selector_of(action)
            target = f"selector {sel!r}" if sel else "the locator"
            raise ValueError(
                f"{action.type}: {target} matched multiple elements — narrow it (add :visible, a "
                "parent scope, or `>> nth=0`) or use a unique `ref` from get_interactive_elements."
            ) from None
        if not targets_element:
            raise ValueError(f"{action.type} failed: {first}") from None
        sel = _selector_of(action)
        where = f" (selector {sel!r})" if sel else ""
        raise ValueError(
            f"{action.type} failed{where}: {first}. "
            "get_interactive_elements lists the page's elements as refs."
        ) from None


async def _named_locator(page, action):
    """Resolve a name/role/text target to a *single* locator, or fail with an actionable,
    ref-nudging message. Playwright's ``get_by_role``/``get_by_text`` are strict: when the name
    matches many elements they raise an opaque "strict mode violation" dumping every match (the
    real run hit 17). interact pre-checks the count and instead tells the agent how to recover —
    the stable fix is a unique ``ref`` from ``get_interactive_elements``, which can't be
    ambiguous by construction (raw text/role can)."""
    def _by_name(exact: bool):
        return (
            page.get_by_role(action.role, name=action.name, exact=exact)
            if action.role
            else page.get_by_text(action.name, exact=exact)
        )

    locator = _by_name(exact=False)
    target = f"name={action.name!r}" + (f" role={action.role!r}" if action.role else "")
    count = await locator.count()
    if count == 0:
        raise ValueError(
            f"No element matches {target}. Check the name/role, or use a `ref` from "
            "get_interactive_elements."
        )
    if count == 1:
        return locator
    # Several substring matches. Agents name what they SEE, so resolve the common cases before
    # giving up: (1) exactly one EXACT-text match ('Connexion' vs 'Connexion aide'); (2) exactly
    # one VISIBLE match (the same label hidden in a closed menu/template elsewhere).
    exact = _by_name(exact=True)
    if await exact.count() == 1:
        return exact
    visible_idx = [i for i in range(count) if await locator.nth(i).is_visible()]
    if len(visible_idx) == 1:
        return locator.nth(visible_idx[0])
    # Genuinely ambiguous — describe the matches so the agent can refine without a scan round-trip.
    lines = []
    for i in range(min(count, 5)):
        nth = locator.nth(i)
        try:
            tag = await nth.evaluate("e => e.tagName.toLowerCase()")
            text = re.sub(r"\s+", " ", (await nth.inner_text(timeout=500)).strip())[:50]
            shown = "" if i in visible_idx else " (hidden)"
            lines.append(f"  [{i}] <{tag}> {text!r}{shown}")
        except Exception:
            lines.append(f"  [{i}] (could not inspect)")
    more = f"\n  … and {count - 5} more" if count > 5 else ""
    raise ValueError(
        f"{count} elements match {target} — ambiguous. Matches:\n" + "\n".join(lines) + more +
        "\nUse a `ref` from get_interactive_elements, or a more specific `name`/`selector`."
    )


async def _click_element(page, mgr, element: int, tab: int) -> bool:
    """Click the numbered ``element`` on ``page``. Prefers the stored element map (its ref →
    locator, or center coordinates); if the map has no entry — it was cleared, or the ref came
    from a scan in a separate call — falls back to the live ``data-interact-ref="e{N}"`` attribute,
    which persists on the DOM across tool calls until the next scan. So a ref from an earlier
    get_interactive_elements still clicks even when the server-side map is stale (#34). Returns
    False only when the element resolves by neither route (a genuinely stale ref)."""
    el = mgr.get_element(element, tab)
    if el is not None:
        if el.ref:
            await page.locator(el.playwright_ref).click()
        else:
            await page.mouse.click(el.center_x, el.center_y)
        return True
    locator = page.locator(ref_locator(f"e{element}"))
    if await locator.count() == 1:  # the badge is still on the live DOM — resolve it directly
        await locator.click()
        return True
    return False


def _element_miss(element: int) -> str:
    return (
        f"Element {element} not found on the active tab — re-run get_interactive_elements "
        "(or switch_tab first if it is on another tab)."
    )


def _step(i: int, action_type: str, msg: str) -> str:
    return f"Step {i + 1} ({action_type}): {msg}"


_JS_RESULT_CAP = 4000  # the return value is the point of evaluate_js, so cap generously


def _render_js_result(value) -> str:
    """The evaluate_js return value, JSON-serialised (devtools-style) so dicts / lists / numbers /
    strings all read back unambiguously — this IS the step's output. undefined/null → an explicit
    nudge, since the usual cause is a script that computed a value without ``return``ing it."""
    if value is None:
        return (
            "→ returned undefined/null. To read a value back, `return` it "
            "(e.g. `return document.title`) or use an arrow that returns one."
        )
    try:
        rendered = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) > _JS_RESULT_CAP:
        rendered = rendered[:_JS_RESULT_CAP] + f"… (+{len(rendered) - _JS_RESULT_CAP} chars)"
    return rendered


def _fmt_cursor() -> str:
    ct = desktop.Cursor.current_type()
    return f"{ct} ({desktop.Cursor.label(ct)})"


def _el_report(verb: str, el) -> str:
    # Reference elements by ref/index + role/name — never pixel coordinates. The agent
    # acts via refs; resolved coords are a dispatch implementation detail it must not see.
    return f"{verb} [{el.index}] {el.role}: {el.name!r} cursor={_fmt_cursor()}"


def _xy_report(verb: str, x: int, y: int) -> str:
    # Raw-coordinate action — report it factually. No prescriptive nudge: the agent can act by
    # coordinates if it wants; refs are available from get_interactive_elements if it prefers them.
    return f"{verb} at coordinates cursor={_fmt_cursor()}"


def _report_with_change(win_name: str, before: DesktopState, report: str) -> str:
    after = DesktopState.capture(win_name)
    change = DesktopState.compute_change(before, after)
    if change:
        report += f"\n  \u2192 {change}"
    return report


async def _run_actions_desktop(
    win: DesktopWindow,
    actions: list[AnyAction],
    query: str | None,
    invocation_id: str | None = None,
    record_frames: list[bytes] | None = None,
) -> str:
    from interact.server import (  # noqa: PLC0415 — circular: server imports dispatch
        _annotate_desktop,
        _capture_desktop,
        _desktop_label,
        _resolve_desktop_el,
        _run_compare,
        _run_observe,
    )

    wid = win.wid
    label = _desktop_label(win)
    step_reports: list[str] = []
    snapshots: dict[int, bytes] = {}

    for i, action in enumerate(actions):
        step_idx = i + 1
        _log.info("desktop action %d: %s", step_idx, action.type)

        if isinstance(action, CompareAction):
            result = await _run_compare(
                snapshots, action.steps, action.query, _desktop_context(win)
            )
            step_reports.append(_step(i, action.type, result))
            continue

        if action.type in BROWSER_ONLY_ACTIONS:
            step_reports.append(
                _step(
                    i,
                    action.type,
                    f"Action '{action.type}' is browser-only — use a session instead of window",
                )
            )
            continue

        if isinstance(action, SleepAction):
            await asyncio.sleep(action.duration)
            step_reports.append(_step(i, action.type, f"waited {action.duration}s"))

        elif isinstance(action, (ClickAction, ClickElementAction)):
            x, y, el, err = _resolve_action_coords(action, wid, win)
            if err:
                step_reports.append(_step(i, action.type, f"SKIPPED: {err}"))
                continue
            before_state = DesktopState.capture(win.name)
            await win.click(x, y)
            await asyncio.sleep(0.05)
            report = _el_report("clicked", el) if el else _xy_report("clicked", x, y)
            report = _report_with_change(win.name, before_state, report)
            step_reports.append(_step(i, action.type, report))

        elif isinstance(action, HoverAction):
            x, y, el, err = _resolve_action_coords(action, wid, win)
            if err:
                step_reports.append(_step(i, action.type, f"SKIPPED: {err}"))
                continue
            await win.hover(x, y)
            await asyncio.sleep(0.05)
            report = _el_report("hovered", el) if el else _xy_report("hovered", x, y)
            step_reports.append(_step(i, action.type, report))

        elif isinstance(action, TypeTextAction):
            fx = fy = None
            if action.name or action.ref or action.selector:
                x, y, _, err = _resolve_action_coords(action, wid, win)
                if err:
                    step_reports.append(_step(i, action.type, f"SKIPPED: {err}"))
                    continue
                await win.click(x, y)
                await asyncio.sleep(_TYPE_FOCUS_SETTLE)  # let the toolkit wire up text input (#59)
                fx, fy = x, y
            before_state = DesktopState.capture(win.name)
            if action.clear_first:
                await win.press_key("ctrl+a")
                await win.press_key("Delete")
            await _type_desktop(win, action.text, fx, fy)
            report = f"typed {len(action.text)} chars"
            report = _report_with_change(win.name, before_state, report)
            step_reports.append(_step(i, action.type, report))

        elif isinstance(action, KeyPressAction):
            before_state = DesktopState.capture(win.name)
            await win.press_key(action.key)
            report = _report_with_change(
                win.name, before_state, f"pressed {action.key}"
            )
            step_reports.append(_step(i, action.type, report))

        elif isinstance(action, ScrollAction):
            before_state = DesktopState.capture(win.name)
            await win.scroll(win.w // 2, win.h // 2, action.direction, action.amount)
            report = _report_with_change(
                win.name, before_state, f"scrolled {action.direction} x{action.amount}"
            )
            step_reports.append(_step(i, action.type, report))

        elif isinstance(action, DragAction):
            fx, fy = action.from_x, action.from_y
            tx, ty = action.to_x, action.to_y
            if action.from_ref:
                el = _resolve_desktop_el(wid, win.name, ref=action.from_ref)
                if el is None:
                    step_reports.append(
                        _step(
                            i,
                            action.type,
                            f"SKIPPED: from_ref element {action.from_ref!r} not found",
                        )
                    )
                    continue
                fx, fy = el.center_x, el.center_y
            if action.to_ref:
                el = _resolve_desktop_el(wid, win.name, ref=action.to_ref)
                if el is None:
                    step_reports.append(
                        _step(
                            i,
                            action.type,
                            f"SKIPPED: to_ref element {action.to_ref!r} not found",
                        )
                    )
                    continue
                tx, ty = el.center_x, el.center_y
            before_state = DesktopState.capture(win.name)
            await win.drag(fx, fy, tx, ty, action.steps)
            report = _report_with_change(
                win.name, before_state, f"dragged ({fx},{fy})->({tx},{ty})"
            )
            step_reports.append(_step(i, action.type, report))

        elif isinstance(action, ScreenshotAction):
            screenshot_bytes, report = await _capture_desktop(win, action.query, action.path)
            snapshots[step_idx] = screenshot_bytes
            Debug.step_save(
                invocation_id,
                i,
                action.type,
                "screenshot",
                screenshot_bytes,
                ext="png",
            )
            step_reports.append(_step(i, action.type, report))

        elif isinstance(action, AnnotateAction):
            _, report = await _annotate_desktop(
                win, action.query, invocation_id=invocation_id
            )
            snapshots[step_idx] = win.capture()
            step_reports.append(_step(i, action.type, report))

        elif isinstance(action, HttpRequestAction):
            result = await action.execute(None)
            step_reports.append(_step(i, action.type, str(result)))

        else:
            step_reports.append(
                _step(
                    i, action.type, f"Action '{action.type}' not supported on desktop"
                )
            )

        await asyncio.sleep(0.1)

        if step_reports:
            Debug.step_save(invocation_id, i, action.type, "report", step_reports[-1])

        if record_frames is not None:  # one frame per step → captures every action's result
            record_frames.append(win.capture())

        if action.observe:
            obs_bytes = win.capture()
            snapshots[step_idx] = obs_bytes
            Debug.step_save(
                invocation_id,
                i,
                action.type,
                "observe",
                obs_bytes,
                ext="png",
            )
            obs_result = await _run_observe(
                obs_bytes, action.observe, _desktop_context(win)
            )
            step_reports[-1] += f"\n  observation: {obs_result}"

    if query:
        _, final_summary = await _capture_desktop(win, query)
    else:
        final_summary = f"{win.name} ({win.w}x{win.h})"

    report = (
        f"{label}\n"
        + "\n".join(step_reports)
        + f"\n\n---\nFinal state: {final_summary}"
    )

    Debug.save("desktop_final", report, invocation_id=invocation_id)
    return report


async def _run_actions_browser(
    mgr: BrowserManager,
    actions: list[AnyAction],
    query: str | None,
    scope: str | None,
    wait: str | None,
    session: str,
    invocation_id: str | None = None,
    record_frames: list[bytes] | None = None,
) -> str:
    from interact.server import (  # noqa: PLC0415 — circular: server imports dispatch
        _annotate_and_describe,
        _analyze,
        _capture,
        _element_screenshot,
        _run_compare,
        _run_observe,
        _save_to_path,
        _session_response,
        _wait as _wait_fn,
    )

    current_tab = mgr.active_tab  # the tab the prior tab-less scan acted on, so its refs resolve (#34)
    page = await mgr.get_page(current_tab)
    step_reports: list[str] = []
    final: PageState | None = None
    snapshots: dict[int, bytes] = {}

    for i, action in enumerate(actions):
        step_idx = i + 1
        _log.info("browser action %d: %s", step_idx, action.type)

        if isinstance(action, CompareAction):
            ctx = f"Browser session comparison of steps {action.steps}"
            result = await _run_compare(snapshots, action.steps, action.query, ctx)
            step_reports.append(_step(i, action.type, result))
            continue

        if isinstance(action, NewTabAction):
            idx = await mgr.new_tab(action.url)
            current_tab = idx
            page = await mgr.get_page(current_tab)
            step_reports.append(_step(i, action.type, f"opened tab {idx}"))

        elif isinstance(action, SwitchTabAction):
            page = await mgr.switch_tab(action.index)  # persists the active tab on the session (#30)
            current_tab = action.index
            step_reports.append(
                _step(i, action.type, f"switched to tab {action.index}")
            )

        elif isinstance(action, CloseTabAction):
            idx = action.index if action.index is not None else mgr.tab_count - 1
            await mgr.close_tab(idx)  # adjusts the session's active tab to stay valid
            current_tab = mgr.active_tab
            page = await mgr.get_page(current_tab)
            step_reports.append(_step(i, action.type, f"closed tab {idx}"))

        elif isinstance(action, AnnotateAction):
            report = await _annotate_and_describe(
                mgr, current_tab, action.scope, action.query, action.limit
            )
            step_reports.append(_step(i, action.type, report))
            final = await _capture(mgr, scope=action.scope, tab=current_tab)
            snapshots[step_idx] = base64.b64decode(final.screenshot_base64)

        elif isinstance(action, ClickElementAction):
            before = await _capture(mgr, tab=current_tab)
            if not await _click_element(page, mgr, action.element, current_tab):
                step_reports.append(_step(i, action.type, _element_miss(action.element)))
                continue
            if action.wait:
                await _wait_fn(page, action.wait)
            final = await _capture(mgr, tab=current_tab)
            change = StateChange.compute(before, final)
            step_reports.append(_step(i, action.type, change.description))

        elif isinstance(action, ClickAction) and (
            action.name or action.element is not None
        ):
            before = await _capture(mgr, tab=current_tab)
            if action.name:
                locator = await _named_locator(page, action)
                await locator.click()
            elif not await _click_element(page, mgr, action.element, current_tab):
                step_reports.append(_step(i, action.type, _element_miss(action.element)))
                continue
            if action.wait:
                await _wait_fn(page, action.wait)
            final = await _capture(mgr, tab=current_tab)
            change = StateChange.compute(before, final)
            step_reports.append(_step(i, action.type, change.description))

        elif isinstance(action, HoverAction) and action.name:
            locator = await _named_locator(page, action)
            await locator.hover()
            await settle_animations(page)  # final hovered state for the next capture (#49)
            step_reports.append(_step(i, action.type, "hovered"))

        elif isinstance(action, TypeTextAction) and action.name:
            locator = await _named_locator(page, action)
            await locator.click()
            before = await _capture(mgr, tab=current_tab)
            if action.clear_first:
                await locator.fill(action.text)
            else:
                await locator.type(action.text)
            if action.wait:
                await _wait_fn(page, action.wait)
            final = await _capture(mgr, tab=current_tab)
            change = StateChange.compute(before, final)
            step_reports.append(_step(i, action.type, change.description))

        elif isinstance(action, ScreenshotAction):
            if action.element is not None or action.selector is not None:
                report = await _element_screenshot(
                    mgr, current_tab, action.selector, action.element, action.query, action.path
                )
            else:
                state = await _capture(mgr, action.scope, current_tab)
                snapshots[step_idx] = base64.b64decode(state.screenshot_base64)
                Debug.step_save(
                    invocation_id,
                    i,
                    action.type,
                    "screenshot",
                    snapshots[step_idx],
                    ext="png",
                )
                if action.path:  # honour an inline screenshot's path, like the standalone tool (#27)
                    _save_to_path(action.path, snapshots[step_idx])
                if action.query:
                    report = await _analyze(state, action.query)
                else:
                    report = f"{state.title} — {state.visible_text[:300]}"
                if action.path:
                    report += f"  (saved {action.path})"
                final = state
            step_reports.append(_step(i, action.type, report))

        elif isinstance(action, EvaluateJsAction):
            # The return value IS the output — surface it JSON-serialised as the step's primary
            # text (never bury it under a change description). Any DOM mutation the script caused
            # shows in the final state / the next observation.
            result = await _execute_browser_action(action, page)
            if action.wait:
                await _wait_fn(page, action.wait)
            step_reports.append(_step(i, action.type, _render_js_result(result)))

        elif isinstance(action, EmulateDeviceAction):
            try:
                desc = await mgr.emulate_device(
                    device=action.device,
                    width=action.width,
                    height=action.height,
                    device_scale_factor=action.device_scale_factor,
                    is_mobile=action.is_mobile,
                    has_touch=action.has_touch,
                    user_agent=action.user_agent,
                    reset=action.reset,
                )
                current_tab = 0
                page = await mgr.get_page(current_tab)  # context rebuilt → refresh the handle
            except ValueError as e:
                desc = f"SKIPPED: {e}"
            step_reports.append(_step(i, action.type, desc))

        elif not action.mutates:
            result = await _execute_browser_action(action, page)
            step_reports.append(_step(i, action.type, str(result)))

        else:
            before = await _capture(mgr, tab=current_tab)
            result = await _execute_browser_action(action, page)
            if action.wait:
                await _wait_fn(page, action.wait)
            final = await _capture(mgr, tab=current_tab)
            change = StateChange.compute(before, final)
            entry = _step(i, action.type, change.description)
            if result is not None:
                entry += f"\n  result: {result}"
            step_reports.append(entry)

        if step_reports:
            Debug.step_save(invocation_id, i, action.type, "report", step_reports[-1])

        if record_frames is not None:  # one frame per step → captures every action's result
            record_frames.append(await page.screenshot(type="png"))

        if action.observe:
            obs_bytes = await page.screenshot(type="png")
            snapshots[step_idx] = obs_bytes
            Debug.step_save(
                invocation_id,
                i,
                action.type,
                "observe",
                obs_bytes,
                ext="png",
            )
            obs_result = await _run_observe(
                obs_bytes, action.observe, f"Browser step {step_idx}"
            )
            step_reports[-1] += f"\n  observation: {obs_result}"

    if wait:
        await _wait_fn(page, wait)
    # Always recapture at the END of the batch: a `final` kept from a mid-batch action's
    # before/after diff misses everything later actions changed (a login redirect, an evaluate_js
    # mutation, a render settling) — the "Final state" summary lagged reality (#65).
    final = await _capture(mgr, scope, current_tab)
    if query:
        final_summary = await _analyze(final, query)
    else:
        final_summary = f"{final.title} — {final.url}\n{final.visible_text[:500]}"

    result = _session_response(
        session, "\n".join(step_reports) + f"\n\n---\nFinal state: {final_summary}"
    )
    Debug.save("browser_final", result, invocation_id=invocation_id)
    return result
