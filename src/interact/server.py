import base64
from contextlib import asynccontextmanager
import json
import os
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import litellm as _litellm

_log = logging.getLogger("interact")

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from playwright.async_api import Page

from interact import desktop
from interact.atspi import AtSpi
from interact.actions import AnyAction
from interact.browser import BrowserManager, SessionRegistry
from interact.config import DEFAULT_LIMIT
from interact.debug_utils import Debug
from interact.desktop import DesktopElement, DesktopWindow
from interact.detect import (
    _crop_image,
    _desktop_context,
    _detect_desktop_elements,
)
from interact.dispatch import (
    _run_actions_browser,
    _run_actions_desktop,
)
from interact.runtime import breaker, config
from interact.state import (
    InteractiveElement,
    PageState,
    annotate_screenshot,
    format_element_list,
)
from interact.vision import (
    MediaItem,
    VLMResult,
    _UNSET,
    _Unset,
    analyze_media,
    analyze_screenshot,
)

_log.info(
    "Models: image=%s, component=%s, video=%s",
    config.image_model or "not set",
    config.component_model or "not set",
    config.video_model or "not set",
)
_sessions = SessionRegistry(config)
_DEFAULT_SESSION = "default"
_NO_WINDOWS_MSG = "No desktop windows detected (X11/maim required)."
_ANNOTATE_JS = (Path(__file__).parent / "js" / "annotate_elements.js").read_text()
_DBG_ELEMENTS = "get_interactive_elements"
_DBG_ACTIONS = "run_actions"


_MAX_FALLBACKS = 3


async def _vlm(
    data: bytes,
    context: str,
    query: str | None = None,
    media_type: str = "image",
    mime: str = "image/png",
    max_tokens: int | None | _Unset = _UNSET,
    response_format: type | dict | None = None,
    model_override: str | None = None,
) -> VLMResult:
    import asyncio

    item_type = "video" if media_type == "video" else "image"
    routing = media_type or "image"
    effective_model = model_override or config.model_for(routing)

    async def _call(model_id: str | None) -> VLMResult:
        return await analyze_media(
            [MediaItem.from_bytes(data, item_type, mime)],
            context,
            config,
            query,
            max_tokens=max_tokens,
            response_format=response_format,
            config_media_type=media_type,
            model_override=model_id,
        )

    try:
        return await _call(model_override)
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as primary_err:
        primary_type = type(primary_err).__name__
        _log.warning(
            "%s on %s, attempting fallback chain", primary_type, effective_model
        )
        breaker.trip(effective_model)

        chain = config.chain_for(routing)
        candidates = [
            m
            for m in chain.preferences
            if m.id != effective_model
            and not breaker.tripped(m.id)
            and m.is_available()
        ]

        prev_model = effective_model
        prev_err_type = primary_type
        last_err: Exception = primary_err
        for fallback in candidates[:_MAX_FALLBACKS]:
            try:
                result = await _call(fallback.litellm_id())
                result.text = (
                    f"[Fallback: used {fallback.id} after {prev_model} "
                    f"failed with {prev_err_type}]\n\n{result.text}"
                )
                result.model = fallback.id
                return result
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as err:
                breaker.trip(fallback.id)
                prev_model = fallback.id
                prev_err_type = type(err).__name__
                last_err = err

        return VLMResult(
            text=(
                f"[All {1 + len(candidates[:_MAX_FALLBACKS])} fallbacks failed "
                f"— last error on {prev_model}: {type(last_err).__name__}]"
            ),
            elapsed=0,
            model=effective_model,
        )


def _fmt_timing(r: VLMResult) -> str:
    model_tag = f" {r.model}" if r.model else ""
    return f"{r.text}\n(VLM:{model_tag} {r.elapsed:.1f}s)"


async def _run_observe(
    screenshot_bytes: bytes,
    query: str,
    context: str,
) -> str:
    try:
        r = await _vlm(screenshot_bytes, context, query)
        return _fmt_timing(r)
    except Exception as e:
        return f"observe error: {e}"


