import asyncio
from contextlib import asynccontextmanager
import base64
import inspect
import json
import logging
import re
from typing import Callable, NamedTuple

from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from interact import desktop
from interact.desktop.atspi import AtSpi
from interact.actions.models import (
    AnyAction,
    AnnotateAction,
    BROWSER_ONLY_ACTIONS,
    DESKTOP_ONLY_ACTIONS,
    ClickAction,
    ClickElementAction,
    CloseTabAction,
    CompareAction,
    EmulateDeviceAction,
    EvaluateJsAction,
    HandleDialogAction,
    HoverAction,
    settle_animations,
    NewTabAction,
    ScreenshotAction,
    SwitchTabAction,
    TypeTextAction,
    WaitForAction,
)
from interact.browser import BrowserManager
from interact.debug_utils import Debug
from interact.desktop import DesktopWindow
from interact.vision.detect import _desktop_context
from interact.state import DesktopState, PageState, StateChange, ref_locator

_log = logging.getLogger("interact")


async def _finalize_step(
    action, i, step_idx, invocation_id, step_reports, record_frames, snapshots,
    *, capture_fn, context,
):
    """The per-step tail shared by the desktop and browser runners: save the step report, capture a
    record frame if recording, and run an `observe` VLM query if the action asked for one. Only the
    per-surface capture (`win.capture()` sync vs `page.screenshot()` async) and the observe context
    string differ, so both are injected; ``capture_fn`` may return bytes or an awaitable."""
    from interact.server import _run_observe  # noqa: PLC0415 — circular: server imports dispatch

    async def _grab() -> bytes:
        frame = capture_fn()
        return await frame if inspect.isawaitable(frame) else frame

    if step_reports:
        Debug.step_save(invocation_id, i, action.type, "report", step_reports[-1])
    if record_frames is not None:  # one frame per step → captures every action's result
        record_frames.append(await _grab())
    if action.observe:
        obs_bytes = await _grab()
        snapshots[step_idx] = obs_bytes
        Debug.step_save(invocation_id, i, action.type, "observe", obs_bytes, ext="png")
        obs_result = await _run_observe(obs_bytes, action.observe, context)
        step_reports[-1] += f"\n  observation: {obs_result}"

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


async def _type_desktop(
    win: "DesktopWindow", text: str, fx: int | None, fy: int | None
) -> str | None:
    """Type ``text`` into a desktop field, re-typing if the keystrokes were dropped during the
    toolkit's post-focus input-connection setup (#59). Only the sandbox/desktop backend path with a
    known focus point (fx, fy) is verified-and-retried — the diff needs both. Safe against
    double-typing: a real type changes the field band far past the threshold, so a landed type is
    detected on the first check and never re-sent; before each retry the field is cleared so a
    partial never accumulates.

    Returns a warning when every attempt was verifiably dropped, else None. Reporting the verdict
    is the point: the retries already knew the text never appeared, but the step still read "typed
    N chars", so a caller drove two Flutter TextFields with three different methods, believed each
    had worked, and only found out by screenshotting after each one (#93). A verified failure is
    worth far more than a confident success."""
    backend = win._backend
    if not text.strip() or fx is None or fy is None or backend is None:
        await win.type_text(text)
        return None  # unverifiable (no focus point / no backend) — nothing to claim either way
    before = win.capture()
    await win.type_text(text)
    for _ in range(_TYPE_RETRIES):
        await asyncio.sleep(_TYPE_RENDER)
        if _field_changed(before, win.capture(), fx, fy):
            return None
        await win.press_key("ctrl+a")
        await win.press_key("Delete")
        await asyncio.sleep(_TYPE_FOCUS_SETTLE)
        await win.type_text(text)
    await asyncio.sleep(_TYPE_RENDER)
    if _field_changed(before, win.capture(), fx, fy):
        return None
    return (
        f"WARNING: the text did not appear in the field after {_TYPE_RETRIES + 1} attempts — the "
        f"keystrokes were sent but the widget never rendered them, so treat this step as FAILED. "
        f"Some toolkits (Flutter/GTK) only accept text input while their toplevel is ACTIVE, which "
        f"a WM-less sandbox cannot always signal. Try: click the field first if you typed without a "
        f"target, then key_press single characters to confirm delivery; if nothing lands, drive the "
        f"value another way (the app's own API/deep-link) rather than trusting this step."
    )


