"""Browser-session MCP tools: navigate, run_actions, get_page_state, session, download_asset,
get_logs. Thin surfaces — they resolve the target and delegate to the capture/vlm/dispatch
helpers."""

import base64
import json
from pathlib import Path
from typing import Literal

from interact.actions import AnyAction
from interact.browser import BrowserManager
from interact.config import DEFAULT_LIMIT
from interact.debug_utils import Debug
from interact.actions.dispatch import _run_actions_browser, _run_actions_desktop
from interact.server import capture, core, targets, vlm
from interact.server.core import _DBG_ACTIONS, _DEFAULT_SESSION, _session_response, config, mcp
from interact.state import format_element_list


@mcp.tool()
async def navigate(
    url: str,
    query: str | None = None,
    scope: str | None = None,
    wait: str | None = None,
    timeout: float | None = None,
    debug_dir: str | None = None,
    session: str = _DEFAULT_SESSION,
    http_credentials: str | None = None,
) -> str:
    """Navigate to a URL and return page content. Browser-only — requires a session, not a window.

    scope: CSS selector to restrict to a page sub-tree.
    wait: "networkidle", "load", "domcontentloaded", a number of seconds (e.g. "3"), or a CSS
        selector (waits for visibility, 10s timeout). Also accepted per-action in run_actions.
    timeout: max milliseconds to wait for navigation. Default uses the 10s context default; raise it
        for slow dev servers that compile routes on first hit (e.g. 60000 for a cold Next.js route).
    query: when set, returns vision analysis instead of text summary.
    http_credentials: "user:password" for an HTTP Basic-auth site — Playwright authenticates at the
        context level so the browser's native Sign-in dialog never appears (that dialog can't be
        typed into reliably). Persists for the session; pass again to change it.
    debug_dir: when set, dump inputs/outputs/screenshots to this directory for debugging.
    """
    config.refresh()  # ~/.interact/config.env is the source of truth: pick up live edits per call
    inv = Debug.new_invocation_dir(debug_dir, "navigate")
    Debug.dump_input(inv, {"tool": "navigate", "url": url, "query": query, "scope": scope,
                           "wait": wait, "timeout": timeout, "session": session},
                     vlm._resolved_config(None, "image"))
    mgr = core._sessions.get(session)
    if http_credentials is not None:
        await mgr.apply_http_credentials(http_credentials)  # (#70) auth before the goto
    page = await mgr.get_page()
    await page.goto(url, **({"timeout": timeout} if timeout is not None else {}))
    await capture._wait(page, wait)
    state = await capture._capture(mgr, scope)
    if state.screenshot_base64:
        Debug.save("page", base64.b64decode(state.screenshot_base64), ext="png", invocation_id=inv)
    if query:
        result = _session_response(session, await vlm._analyze(state, query))
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
    target: str | None = None,
    session: str = _DEFAULT_SESSION,
    record: bool = False,
) -> str:
    """Execute a sequence of actions on a browser session or desktop window.

    TARGET — the `target` param picks ONE surface:
    - A web page (the common case): leave `target` unset (or "browser"); actions run on browser
      session "default" (or the named `session`). This is the default for all web automation.
    - A NATIVE desktop app (not a web page — e.g. a terminal, editor, Electron/GTK/Qt window):
      set `target=<window title substring>`. Call list_desktop_windows FIRST to discover titles.
    - The whole desktop: `target="screen"` (all monitors combined) or `target="screen:<index>"`
      for one monitor (list_desktop_windows shows the monitor indexes).
    A desktop `target` and a non-default `session` are mutually exclusive. For a website, leave
    `target` unset.

    TARGETING a click/type_text/hover/drag — any of: `ref` (browser, from get_interactive_elements
    / get_page_state / screenshot — unique, survives re-renders), `element` index (desktop),
    `selector` (CSS), `name`(+`role`) (accessible name), or `x`,`y` coordinates. Use whichever
    fits; a `ref` avoids the "N elements match" ambiguity a bare name/selector can hit.

    Each action needs a 'type' key to select the action model.

    Mutating: click, double_click, type_text, scroll, drag, navigate, evaluate_js, upload_file, key_press, click_element
      - double_click: select a word / fire a dblclick (browser; two clicks don't coalesce).
      - select_text (browser): make a real DOM text selection in an element — for a selection-gated
        control like a Lexical inline toolbar (drag dispatches drag-and-drop, not a selection).
    Observations: screenshot, wait_for, http_request, hover, annotate
    Tab control: new_tab, switch_tab, close_tab
    Viewport: emulate_device — set the session to a device profile (a Playwright `device` name like
      "iPhone 13", or explicit width+height (+ device_scale_factor/is_mobile/has_touch), or
      reset=true) to verify responsive/mobile layouts at true device metrics. Run it before
      navigating; the return value of an evaluate_js step is surfaced JSON-serialised as that step's
      output (use `return <expr>` or an arrow that returns).
    Timing: sleep — a FIXED pause (max 30s). Use ONLY for genuine fixed delays (e.g. an
      animation). To wait on something concrete, do NOT sleep-and-guess: attach `wait` to the
      preceding action, or add a `wait_for` step — both block exactly until the condition holds.
    Comparison: compare — VLM comparison of snapshots from earlier steps (by 1-based index).

    Browser-only actions (navigate, evaluate_js, wait_for, upload_file, new_tab, switch_tab, close_tab, emulate_device, double_click, select_text) error when used with a desktop target.

    Any action can include 'wait' to wait after execution (networkidle, load, domcontentloaded, or a CSS selector — browser only).
    wait_for blocks until a `selector` reaches a state OR a `text` substring appears — prefer it over `sleep` for content/navigation.
    Any action can include 'observe' (a VLM query string) to capture a screenshot after execution and analyze it. The snapshot is stored by step index for later compare actions.

    scope: CSS selector to restrict the final capture to a page sub-tree (browser only).
    wait: after all actions, wait for a condition (browser only).
    query: when set, returns vision analysis of the final state (or, with record, of the recording).
    record: when True, capture one frame per step and have a video model read them in order — so
        it understands what each action produced (the flow), not just the end state. Works on
        browser and desktop targets, on the live session (no context reset). Cost is bounded: the
        frames are sampled to config.video_max_frames, so a short interaction keeps every step and
        a long one is evenly down-sampled.
    debug_dir: when set, dump inputs/outputs/screenshots to this directory for debugging.
    """
    config.refresh()  # source of truth before we snapshot the resolved config
    inv = Debug.new_invocation_dir(debug_dir, _DBG_ACTIONS)
    Debug.dump_input(inv, {"tool": "run_actions", "actions": [a.model_dump() for a in actions],
                           "query": query, "scope": scope, "wait": wait, "target": target,
                           "session": session}, vlm._resolved_config(None, "component"))
    win, mgr, err = targets._resolve_target(target, session)
    if err:
        Debug.dump_output(inv, err)
        return err
    # When recording, capture a frame per step and let the video model read the sequence; the
    # action run itself returns its normal step report (so the query goes to the frames, not the
    # final state, avoiding a duplicate analysis).
    frames: list[bytes] | None = [] if record else None
    dispatch_query = None if record else query
    if win:
        result = await _run_actions_desktop(
            win, actions, dispatch_query, invocation_id=inv, record_frames=frames
        )
    else:
        result = await _run_actions_browser(
            mgr, actions, dispatch_query, scope, wait, session, invocation_id=inv, record_frames=frames
        )
    if frames:
        result += await vlm._analyze_interaction_frames(frames, query)
    Debug.dump_output(inv, result)
    return result