async def _run_compare(
    snapshots: dict[int, bytes],
    steps: list[int],
    query: str,
    context: str,
) -> str:
    missing = [s for s in steps if s not in snapshots]
    if missing:
        return ", ".join(
            f"Step {s} has no snapshot — add observe to that action" for s in missing
        )
    try:
        media = [MediaItem.from_bytes(snapshots[s]) for s in steps]
        r = await analyze_media(media, context, config, query)
        return _fmt_timing(r)
    except Exception as e:
        return f"compare error: {e}"


async def _media_response(
    data: bytes,
    context: str,
    query: str | None = None,
    path: str | None = None,
    media_type: str = "image",
    mime: str = "image/png",
    model_override: str | None = None,
) -> str | None:
    if path:
        _save_to_path(path, data)
    if not query:
        return None
    r = await _vlm(
        data, context, query, media_type, mime, model_override=model_override
    )
    return _fmt_timing(r)


def _find_desktop_window(title: str) -> DesktopWindow | str:
    windows = DesktopWindow.all()
    if not windows:
        return _NO_WINDOWS_MSG
    win = DesktopWindow.find(title, windows)
    if win is None:
        return f"No window matching '{title}'. Available:\n{DesktopWindow.listing(windows)}"
    return win


def _resolve_target(
    window: str | None,
    session: str,
) -> tuple[DesktopWindow | None, BrowserManager | None, str | None]:
    if window and session != _DEFAULT_SESSION:
        return None, None, "Cannot use both window and session"
    if window:
        result = _find_desktop_window(window)
        if isinstance(result, str):
            return None, None, result
        return result, None, None
    return None, _sessions.get(session), None


def _save_to_path(path: str, data: bytes):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _session_response(session: str, body: str) -> str:
    return f"[session: {session}]\n{body}"


async def _capture_desktop(
    win: DesktopWindow,
    query: str | None = None,
    path: str | None = None,
    model_override: str | None = None,
) -> tuple[bytes, str]:
    screenshot_bytes = win.capture()
    context = _desktop_context(win)
    result = await _media_response(
        screenshot_bytes, context, query, path, model_override=model_override
    )
    return screenshot_bytes, result or context


async def _annotate_desktop(
    win: DesktopWindow,
    query: str | None = None,
    crop: tuple[int, int, int, int] | None = None,
    invocation_id: str | None = None,
    method: str = "default",
    model_override: str | None = None,
) -> tuple[list[DesktopElement] | None, str]:
    (
        screenshot_bytes,
        elements,
        vlm_raw,
        elapsed,
        method_label,
        img_w,
        img_h,
    ) = await _detect_desktop_elements(
        win,
        crop,
        invocation_id=invocation_id,
        method=method,
        model_override=model_override,
        query=query,
    )
    # Build enriched detection report
    parts = [f"method={method_label}"]
    if "vlm" in method_label or "fused" in method_label:
        # method_label already contains model name: "vlm gpt-4.1-mini" or "fused+gpt-4.1-mini"
        pass
    parts.extend([f"{len(elements)} elements", f"{elapsed:.2f}s"])
    timing = f"Detection: {' | '.join(parts)}"
    if not elements:
        detail = f"VLM response:\n{vlm_raw}" if vlm_raw else "No elements detected"
        return None, f"Could not detect elements. {timing}\n{detail}"
    try:
        if crop:
            ann_elements = [el.translate(-crop[0], -crop[1]) for el in elements]
        else:
            ann_elements = elements
        annotated = annotate_screenshot(screenshot_bytes, ann_elements)
    except Exception:
        _log.warning(
            "annotate_desktop: failed to generate annotated image (%d elements)",
            len(elements),
            exc_info=True,
        )
        element_list = DesktopElement.format_list(elements)
        return (
            elements,
            f"Elements detected but annotation failed.\n{element_list}\n{timing}",
        )
    Debug.save(
        "annotated",
        annotated,
        ext="png",
        invocation_id=invocation_id,
    )
    element_list = DesktopElement.format_list(elements)
    context = f"Annotated desktop window with {len(elements)} elements:\n{element_list}"
    result = await _media_response(
        annotated,
        context,
        query,
        model_override=model_override,
    )
    return elements, f"{result or context}\n{timing}"