def _element_at(wid: int, x: int, y: int):
    """The smallest already-detected element whose box contains (x, y), or None. Smallest-area
    wins so a click inside a button-within-a-panel snaps to the button, not the panel."""
    hits = [
        el
        for el in (desktop.DesktopElement.cached(wid) or [])
        if el.x <= x <= el.x + el.w and el.y <= y <= el.y + el.h
    ]
    return min(hits, key=lambda el: el.w * el.h) if hits else None


class _Resolved(NamedTuple):
    """What a desktop action resolved to: where to act, the element it named (None for a literal
    coordinate action), a fatal error, and a `note` appended to the step report — the hedged
    cached-detection annotation or the stale-detection warning (#81, #88)."""

    x: int
    y: int
    el: object | None
    err: str | None
    note: str = ""


def _coord_note(wid: int, win: DesktopWindow, x: int, y: int) -> str:
    """The hedged annotation for a LITERAL coordinate action. What is believed to sit at (x,y) is
    genuinely useful context — but only as an annotation, never as a relabelling, and never at all
    when the detection predates a layout change (a stale guess is worse than none, #88)."""
    stale = desktop.DesktopElement.detection_stale(wid, win)
    if stale:
        return f" (WARNING: {stale} — cached refs may name the wrong widget)"
    el = _element_at(wid, x, y)
    return f" (cached detection says: {el.role} {el.name!r})" if el else ""


def _element_note(wid: int, win: DesktopWindow) -> str:
    """#88 item 1: an action resolved BY REF/element/selector against a detection taken under a
    different window geometry is clicking a box that may now hold a different widget. Say so in
    the step report instead of silently acting on it."""
    stale = desktop.DesktopElement.detection_stale(wid, win)
    return f" (WARNING: {stale} — re-run get_interactive_elements)" if stale else ""


def _resolve_action_coords(action, wid: int, win: DesktopWindow) -> _Resolved:
    from interact.server import _name_not_found_msg, _not_found, _resolve_desktop_el  # noqa: PLC0415 — circular: server imports dispatch

    x = getattr(action, "x", None)
    y = getattr(action, "y", None)
    if x is not None and y is not None:
        # A raw x,y is LITERAL — the caller asked for THAT pixel. This used to SNAP the point onto
        # any cached element whose box contained it, replacing the coordinates with that element's
        # centre and reporting the step as that element; once the layout had changed under a stale
        # cache the click landed elsewhere and was labelled with an unrelated widget (#81, #88).
        return _Resolved(x, y, None, None, _coord_note(wid, win, x, y))
    name = getattr(action, "name", None)
    if name:
        role = getattr(action, "role", None)
        el = AtSpi.find_element(win.name, name=name, role=role)
        if not el:
            return _Resolved(0, 0, None, _name_not_found_msg(win.name, name))
        return _Resolved(el.center_x, el.center_y, el, None)  # live AT-SPI lookup: never stale
    note = _element_note(wid, win)
    element = getattr(action, "element", None)
    if element is not None:
        el = _resolve_desktop_el(wid, win.name, element=element)
        if not el:
            return _Resolved(0, 0, None, _not_found(f"Element {element}"))
        return _Resolved(el.center_x, el.center_y, el, None, note)
    ref = getattr(action, "ref", None)
    if ref:
        el = _resolve_desktop_el(wid, win.name, ref=ref)
        if not el:
            return _Resolved(0, 0, None, _not_found(f"Element ref={ref!r}"))
        return _Resolved(el.center_x, el.center_y, el, None, note)
    selector = getattr(action, "selector", None)
    if selector:
        el = _resolve_desktop_el(wid, win.name, selector=selector)
        if not el:
            return _Resolved(0, 0, None, f"No desktop element matching '{selector}'")
        return _Resolved(el.center_x, el.center_y, el, None, note)
    return _Resolved(0, 0, None, "Provide x,y, name, ref, selector, or element for desktop action")


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