@mcp.tool()
async def get_page_state(scope: str | None = None, session: str = _DEFAULT_SESSION) -> str:
    """Get current page URL, title, accessibility tree, focused element, visible text, and the
    page's interactive elements as a numbered `ref` list — so you can act by `ref` in run_actions
    immediately, no separate get_interactive_elements call needed. Refs come from a pure DOM scan
    (no VLM, works with any model). scope: CSS selector to restrict to a page sub-tree."""
    config.refresh()
    mgr = core._sessions.get(session)
    state = await capture._capture(mgr, scope)
    elements = await capture._scan_elements(mgr, scope=scope)
    refs = (
        f"Interactive elements (act by ref in run_actions):\n{format_element_list(elements)}"
        if elements
        else "Interactive elements: none detected"
    )
    return _session_response(
        session,
        f"URL: {state.url}\n"
        f"Title: {state.title}\n"
        f"Focused: {state.focused_element}\n\n"
        f"Accessibility Tree:\n{state.accessibility_tree}\n\n"
        f"Visible Text:\n{state.visible_text}\n\n"
        f"{refs}",
    )


@mcp.tool()
async def session(
    action: Literal["list", "save", "load", "close"],
    name: str = _DEFAULT_SESSION,
    path: str | None = None,
) -> str:
    """Manage browser sessions — one tool for the whole lifecycle.

    action:
      - "list"  — active sessions + how long each has been idle (ignores name/path).
      - "save"  — export `name`'s cookies + localStorage to `path` (path required).
      - "load"  — restore `name` from a previously saved `path` (path required).
      - "close" — close `name` and free its browser/resources.

    name: the session to act on (default "default"). path: the session-state file for save/load.
    """
    if action == "list":
        sessions = core._sessions.active()
        if not sessions:
            return "No active sessions."
        lines = []
        for s in sessions:
            idle = core._sessions.idle_seconds(s)
            lines.append(f"  {s}" + (f" — idle {idle:.0f}s" if idle is not None else " — no browser open"))
        ttl = config.session_idle_ttl
        if ttl > 0:
            lines.append(f"(idle sessions auto-close after {ttl}s; set INTERACT_SESSION_IDLE_TTL=0 to disable)")
        return "\n".join(lines)
    if action == "close":
        await core._sessions.close(name)
        return _session_response(name, f"Session '{name}' closed.")
    if not path:
        return f"ERROR: action={action!r} requires `path` (the session-state file)"
    mgr = core._sessions.get(name)
    if action == "save":
        state = await mgr.save_state()
        Path(path).write_text(json.dumps(state))
        return _session_response(name, f"Session '{name}' saved to {path}.")
    state = json.loads(Path(path).read_text())
    await mgr.load_state(state)
    return _session_response(name, f"Session '{name}' restored from {path}.")