def _resolve_desktop_el(
    wid: int,
    win_name: str,
    *,
    ref: str | None = None,
    selector: str | None = None,
    element: int | None = None,
) -> DesktopElement | None:
    if ref:
        return DesktopElement.get_by_index(wid, DesktopElement.ref_to_index(ref))
    if selector:
        return AtSpi.find_element_by_name(win_name, selector)
    if element is not None:
        return DesktopElement.get_by_index(wid, element)
    return None


def _not_found(what: str) -> str:
    return f"{what} not found \u2014 run get_interactive_elements first"


def _name_not_found_msg(win_name: str, name: str) -> str:
    elements = AtSpi.detect_elements(win_name)
    if not elements:
        return f"No element with name='{name}' (no elements detected via AT-SPI)"
    names = sorted({e.name for e in elements if e.name})[:10]
    return (
        f"No element with name='{name}'. Available: {', '.join(repr(n) for n in names)}"
    )


def _desktop_label(win: DesktopWindow) -> str:
    return f"[window: {win.name}]"


@asynccontextmanager
async def _lifespan(_: FastMCP) -> AsyncIterator[None]:
    yield
    await _sessions.close_all()


mcp = FastMCP("interact", lifespan=_lifespan)


async def _capture(mgr: BrowserManager, scope: str | None = None, tab: int = 0):
    page = await mgr.get_page(tab)
    state = await PageState.capture(page, scope=scope)
    return state