async def _click_element(page, mgr, element: int, tab: int, button: str = "left") -> bool:
    """Click the numbered ``element`` on ``page``. Prefers the stored element map (its ref →
    locator, or center coordinates); if the map has no entry — it was cleared, or the ref came
    from a scan in a separate call — falls back to the live ``data-interact-ref="e{N}"`` attribute,
    which persists on the DOM across tool calls until the next scan. So a ref from an earlier
    get_interactive_elements still clicks even when the server-side map is stale (#34). Returns
    False only when the element resolves by neither route (a genuinely stale ref)."""
    el = mgr.get_element(element, tab)
    if el is not None:
        if el.ref:
            await page.locator(el.playwright_ref).click(button=button)
        else:
            await page.mouse.click(el.center_x, el.center_y, button=button)
        return True
    locator = page.locator(ref_locator(f"e{element}"))
    if await locator.count() == 1:  # the badge is still on the live DOM — resolve it directly
        await locator.click(button=button)
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


def _click_verb(action) -> str:
    """"clicked" / "right-clicked" / "middle-clicked" — the report must NAME a non-left button so
    the agent can see WHICH click it made; a right-click that opened no context menu is otherwise
    indistinguishable from a left-click in the transcript (#91)."""
    button = getattr(action, "button", "left")
    return "clicked" if button == "left" else f"{button}-clicked"


def _button_prefix(action) -> str:
    """A non-left BROWSER click, prefixed onto the step's change description: the browser reports a
    click as a before/after state diff, which alone never reveals which button was pressed (#91)."""
    return "" if getattr(action, "button", "left") == "left" else f"{_click_verb(action)}: "


def _el_report(verb: str, el, note: str = "") -> str:
    # Reference elements by ref/index + role/name — never pixel coordinates. The agent
    # acts via refs; resolved coords are a dispatch implementation detail it must not see.
    return f"{verb} [{el.index}] {el.role}: {el.name!r} cursor={_fmt_cursor()}{note}"


def _xy_report(verb: str, x: int, y: int, note: str = "") -> str:
    # A raw-coordinate action reports the ACTUAL coordinates it acted on: this said only "at
    # coordinates", which — combined with the old snap — left the agent unable to tell where the
    # click actually landed (#81). `note` carries the hedged cached-detection annotation.
    return f"{verb} at ({x},{y}) cursor={_fmt_cursor()}{note}"


def _report_with_change(win_name: str, before: DesktopState, report: str) -> str:
    after = DesktopState.capture(win_name)
    change = DesktopState.compute_change(before, after)
    if change:
        report += f"\n  \u2192 {change}"
    return report


async def _settle_and_diff(mgr, page, action, tab: int, before: PageState):
    """The tail every mutating BROWSER branch repeats: honour the action's own ``wait``, recapture,
    and diff against ``before``. Returns ``(final_state, description)`` — the state is handed back
    rather than set here because the batch's closing summary reads it (#71/#65).

    The desktop twin is :func:`_mutating_step`; a browser step can't use a context manager for the
    same job because ``final`` has to reach the enclosing runner."""
    from interact.server import _capture, _wait as _wait_fn  # noqa: PLC0415 — circular

    if action.wait:
        await _wait_fn(page, action.wait)
    final = await _capture(mgr, tab=tab)
    return final, StateChange.compute(before, final).description


class _StepReport:
    """What a mutating desktop step produces, set inside :func:`_mutating_step`: ``text`` is the
    verb line (annotated with the window change), ``suffix`` is appended AFTER that annotation —
    for a warning that must not read as part of the change, e.g. a verified-dropped type (#93)."""

    __slots__ = ("text", "suffix")

    def __init__(self) -> None:
        self.text = ""
        self.suffix = ""


