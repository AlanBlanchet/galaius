import asyncio
import base64
import logging

from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from interact import desktop
from interact.atspi import AtSpi
from interact.actions import (
    AnyAction,
    AnnotateAction,
    BROWSER_ONLY_ACTIONS,
    ClickAction,
    ClickElementAction,
    CloseTabAction,
    CompareAction,
    DragAction,
    HoverAction,
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
from interact.state import DesktopState, PageState, StateChange

_log = logging.getLogger("interact")


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
_TARGETING_TYPES = frozenset({"click", "hover", "type_text", "drag"})


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
        first = str(e).splitlines()[0]  # trim Playwright's multi-line call-log dump
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
    locator = (
        page.get_by_role(action.role, name=action.name)
        if action.role
        else page.get_by_text(action.name, exact=False)
    )
    target = f"name={action.name!r}" + (f" role={action.role!r}" if action.role else "")
    count = await locator.count()
    if count == 0:
        raise ValueError(
            f"No element matches {target}. Check the name/role, or use a `ref` from "
            "get_interactive_elements."
        )
    if count > 1:
        raise ValueError(
            f"{count} elements match {target} — ambiguous. Use a `ref` from "
            "get_interactive_elements, or a more specific `name`/`selector`."
        )
    return locator


def _step(i: int, action_type: str, msg: str) -> str:
    return f"Step {i + 1} ({action_type}): {msg}"


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
            if action.name or action.ref or action.selector:
                x, y, _, err = _resolve_action_coords(action, wid, win)
                if err:
                    step_reports.append(_step(i, action.type, f"SKIPPED: {err}"))
                    continue
                await win.click(x, y)
            before_state = DesktopState.capture(win.name)
            if action.clear_first:
                await win.press_key("ctrl+a")
                await win.press_key("Delete")
            await win.type_text(action.text)
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
            screenshot_bytes, report = await _capture_desktop(win, action.query)
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
        _session_response,
        _wait as _wait_fn,
    )

    current_tab = 0
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
            current_tab = action.index
            page = await mgr.get_page(current_tab)
            step_reports.append(
                _step(i, action.type, f"switched to tab {action.index}")
            )

        elif isinstance(action, CloseTabAction):
            idx = action.index if action.index is not None else mgr.tab_count - 1
            await mgr.close_tab(idx)
            step_reports.append(_step(i, action.type, f"closed tab {idx}"))
            if idx == current_tab:
                current_tab = max(0, current_tab - 1)
                page = await mgr.get_page(current_tab)
            elif idx < current_tab:
                current_tab -= 1

        elif isinstance(action, AnnotateAction):
            report = await _annotate_and_describe(
                mgr, current_tab, action.scope, action.query, action.limit
            )
            step_reports.append(_step(i, action.type, report))
            final = await _capture(mgr, scope=action.scope, tab=current_tab)
            snapshots[step_idx] = base64.b64decode(final.screenshot_base64)

        elif isinstance(action, ClickElementAction):
            el = mgr.get_element(action.element, current_tab)
            if el is None:
                step_reports.append(
                    _step(
                        i,
                        action.type,
                        f"Element {action.element} not found — run annotate first",
                    )
                )
            else:
                before = await _capture(mgr, tab=current_tab)
                if el.ref:
                    await page.locator(el.playwright_ref).click()
                else:
                    await page.mouse.click(el.center_x, el.center_y)
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
            else:
                el = mgr.get_element(action.element, current_tab)
                if el is None:
                    step_reports.append(
                        _step(
                            i,
                            action.type,
                            f"Element {action.element} not found — run annotate first",
                        )
                    )
                    continue
                if el.ref:
                    await page.locator(el.playwright_ref).click()
                else:
                    await page.mouse.click(el.center_x, el.center_y)
            if action.wait:
                await _wait_fn(page, action.wait)
            final = await _capture(mgr, tab=current_tab)
            change = StateChange.compute(before, final)
            step_reports.append(_step(i, action.type, change.description))

        elif isinstance(action, HoverAction) and action.name:
            locator = await _named_locator(page, action)
            await locator.hover()
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
                    mgr, current_tab, action.selector, action.element, action.query
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
                if action.query:
                    report = await _analyze(state, action.query)
                else:
                    report = f"{state.title} — {state.visible_text[:300]}"
                final = state
            step_reports.append(_step(i, action.type, report))

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

    if final is None:
        final = await _capture(mgr, scope, current_tab)
    elif wait:
        await _wait_fn(page, wait)
        final = await _capture(mgr, scope, current_tab)
    elif scope:
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