async def _annotate_page(
    mgr: BrowserManager,
    tab: int = 0,
    scope: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[bytes, list[InteractiveElement]]:
    page = await mgr.get_page(tab)
    raw_boxes = await page.evaluate(_ANNOTATE_JS, {"scope": scope, "limit": limit})
    elements = [
        InteractiveElement(
            index=i + 1,
            ref=raw["ref"],
            role=raw["tag"],
            name=raw["name"],
            x=raw["x"],
            y=raw["y"],
            width=raw["width"],
            height=raw["height"],
        )
        for i, raw in enumerate(raw_boxes)
    ]
    screenshot_bytes = await page.screenshot(type="png")
    return annotate_screenshot(screenshot_bytes, elements), elements


async def _annotate_and_describe(
    mgr: BrowserManager,
    tab: int = 0,
    scope: str | None = None,
    query: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> str:
    annotated_bytes, elements = await _annotate_page(mgr, tab, scope, limit)
    mgr.set_element_map(tab, elements)
    element_list = format_element_list(elements)
    context = (
        f"Annotated page with {len(elements)} interactive elements:\n{element_list}"
    )
    result = await _media_response(annotated_bytes, context, query)
    return result or context


async def _analyze(
    state: PageState, query: str | None = None, model_override: str | None = None
) -> str:
    if model_override:
        media = [MediaItem(data=state.screenshot_base64)]
        r = await analyze_media(
            media,
            f"Page: {state.title} ({state.url})",
            config,
            query,
            model_override=model_override,
        )
    else:
        r = await analyze_screenshot(state, config, query)
    return _fmt_timing(r)


async def _element_screenshot(
    mgr: BrowserManager,
    tab: int,
    selector: str | None,
    element: int | None,
    query: str | None = None,
    path: str | None = None,
) -> str:
    page = await mgr.get_page(tab)

    if element is not None:
        el = mgr.get_element(element, tab)
        if el is None:
            return _not_found(f"Element {element}")
        if not el.playwright_ref:
            return f"Element {element} has no ref attribute — cannot screenshot"
        locator = page.locator(el.playwright_ref)
        meta = f"[{el.index}] {el.role}: {el.name!r} ({el.width:.0f}x{el.height:.0f} at {el.x:.0f},{el.y:.0f})"
    else:
        locator = page.locator(selector)
        count = await locator.count()
        if count == 0:
            return f"No element matches '{selector}'"
        if count > 1:
            return f"'{selector}' matches {count} elements — use get_interactive_elements and element for precision"
        tag = await locator.evaluate("el => el.tagName.toLowerCase()")
        text = (await locator.inner_text())[:200]
        box = await locator.bounding_box()
        meta = f"{tag}: {text!r}"
        if box:
            meta += f" ({box['width']:.0f}x{box['height']:.0f} at {box['x']:.0f},{box['y']:.0f})"

    try:
        png_bytes = await locator.screenshot(type="png")
    except Exception as e:
        return f"Cannot screenshot element: {e}"
    result = await _media_response(png_bytes, meta, query, path)
    return result or meta


async def _wait(page: Page, condition: str | None):
    if condition is None:
        return
    if condition in ("networkidle", "domcontentloaded", "load"):
        await page.wait_for_load_state(condition)
    else:
        await page.wait_for_selector(
            condition, state="visible", timeout=config.wait_timeout
        )


@mcp.tool()
async def navigate(
    url: str,
    query: str | None = None,
    scope: str | None = None,
    wait: str | None = None,
    debug_dir: str | None = None,
    session: str = _DEFAULT_SESSION,
) -> str:
    """Navigate to a URL and return page content. Browser-only — requires a session, not a window.

    scope: CSS selector to restrict to a page sub-tree.
    wait: "networkidle", "load", "domcontentloaded", or a CSS selector (waits for visibility, 10s timeout).
    query: when set, returns vision analysis instead of text summary.
    debug_dir: when set, dump inputs/outputs/screenshots to this directory for debugging.
    """
    inv = Debug.new_invocation_dir(debug_dir, "navigate")
    Debug.dump_input(inv, {"tool": "navigate", "url": url, "query": query, "scope": scope,
                           "wait": wait, "session": session}, config.model_dump(mode="json"))
    mgr = _sessions.get(session)
    page = await mgr.get_page()
    await page.goto(url)
    await _wait(page, wait)
    state = await _capture(mgr, scope)
    if state.screenshot_base64:
        Debug.save(
            "page",
            base64.b64decode(state.screenshot_base64),
            ext="png",
            invocation_id=inv,
        )
    if query:
        result = _session_response(session, await _analyze(state, query))
    else:
        result = _session_response(session, state.text_summary())
    Debug.dump_output(inv, result)
    return result


@mcp.tool()
async def run_actions(
    actions: list[AnyAction],
    query: str | None = None,
    scope: str | None = None,
    wait: str | None = None,
    debug_dir: str | None = None,
    window: str | None = None,
    session: str = _DEFAULT_SESSION,
) -> str:
    """Execute a sequence of actions on a browser session or desktop window.

    PREFER REFS OVER COORDINATES: first call get_interactive_elements, then target elements by
    `ref` (browser) or `element` index (desktop) in click/type_text/hover/drag. Raw `x`,`y` are
    a last resort — they break on any layout shift, scroll, or window move; refs do not.

    Default: operates on browser session "default".
    With window: operates on a desktop window by title (use list_desktop_windows to discover).
    window and session are mutually exclusive.

    Each action needs a 'type' key to select the action model.

    Mutating: click, type_text, scroll, drag, navigate, evaluate_js, upload_file, key_press, click_element
    Observations: screenshot, wait_for, http_request, hover, annotate
    Tab control: new_tab, switch_tab, close_tab
    Timing: sleep — pause execution for a duration (max 30s), useful for waiting on animations or delayed UI updates.
    Comparison: compare — VLM comparison of snapshots from earlier steps (by 1-based index).

    Browser-only actions (navigate, evaluate_js, wait_for, upload_file, new_tab, switch_tab, close_tab) error when used with window.

    Any action can include 'wait' to wait after execution (networkidle, load, domcontentloaded, or a CSS selector — browser only).
    Any action can include 'observe' (a VLM query string) to capture a screenshot after execution and analyze it. The snapshot is stored by step index for later compare actions.

    scope: CSS selector to restrict the final capture to a page sub-tree (browser only).
    wait: after all actions, wait for a condition (browser only).
    query: when set, returns vision analysis of the final state instead of text summary.
    debug_dir: when set, dump inputs/outputs/screenshots to this directory for debugging.
    """
    inv = Debug.new_invocation_dir(debug_dir, _DBG_ACTIONS)
    Debug.dump_input(inv, {"tool": "run_actions", "actions": [a.model_dump() for a in actions],
                           "query": query, "scope": scope, "wait": wait, "window": window,
                           "session": session}, config.model_dump(mode="json"))
    win, mgr, err = _resolve_target(window, session)
    if err:
        Debug.dump_output(inv, err)
        return err
    if win:
        result = await _run_actions_desktop(win, actions, query, invocation_id=inv)
    else:
        result = await _run_actions_browser(
            mgr, actions, query, scope, wait, session, invocation_id=inv
        )
    Debug.dump_output(inv, result)
    return result


@mcp.tool()
async def screenshot(
    query: str | None = None,
    scope: str | None = None,
    selector: str | None = None,
    element: int | None = None,
    path: str | None = None,
    return_image: bool = False,
    debug_dir: str | None = None,
    window: str | None = None,
    session: str = _DEFAULT_SESSION,
    model: str | None = None,
):
    """Capture the current page or a desktop window.

    Default: operates on browser session "default".
    With window: captures a desktop window by title (use list_desktop_windows to discover).
    window and session are mutually exclusive.

    Returns depend on parameters:
    - No selector/element, no query: page title + visible text content (browser) or window metadata (desktop).
    - No selector/element, with query: full screenshot analyzed by VLM.
    - With selector/element, no query: element metadata (browser only).
    - With selector/element, with query: cropped element screenshot analyzed by VLM (browser only).

    element: integer index from get_interactive_elements (priority over selector).
    selector: CSS selector targeting one element (browser only).
    query: question for VLM visual analysis of the captured content.
    scope: CSS selector to restrict text extraction to a sub-tree (browser only).
    path: save the PNG screenshot to this file path.
    return_image: when True, return the raw screenshot bytes as an MCP ImageContent alongside the text,
        so the calling agent can SEE the pixels directly (not just a VLM summary).
    model: override the configured VLM model for this call. Uses the VS Code configured model when not set.
    """
    inv = Debug.new_invocation_dir(debug_dir, "screenshot")
    Debug.dump_input(inv, {"tool": "screenshot", "query": query, "scope": scope, "selector": selector,
                           "element": element, "window": window, "session": session, "model": model},
                     config.model_dump(mode="json"))
    win, mgr, err = _resolve_target(window, session)
    if err:
        Debug.dump_output(inv, err)
        return err
    img_bytes: bytes | None = None
    if win:
        if element is not None:
            el = _resolve_desktop_el(win.wid, win.name, element=element)
            if el is None:
                nf = _not_found(f"Element {element}")
                Debug.dump_output(inv, nf)
                return nf
            raw = win.capture()
            img_bytes = _crop_image(raw, el.x, el.y, el.w, el.h)
            meta = (
                f"[{el.index}] {el.role}: {el.name!r} ({el.w}x{el.h} at {el.x},{el.y})"
            )
            result = await _media_response(
                img_bytes,
                meta,
                query,
                path,
                model_override=model,
            )
            text = f"{_desktop_label(win)}\n{result or meta}"
        else:
            img_bytes, description = await _capture_desktop(
                win, query, path, model_override=model
            )
            text = f"{_desktop_label(win)}\n{description}"
    elif element is not None or selector is not None:
        text = _session_response(
            session, await _element_screenshot(mgr, 0, selector, element, query, path)
        )
    else:
        state = await _capture(mgr, scope)
        img_bytes = base64.b64decode(state.screenshot_base64)
        if path:
            _save_to_path(path, img_bytes)
        if query:
            text = _session_response(
                session, await _analyze(state, query, model_override=model)
            )
        else:
            text = _session_response(session, state.text_summary())
    if img_bytes is not None:
        Debug.save("capture", img_bytes, ext="png", invocation_id=inv)
    result = [text, Image(data=img_bytes, format="png")] if (return_image and img_bytes is not None) else text
    Debug.dump_output(inv, result)
    return result


@mcp.tool()
async def get_interactive_elements(
    scope: str | None = None,
    query: str | None = None,
    element: int | None = None,
    limit: int = DEFAULT_LIMIT,
    tab: int = 0,
    debug_dir: str | None = None,
    window: str | None = None,
    session: str = _DEFAULT_SESSION,
    method: str = "default",
    model: str | None = None,
) -> str:
    """Annotate interactive elements with numbered badges and return their details. Call this
    FIRST, then act by the returned `ref`/`element` in run_actions — never guess pixel x,y.

    Default: operates on browser session "default". Sets data-interact-ref attributes on DOM elements.
    With window: uses VLM to detect interactive elements in a desktop window screenshot.
    window and session are mutually exclusive. Use list_desktop_windows to discover windows.

    Returns a numbered list with role/name for each element.
    Use element indices in subsequent click_element actions, or ref values for click/type_text/hover (browser only).
    scope: CSS selector to restrict to a page sub-tree (browser only).
    element: re-detect within a previously detected element's bounding box (crop and refine, window only).
    limit: Maximum number of elements to return (browser only).
    With query, also returns a vision analysis of the annotated screenshot.
    debug_dir: when set, dump inputs/outputs/screenshots to this directory for debugging.
    method: detection strategy — "default" (AT-SPI with VLM fallback) or "vlm" (force VLM only). Applies to desktop windows.
    model: override the configured VLM model for this call. Uses the VS Code configured model when not set.
    """
    inv = Debug.new_invocation_dir(debug_dir, _DBG_ELEMENTS)
    Debug.dump_input(inv, {"tool": "get_interactive_elements", "query": query, "scope": scope,
                           "element": element, "limit": limit, "tab": tab, "window": window,
                           "session": session, "method": method, "model": model},
                     config.model_dump(mode="json"))
    win, mgr, err = _resolve_target(window, session)
    if err:
        Debug.dump_output(inv, err)
        return err
    if win:
        crop = None
        if element is not None:
            el = _resolve_desktop_el(win.wid, win.name, element=element)
            if el is None:
                nf = _not_found(f"Element {element}")
                Debug.dump_output(inv, nf)
                return nf
            crop = (el.x, el.y, el.w, el.h)
        _, report = await _annotate_desktop(
            win,
            query,
            crop,
            invocation_id=inv,
            method=method,
            model_override=model,
        )
        result = f"{_desktop_label(win)}\n{report}"
    else:
        result = _session_response(
            session, await _annotate_and_describe(mgr, tab, scope, query, limit)
        )
    Debug.dump_output(inv, result)
    _log.info("get_interactive_elements: %s", "desktop" if win else "browser")
    return result


@mcp.tool()
async def get_page_state(
    scope: str | None = None, session: str = _DEFAULT_SESSION
) -> str:
    """Get current page URL, title, accessibility tree, focused element, and visible text. scope: CSS selector to restrict to a page sub-tree."""
    mgr = _sessions.get(session)
    state = await _capture(mgr, scope)
    return _session_response(
        session,
        f"URL: {state.url}\n"
        f"Title: {state.title}\n"
        f"Focused: {state.focused_element}\n\n"
        f"Accessibility Tree:\n{state.accessibility_tree}\n\n"
        f"Visible Text:\n{state.visible_text}",
    )


@mcp.tool()
async def list_sessions() -> str:
    """List all active browser sessions."""
    sessions = _sessions.active()
    if not sessions:
        return "No active sessions."
    return "\n".join(f"  {s}" for s in sessions)


@mcp.tool()
async def close_session(session: str = _DEFAULT_SESSION) -> str:
    """Close a browser session and free its resources."""
    await _sessions.close(session)
    return _session_response(session, f"Session '{session}' closed.")


@mcp.tool()
async def save_session(path: str, session: str = _DEFAULT_SESSION) -> str:
    """Export cookies and localStorage to a file for later restoration."""
    mgr = _sessions.get(session)
    state = await mgr.save_state()
    Path(path).write_text(json.dumps(state))
    return _session_response(session, f"Session '{session}' saved to {path}.")


@mcp.tool()
async def load_session(path: str, session: str = _DEFAULT_SESSION) -> str:
    """Restore cookies and localStorage from a previously saved session file."""
    state = json.loads(Path(path).read_text())
    mgr = _sessions.get(session)
    await mgr.load_state(state)
    return _session_response(session, f"Session '{session}' restored from {path}.")


@mcp.tool()
async def download_asset(url: str, path: str, session: str = _DEFAULT_SESSION) -> str:
    """Download a URL to a local file path. Uses the browser session's cookies for authenticated downloads."""
    mgr = _sessions.get(session)
    page = await mgr.get_page()
    response = await page.context.request.get(url)
    data = await response.body()
    _save_to_path(path, data)
    return _session_response(session, f"Downloaded {len(data)} bytes to {path}")


@mcp.tool()
async def get_network_log(
    clear: bool = False, limit: int = DEFAULT_LIMIT, session: str = _DEFAULT_SESSION
) -> str:
    """Return captured network requests (last `limit` entries). Set clear=True to flush the log after reading."""
    mgr = _sessions.get(session)
    entries = mgr.drain_network_log(clear)
    entries = entries[-limit:]
    if not entries:
        return _session_response(session, "No network requests captured.")
    lines = []
    for e in entries:
        status = e.get("status", "pending")
        ctype = e.get("content_type", "")
        lines.append(
            f"{e['method']} {status} {e['url']}" + (f" ({ctype})" if ctype else "")
        )
    return _session_response(session, "\n".join(lines))


@mcp.tool()
async def get_console_log(
    clear: bool = False, limit: int = DEFAULT_LIMIT, session: str = _DEFAULT_SESSION
) -> str:
    """Return captured browser console messages and errors (last `limit` entries). Set clear=True to flush after reading."""
    mgr = _sessions.get(session)
    entries = mgr.drain_console_log(clear)
    entries = entries[-limit:]
    if not entries:
        return _session_response(session, "No console messages captured.")
    lines = [f"[{e['level']}] {e['text']}" for e in entries]
    return _session_response(session, "\n".join(lines))


@mcp.tool()
async def list_desktop_windows() -> str:
    """List all visible desktop windows."""
    windows = DesktopWindow.all()
    if not windows:
        return _NO_WINDOWS_MSG
    return DesktopWindow.listing(windows)


@mcp.tool()
async def record(
    start: bool = True,
    query: str | None = None,
    duration: float | None = None,
    fps: int | None = None,
    path: str | None = None,
    window: str | None = None,
    session: str = _DEFAULT_SESSION,
) -> str:
    """Record actions as video and optionally analyze with vision.

    Browser (session): Two-step — record(start=True) then perform actions then record(start=False).
    Desktop (window): Records for duration seconds, then returns.
    window and session are mutually exclusive. Use list_desktop_windows to discover windows.

    start: True to begin recording, False to stop and export (browser only).
    query: question for VLM visual analysis of the recording.
    duration: recording length in seconds (when recording a window, default from config).
    fps: frames per second (when recording a window, default from config).
    path: save the video file to this path.
    """
    win, mgr, err = _resolve_target(window, session)
    if err:
        return err
    if win:
        return await _record_desktop(win, query, duration, fps, path)
    return await _record_browser(mgr, start, query, path, session)


async def _record_desktop(
    win: DesktopWindow,
    query: str | None,
    duration: float | None,
    fps: int | None,
    path: str | None,
) -> str:
    dur = duration or config.video_duration
    actual_fps = fps or config.video_fps
    video_bytes = win.capture_video(dur, actual_fps)
    if path:
        _save_to_path(path, video_bytes)

    is_static = not desktop.Motion.detect(video_bytes)
    if is_static and not query:
        return (
            f"Recording captured but no motion detected — frames are identical. "
            f"The window content did not change during the {dur}s recording."
        )

    context = f"Desktop window recording: {win.name} ({win.w}x{win.h}, {dur}s)"
    if is_static:
        context = (
            "WARNING: Recording appears static — no significant motion was detected "
            "between frames. Describe only what you actually observe.\n" + context
        )
    r = await _vlm(video_bytes, context, query, "video", "video/mp4")
    return _fmt_timing(r)


async def _record_browser(
    mgr: BrowserManager,
    start: bool,
    query: str | None,
    path: str | None,
    session: str,
) -> str:
    if start:
        url = await mgr.start_recording()
        return _session_response(session, f"Recording started. Current URL: {url}")
    video_bytes = await mgr.stop_recording()
    if not video_bytes:
        return _session_response(
            session, "Recording stopped but no video data captured."
        )
    result = await _media_response(
        video_bytes,
        "Browser recording",
        query,
        path,
        "video",
        "video/webm",
    )
    if result:
        return _session_response(session, result)
    size = len(video_bytes)
    msg = f"Recording stopped. Video captured ({size} bytes)."
    if path:
        msg += f" Saved to {path}."
    return _session_response(session, msg)


@mcp.tool()
async def configured_providers() -> str:
    """Return available VLM providers, models, and current configuration.

    Use this to discover what models can be passed as the 'model' override
    to get_interactive_elements and screenshot tools.
    """
    # Extension declaratively passes which providers have keys configured
    declared = os.environ.get("INTERACT_CONFIGURED_PROVIDERS", "")
    if declared:
        available = set(declared.split(","))
    else:
        # Fallback: scan env against litellm known providers
        known_providers: set[str] = {
            info.get("litellm_provider", "") for info in _litellm.model_cost.values()
        }
        known_providers.discard("")

        _extra_keys: dict[str, list[str]] = {
            "ollama": ["OLLAMA_API_KEY"],
            "zai": ["ZAI_API_KEY"],
        }
        _provider_aliases: dict[str, str] = {"google": "gemini"}

        available: set[str] = set()
        for key, val in os.environ.items():
            if not val:
                continue
            if key.endswith("_API_KEY"):
                candidate = key.removesuffix("_API_KEY").lower()
                candidate = _provider_aliases.get(candidate, candidate)
                if candidate in known_providers:
                    available.add(candidate)
            for provider, keys in _extra_keys.items():
                if key in keys:
                    available.add(provider)

    result: dict = {
        "config": {
            "image_model": config.image_model or None,
            "component_model": config.component_model or None,
            "video_model": config.video_model or None,
        },
        "available_providers": sorted(available),
    }

    # Warn on configured models whose provider has no key — via the env-key check, NOT
    # litellm.validate_environment (which can hang on interactive provider auth flows).
    from interact.models import Model

    Model.load_registry()
    warnings = []
    for model_name in [config.image_model, config.component_model, config.video_model]:
        if not model_name:
            continue
        model = Model.by_id(model_name)
        provider = model.provider if model else (model_name.split("/", 1)[0] if "/" in model_name else None)
        if provider and provider not in available:
            warnings.append(f"{model_name}: provider '{provider}' has no API key set")
    if warnings:
        result["warnings"] = warnings

    return json.dumps(result, indent=2)


def main():
    mcp.run(transport="stdio")