@asynccontextmanager
async def _mutating_step(win: DesktopWindow, i: int, action, step_reports: list[str]):
    """The shape EVERY mutating desktop branch repeats: snapshot the window, act, then append the
    step's report annotated with what changed. Each branch had its own hand-written copy of the
    four lines, so a change to how a desktop mutation reports had to be made six times and drifted
    (#71). The branch now contributes only its own action and verb:

        async with _mutating_step(win, i, action, step_reports) as step:
            await win.press_key(action.key)
            step.text = f"pressed {action.key}"
    """
    before = DesktopState.capture(win.name)
    step = _StepReport()
    yield step
    report = _report_with_change(win.name, before, step.text) + step.suffix
    step_reports.append(_step(i, action.type, report))


class _DesktopCtx(NamedTuple):
    """Everything a desktop action handler needs. Passed instead of closing over the runner's
    locals, so each handler is an independent function rather than a branch of one long ladder."""

    win: DesktopWindow
    wid: int
    i: int
    action: object
    step_reports: list[str]
    snapshots: dict[int, bytes]
    step_idx: int
    invocation_id: str | None

    def say(self, report: str) -> None:
        self.step_reports.append(_step(self.i, self.action.type, report))

    def skip(self, why: str) -> None:
        self.say(f"SKIPPED: {why}")


# type string -> handler. Replaces the isinstance ladder the desktop runner used to be: adding an
# action is now one registration next to its behaviour, not an edit to a 200-line chain, and each
# handler is reachable (and testable) on its own (#71).
_DESKTOP_HANDLERS: dict[str, "Callable"] = {}


def _handles(*types: str):
    def register(fn):
        for t in types:
            _DESKTOP_HANDLERS[t] = fn
        return fn

    return register


@_handles("sleep")
async def _d_sleep(c: _DesktopCtx) -> None:
    await asyncio.sleep(c.action.duration)
    c.say(f"waited {c.action.duration}s")


@_handles("wait_for")
async def _d_wait_for(c: _DesktopCtx) -> None:
    # Bare pause only — the DOM-bearing forms are rejected by the runner's browser-only guard.
    c.say(await c.action.execute(None))


@_handles("resize")
async def _d_resize(c: _DesktopCtx) -> None:
    # #84: resize the native window in-band, instead of the reporter's `xdotool windowsize`
    # workaround. The window's own geometry is re-read by resize(), so before→after is the
    # honest report — and a WM that clamps the request shows up as a mismatch.
    action, win = c.action, c.win
    before = f"{win.w}x{win.h}"
    if not await win.resize(action.width, action.height):
        c.skip(
            f"this target cannot be resized (still {before}) — a screen target has no window "
            "to resize; relaunch the app at the wanted size"
        )
        return
    after = f"{win.w}x{win.h}"
    report = f"resized {before} -> {after}"
    if after != f"{action.width}x{action.height}":
        report += f" (requested {action.width}x{action.height}; the WM adjusted it)"
    # Every cached box describes the OLD layout now — say so rather than let the next
    # ref-targeted step click a widget that moved (#88).
    c.say(report + ". Element refs are now stale — re-run get_interactive_elements")


@_handles("click", "click_element")
async def _d_click(c: _DesktopCtx) -> None:
    x, y, el, err, note = _resolve_action_coords(c.action, c.wid, c.win)
    if err:
        c.skip(err)
        return
    async with _mutating_step(c.win, c.i, c.action, c.step_reports) as step:
        # click_element carries no `button`, so default left for it (#91).
        await c.win.click(x, y, getattr(c.action, "button_code", 1))
        await asyncio.sleep(0.05)
        verb = _click_verb(c.action)
        step.text = _el_report(verb, el, note) if el else _xy_report(verb, x, y, note)


@_handles("hover")
async def _d_hover(c: _DesktopCtx) -> None:
    x, y, el, err, note = _resolve_action_coords(c.action, c.wid, c.win)
    if err:
        c.skip(err)
        return
    await c.win.hover(x, y)
    await asyncio.sleep(0.05)
    c.say(_el_report("hovered", el, note) if el else _xy_report("hovered", x, y, note))