@mcp.tool()
async def download_asset(url: str, path: str, session: str = _DEFAULT_SESSION) -> str:
    """Download a URL to a local file path. Uses the browser session's cookies for authenticated downloads."""
    mgr = core._sessions.get(session)
    page = await mgr.get_page()
    response = await page.context.request.get(url)
    data = await response.body()
    core._save_to_path(path, data)
    return _session_response(session, f"Downloaded {len(data)} bytes to {path}")


@mcp.tool()
async def get_logs(
    source: Literal["network", "console"],
    clear: bool = False,
    limit: int = DEFAULT_LIMIT,
    session: str = _DEFAULT_SESSION,
) -> str:
    """Return captured browser logs (last `limit` entries). source="network" → requests
    (method/status/url), source="console" → console messages + errors. clear=True flushes after reading."""
    mgr = core._sessions.get(session)
    if source == "network":
        entries = mgr.drain_network_log(clear)[-limit:]
        if not entries:
            return _session_response(session, "No network requests captured.")
        lines = []
        for e in entries:
            status = e.get("status", "pending")
            ctype = e.get("content_type", "")
            lines.append(f"{e['method']} {status} {e['url']}" + (f" ({ctype})" if ctype else ""))
        return _session_response(session, "\n".join(lines))
    entries = mgr.drain_console_log(clear)[-limit:]
    if not entries:
        return _session_response(session, "No console messages captured.")
    lines = [f"[{e['level']}] {e['text']}" for e in entries]
    return _session_response(session, "\n".join(lines))