@_handles("type_text")
async def _d_type_text(c: _DesktopCtx) -> None:
    action, win = c.action, c.win
    fx = fy = None
    if action.name or action.ref or action.selector:
        x, y, _, err, _ = _resolve_action_coords(action, c.wid, win)
        if err:
            c.skip(err)
            return
        await win.click(x, y)
        await asyncio.sleep(_TYPE_FOCUS_SETTLE)  # let the toolkit wire up text input (#59)
        fx, fy = x, y
    async with _mutating_step(win, c.i, action, c.step_reports) as step:
        if action.clear_first:
            await win.press_key("ctrl+a")
            await win.press_key("Delete")
        undelivered = await _type_desktop(win, action.text, fx, fy)
        step.text = f"typed {len(action.text)} chars"
        if undelivered:  # a verified drop must not read as a success (#93)
            step.suffix = f"\n  {undelivered}"


@_handles("key_press")
async def _d_key_press(c: _DesktopCtx) -> None:
    async with _mutating_step(c.win, c.i, c.action, c.step_reports) as step:
        await c.win.press_key(c.action.key)
        step.text = f"pressed {c.action.key}"


@_handles("scroll")
async def _d_scroll(c: _DesktopCtx) -> None:
    # The wheel goes to the widget UNDER the pointer, so position IS the target: honor the
    # action's anchor (x,y / ref / selector / name) like click does — the hardcoded window center
    # used to zoom an app's canvas instead of scrolling the dock the caller aimed at (#76).
    action, win = c.action, c.win
    if any(getattr(action, a, None) is not None for a in ("x", "ref", "selector", "name")):
        sx, sy, el, err, _ = _resolve_action_coords(action, c.wid, win)
        if err:
            c.skip(err)
            return
    else:
        sx, sy, el = win.w // 2, win.h // 2, None
    async with _mutating_step(win, c.i, action, c.step_reports) as step:
        await win.scroll(sx, sy, action.direction, action.amount)
        at = f" at {el.name!r}" if el is not None and el.name else f" at ({sx},{sy})"
        step.text = f"scrolled {action.direction} x{action.amount}{at}"


@_handles("drag")
async def _d_drag(c: _DesktopCtx) -> None:
    from interact.server import _resolve_desktop_el  # noqa: PLC0415 — circular

    action, win = c.action, c.win
    fx, fy = action.from_x, action.from_y
    tx, ty = action.to_x, action.to_y
    for ref_attr, label in (("from_ref", "from_ref"), ("to_ref", "to_ref")):
        ref = getattr(action, ref_attr)
        if not ref:
            continue
        el = _resolve_desktop_el(c.wid, win.name, ref=ref)
        if el is None:
            c.skip(f"{label} element {ref!r} not found")
            return
        if ref_attr == "from_ref":
            fx, fy = el.center_x, el.center_y
        else:
            tx, ty = el.center_x, el.center_y
    async with _mutating_step(win, c.i, action, c.step_reports) as step:
        await win.drag(fx, fy, tx, ty, action.steps)
        step.text = f"dragged ({fx},{fy})->({tx},{ty})"


@_handles("screenshot")
async def _d_screenshot(c: _DesktopCtx) -> None:
    from interact.server import _capture_desktop  # noqa: PLC0415 — circular

    screenshot_bytes, report = await _capture_desktop(c.win, c.action.query, c.action.path)
    c.snapshots[c.step_idx] = screenshot_bytes
    Debug.step_save(
        c.invocation_id, c.i, c.action.type, "screenshot", screenshot_bytes, ext="png"
    )
    c.say(report)


@_handles("annotate")
async def _d_annotate(c: _DesktopCtx) -> None:
    from interact.server import _annotate_desktop  # noqa: PLC0415 — circular

    _, report = await _annotate_desktop(c.win, c.action.query, invocation_id=c.invocation_id)
    c.snapshots[c.step_idx] = c.win.capture()
    c.say(report)


@_handles("http_request")
async def _d_http_request(c: _DesktopCtx) -> None:
    c.say(str(await c.action.execute(None)))


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

        # A BARE `wait_for` (timeout only) is a plain pause, meaningful on any surface, so it is
        # not rejected here even though `wait_for` is otherwise browser-only; its selector/text
        # forms need a DOM and say so precisely.
        if action.type in BROWSER_ONLY_ACTIONS and not getattr(action, "is_pause", False):
            hint = (
                "a selector/text wait needs a DOM — on a desktop target use a bare wait_for "
                "(timeout only) to pause, then screenshot to check the state"
                if isinstance(action, WaitForAction)
                else "use a session instead of window"
            )
            step_reports.append(
                _step(i, action.type, f"Action '{action.type}' is browser-only — {hint}")
            )
            continue

        handler = _DESKTOP_HANDLERS.get(action.type)
        if handler is None:
            step_reports.append(
                _step(i, action.type, f"Action '{action.type}' not supported on desktop")
            )
        else:
            await handler(
                _DesktopCtx(win, wid, i, action, step_reports, snapshots, step_idx, invocation_id)
            )

        await asyncio.sleep(0.1)

        await _finalize_step(
            action, i, step_idx, invocation_id, step_reports, record_frames, snapshots,
            capture_fn=win.capture, context=_desktop_context(win),
        )

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

        # The mirror of the desktop runner's browser-only guard: a native-window action has no
        # browser meaning, so name the browser-side equivalent instead of failing obscurely (#84).
        if action.type in DESKTOP_ONLY_ACTIONS:
            step_reports.append(
                _step(
                    i,
                    action.type,
                    f"Action '{action.type}' is desktop-only (it resizes a native window) — for a "
                    "browser viewport use emulate_device (width+height, or a device name like "
                    "'iPhone 13'), which sets true device metrics",
                )
            )
            continue

        if isinstance(action, CompareAction):
            ctx = f"Browser session comparison of steps {action.steps}"
            result = await _run_compare(snapshots, action.steps, action.query, ctx)
            step_reports.append(_step(i, action.type, result))
            continue

        if isinstance(action, HandleDialogAction):
            mgr.arm_dialog(action.action, action.prompt_text)
            answer = f" answering {action.prompt_text!r}" if action.prompt_text else ""
            step_reports.append(
                _step(i, action.type, f"armed: the next dialog will be {action.action}ed{answer}")
            )
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
            final, desc = await _settle_and_diff(mgr, page, action, current_tab, before)
            step_reports.append(_step(i, action.type, desc))

        elif isinstance(action, ClickAction) and (
            action.name or action.element is not None
        ):
            before = await _capture(mgr, tab=current_tab)
            if action.name:
                locator = await _named_locator(page, action)
                await locator.click(button=action.button)
            elif not await _click_element(
                page, mgr, action.element, current_tab, action.button
            ):
                step_reports.append(_step(i, action.type, _element_miss(action.element)))
                continue
            final, desc = await _settle_and_diff(mgr, page, action, current_tab, before)
            step_reports.append(_step(i, action.type, _button_prefix(action) + desc))

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
            final, desc = await _settle_and_diff(mgr, page, action, current_tab, before)
            step_reports.append(_step(i, action.type, desc))

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
            final, desc = await _settle_and_diff(mgr, page, action, current_tab, before)
            entry = _step(i, action.type, _button_prefix(action) + desc)
            if result is not None:
                entry += f"\n  result: {result}"
            step_reports.append(entry)

        # Surface any native dialog this step triggered — an auto-dismissed confirm() used to
        # no-op a click with zero trace, the worst default for admin UIs (#77).
        for msg in mgr.drain_dialog_log():
            step_reports.append(_step(i, "dialog", msg))

        await _finalize_step(
            action, i, step_idx, invocation_id, step_reports, record_frames, snapshots,
            capture_fn=lambda: page.screenshot(type="png"), context=f"Browser step {step_idx}",
        )

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
