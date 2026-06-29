import asyncio
import base64
from contextlib import asynccontextmanager, suppress
import json
import os
import re
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import litellm as _litellm

_log = logging.getLogger("interact")

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from playwright.async_api import Page

from interact import desktop
from interact.atspi import AtSpi
from interact.actions import AnyAction
from interact.browser import BrowserManager, SessionRegistry
from interact.config import DEFAULT_LIMIT, QUALITY_TIERS
from interact.critique import (
    UIReview,
    VerifyReport,
    build_review_prompt,
    build_verify_prompt,
    format_grounding,
    format_review,
    format_verify,
    parse_review,
    parse_verify,
)
from interact.debug_utils import Debug
from interact.desktop import DesktopElement, DesktopWindow
from interact.detect import (
    _crop_image,
    _desktop_context,
    _detect_desktop_elements,
)
from interact.measure import format_measure, measure
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
from interact.models import is_audio_model, is_transcription_only_model
from interact.vision import (
    MediaItem,
    VLMResult,
    _UNSET,
    _Unset,
    analyze_media,
    analyze_screenshot,
    transcribe_audio,
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


def _desktop_unsupported(is_screen: bool = False) -> str | None:
    """``"ERROR: …"`` when the requested desktop target isn't available on this OS; ``None`` when it
    is. On Linux everything works. Off Linux (macOS/Windows) the cross-platform PortableBackend
    drives the whole screen, so a ``screen`` target works — but window-title targets (no window
    enumeration yet) and the nested Xephyr sandbox (Linux-only) don't, so those get one clear
    actionable message steering to ``target="screen"`` or the browser tools (#24)."""
    from interact.desktop_backend import desktop_supported

    if desktop_supported() or is_screen:
        return None
    import platform as _pf

    return (
        f"ERROR: on {_pf.system()} only target=\"screen\" desktop automation is available (the "
        "portable mss/pynput backend drives the whole screen); window-title targets and the nested "
        "sandbox (launch_app) are Linux-only. Browser automation works fully — omit `target`. "
        "Track native per-window macOS/Windows support: "
        "https://github.com/AlanBlanchet/interact/issues/24"
    )
_DBG_ELEMENTS = "get_interactive_elements"
_DBG_ACTIONS = "run_actions"


_MAX_FALLBACKS = 3


def _effective_model(model_override: str | None, role: str) -> str:
    """The model id that will actually run for a role — delegates to the one resolution site
    (:meth:`Config.resolve_model`) so the resolved dump matches what the VLM path runs."""
    return config.resolve_model(role, model_override or "")


def _resolved_config(model_override: str | None, role: str) -> dict:
    """The full effective config for tool_input_resolved.json, with the per-call effective model
    surfaced so the resolved dump reflects what actually ran (an override or the auto default) —
    not just the empty configured field."""
    resolved = config.model_dump(mode="json")
    resolved["effective_model"] = _effective_model(model_override, role)
    resolved["effective_model_role"] = role
    return resolved


async def _vlm(
    data: bytes,
    context: str,
    query: str | None = None,
    media_type: str = "image",
    mime: str = "image/png",
    max_tokens: int | None | _Unset = _UNSET,
    response_format: type | dict | None = None,
    model_override: str | None = None,
    extra_images: list[bytes] | None = None,
) -> VLMResult:
    import asyncio

    item_type = "video" if media_type == "video" else "image"
    routing = media_type or "image"
    # extra_images ride alongside the primary frame in ONE call (e.g. a reference + the build, for a
    # divergence review) — judging two images together is what stops the isolation-against-a-generic-
    # ideal false PASSes seen in real usage.
    media = [MediaItem.from_bytes(data, item_type, mime)]
    media += [MediaItem.from_bytes(b, "image", mime) for b in (extra_images or [])]
    # Resolve ONCE, at this boundary, to a concrete id — then both the primary call and the
    # fallback chain run against real models. The old code resolved only for the fallback path
    # and handed the raw (often None) override to the primary call, so auto-mode vision always
    # hit analyze_media's empty-model branch → "[Vision not configured]" (39 real failures).
    effective_model = config.resolve_model(routing, model_override or "", breaker)

    async def _call(model_id: str) -> VLMResult:
        return await analyze_media(
            media,
            context,
            config,
            query,
            max_tokens=max_tokens,
            response_format=response_format,
            model=model_id,
        )

    try:
        return await _call(effective_model)
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
        r = await analyze_media(
            media, context, config, query, model=config.resolve_model("image")
        )
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
    try:
        if not query:
            return None
        r = await _vlm(
            data, context, query, media_type, mime, model_override=model_override
        )
        return _fmt_timing(r)
    finally:
        # Save AFTER the (slow) VLM call, in a finally — so the file on disk is exactly the frame
        # that was analyzed/returned, and is still written even if the VLM errors (#17).
        if path:
            _save_to_path(path, data)


_sandbox: "object | None" = None  # the headless NestedBackend, created on first launch_app


# Named display shapes for launch_app(device=...). A phone/tablet app laid out for portrait looks
# wrong in the default 1280x800 desktop display; these give it a correctly-shaped screen so it
# renders as it would on the device. Values are common Flutter logical sizes.
_DEVICE_SIZES = {
    "phone": "412x915",
    "tablet": "820x1180",
    "desktop": "1280x800",
}
_SIZE_RE = re.compile(r"^\d{2,5}x\d{2,5}$")


def _resolve_nested_size(size: str | None, device: str | None) -> tuple[str | None, str | None]:
    """Pick the nested display size for a launch: explicit ``size`` ("WxH") wins, then a ``device``
    profile, else None → the caller keeps the configured default. Returns (size_or_None, error)."""
    if size:
        norm = size.strip().lower()
        if not _SIZE_RE.match(norm):
            return None, f"ERROR: size must be WxH (e.g. 412x915), got {size!r}"
        return norm, None
    if device:
        key = device.strip().lower()
        if key not in _DEVICE_SIZES:
            opts = ", ".join(_DEVICE_SIZES)
            return None, f"ERROR: unknown device {device!r} — use one of: {opts}, or pass size=WxH"
        return _DEVICE_SIZES[key], None
    return None, None


def _get_sandbox(size: str | None = None):
    """The server-owned isolated display (Xephyr if a display is present, else headless Xvfb).
    Created on first use so a window the user moved/buried — or a GPU app that won't screen-grab on
    the real desktop — can be driven in a clean, occlusion-proof, non-intrusive sandbox.

    ``size`` ("WxH") picks the display resolution: a phone app needs a phone-shaped screen, not the
    1280x800 default. The sandbox is a singleton, so a launch that asks for a *different explicit*
    size than the running one transparently respawns it at that size (the first app's size no longer
    wins forever). ``size=None`` means "attach to whatever is already running" — every capture/attach
    tool (screenshot, run_actions, get_interactive_elements, …) passes None, and must NEVER resize a
    live sandbox: collapsing None to the default once respawned a phone (412x915) sandbox at the
    1280x800 default on the first screenshot, so the launched window opened portrait, closed, and
    reopened landscape (empty). None only picks the default when creating a sandbox from cold.

    A long session can exhaust or kill the nested X server (e.g. dozens of leaked GPU apps); the
    cached backend would then reject every launch until restarted. So a dead sandbox is torn down
    and respawned transparently here — the agent never has to manually reset it (#10)."""
    global _sandbox
    if _sandbox is not None:
        if not _sandbox.is_alive():
            _close_sandbox()  # dead/hung X server → respawn (size-independent self-heal, #10)
        elif size is not None and _sandbox.size != size:
            _close_sandbox()  # an EXPLICIT new size (launch_app) → respawn at it
    if _sandbox is None:
        from interact.desktop_backend import NestedBackend

        _sandbox = NestedBackend(
            config.nested_display, size or config.nested_size, headless=config.nested_headless
        )
    return _sandbox


def _close_sandbox() -> None:
    global _sandbox
    if _sandbox is not None:
        try:
            _sandbox.close()
        finally:
            _sandbox = None


_portable: "object | None" = None  # the macOS/Windows real-desktop backend (mss + pynput)


def _get_portable():
    """The cross-platform real-desktop backend used for ``target="screen"`` on macOS/Windows,
    created on first use (verified on real mac/win CI runners, #24)."""
    global _portable
    if _portable is None:
        from interact.desktop_backend import PortableBackend

        _portable = PortableBackend()
    return _portable


def _resolve_portable_screen() -> DesktopWindow:
    """A whole-screen DesktopWindow bound to the portable backend — capture (mss) + input (pynput)
    route through it, so ``target="screen"`` drives the real macOS/Windows desktop (#24)."""
    from interact.desktop import _SCREEN_WID

    pb = _get_portable()
    win = DesktopWindow(name="screen", wid=_SCREEN_WID, x=0, y=0, w=pb.screen_w, h=pb.screen_h)
    win._backend = pb
    return win


def _resolve_nested_target(spec: str) -> tuple[DesktopWindow | None, None, str | None]:
    """Resolve target="nested" (whole sandbox screen) or "nested:<title>" (one sandbox window)."""
    try:
        backend = _get_sandbox()
    except RuntimeError as e:  # nested server (Xephyr/Xvfb) not installed
        return None, None, f"ERROR: sandbox unavailable — {e}"
    title = spec.split(":", 1)[1].strip() if ":" in spec else ""
    if not title:  # whole nested screen
        win = DesktopWindow(name="sandbox", wid=0, x=0, y=0, w=backend.screen_w, h=backend.screen_h)
        win._backend = backend
        return win, None, None
    win = DesktopWindow.find_in(backend, title)
    if win is None:
        windows = backend.list_windows()
        if windows:
            avail = "\n".join(f'  target="nested:{n}"' for _, n in windows)
            return None, None, f"No sandbox window titled '{title}'. In the sandbox:\n{avail}"
        # Empty sandbox. The old "(none — launch_app first)" misled an agent that had JUST launched —
        # the real cause is the display being respawned (a size change pre-#50/#53, or exhaustion
        # after many GPU launches) and dropping the app. Steer recovery INSIDE the sandbox and forbid
        # the real-desktop fallback: a real session bailed to DISPLAY=:0 xdotool/import on the user's
        # actual desktop, which is exactly what the isolated sandbox exists to avoid.
        return None, None, (
            f"No sandbox window titled '{title}' — the sandbox has no windows right now. "
            f"If you just called launch_app, the display was respawned and dropped the app: call "
            f"launch_app again (or reset_sandbox for a clean display), then retry. Do NOT drive the "
            f"real desktop (xdotool / import / DISPLAY=:0) — keep everything in the isolated sandbox."
        )
    return win, None, None


def _find_desktop_window(title: str) -> DesktopWindow | str:
    windows = DesktopWindow.all()
    if not windows:
        return _NO_WINDOWS_MSG
    t = title.strip()
    if t.lower().startswith("wid:"):  # exact, unambiguous targeting by window id (#5)
        raw = t[4:].strip()
        try:
            wid = int(raw, 0)  # accepts decimal or 0x-hex (as xwininfo prints)
        except ValueError:
            return f"Invalid window id '{raw}' — use the wid shown by list_desktop_windows."
        match = next((w for w in windows if w.wid == wid), None)
        return match or f"No window with wid {raw}. Available:\n{DesktopWindow.listing(windows)}"
    matches = DesktopWindow.matching(t, windows)
    if not matches:
        return f"No window matching '{title}'. Available:\n{DesktopWindow.listing(windows)}"
    # An exact title (sorted first by matching()) or a sole partial match is unambiguous; several
    # partial matches with no exact one would be a silent guess — make the agent pick (#1.3).
    hint = title.strip().lower()
    if len(matches) == 1 or any(w.name.lower() == hint for w in matches):
        return matches[0]
    return (
        f"'{title}' matches {len(matches)} windows — pass a more specific or the exact title:\n"
        f"{DesktopWindow.listing(matches)}"
    )


def _resolve_target(
    target: str | None,
    session: str,
) -> tuple[DesktopWindow | None, BrowserManager | None, str | None]:
    """Resolve the one `target` param to a surface. ``None``/``"browser"`` → the browser session
    named by `session` (the default). Any other string → a desktop window matched by title.
    Unifies the old `window`/`session` split into a single "what am I driving?" choice."""
    config.refresh()  # ~/.interact/config.env is the source of truth: pick up live edits per call
    is_desktop = bool(target) and target.strip().lower() != "browser"
    if is_desktop and session != _DEFAULT_SESSION:
        return None, None, "Cannot combine a desktop `target` with a browser `session`"
    if is_desktop:
        t = target.strip()
        is_screen = t.lower() == "screen" or t.lower().startswith("screen:")
        if unsupported := _desktop_unsupported(is_screen):
            return None, None, unsupported
        from interact.desktop_backend import desktop_supported

        if is_screen and not desktop_supported():
            return _resolve_portable_screen(), None, None  # macOS/Windows whole-screen
        if t.lower() == "nested" or t.lower().startswith("nested:"):
            return _resolve_nested_target(t)
        if is_screen:
            result = DesktopWindow.screen(t)
        else:
            result = _find_desktop_window(t)
        if isinstance(result, str):
            return None, None, result
        return result, None, None
    return None, _sessions.get(session), None


def _save_to_path(path: str, data: bytes):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


_AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".webm": "audio/webm", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".mpeg": "audio/mpeg", ".mpga": "audio/mpeg",
}


def _audio_mime(path: str) -> str:
    """MIME for an audio/media file, by extension (default mp3)."""
    return _AUDIO_MIME.get(Path(path).suffix.lower(), "audio/mpeg")


def _resolve_image_source(target: str | None) -> tuple[bytes | None, str | None]:
    """A ``target="file:<path>"`` reads an EXISTING image file instead of capturing — so
    screenshot/review_ui/measure_ui can judge an artifact produced out-of-band (a saved capture, a
    script's output) without the capture clobbering it (#44). Returns ``(bytes, None)`` for a file
    target, ``(None, "ERROR: …")`` if it can't be read, or ``(None, None)`` for a normal target."""
    if not (target and target.strip().lower().startswith("file:")):
        return None, None
    p = target.strip()[5:]
    if p.startswith("//"):  # tolerate file:// and file:/// URL forms
        p = p[2:]
    try:
        return Path(p).read_bytes(), None
    except OSError as e:
        return None, f"ERROR: could not read image file {p!r} — {e}"


def _parse_int_tuple(s: str | None, n: int, name: str):
    """Parse an "a,b,…" string into an ``n``-int tuple. Returns the tuple, ``None`` if unset, or an
    ``"ERROR: …"`` string if malformed (the caller returns that straight to the agent)."""
    if not s:
        return None
    try:
        vals = tuple(int(p.strip()) for p in s.split(",") if p.strip())
    except ValueError:
        return f"ERROR: {name} must be {n} integers like '{','.join(['0'] * n)}', got {s!r}"
    if len(vals) != n:
        return f"ERROR: {name} needs {n} integers (got {len(vals)}: {s!r})"
    return vals


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


def _reap_sandbox() -> None:
    """Drop a nested sandbox whose X server has died (``is_alive`` polls it, reaping the zombie),
    so no ``<defunct>`` Xephyr lingers and the display frees up for a clean respawn on next use."""
    if _sandbox is not None and not _sandbox.is_alive():
        _close_sandbox()


async def _idle_session_reaper(ttl: int) -> None:
    """Periodically auto-close browser sessions idle beyond ``ttl`` so a long-lived MCP server (one
    per open editor window) doesn't accumulate idle Chromium instances — a left-open page can spin
    CPU for hours. Also reaps a dead sandbox. ``ttl`` <= 0 disables the loop entirely."""
    if ttl <= 0:
        return
    interval = min(60, ttl)
    while True:
        await asyncio.sleep(interval)
        try:
            closed = await _sessions.close_idle(ttl)
            if closed:
                _log.info("auto-closed idle browser session(s): %s", ", ".join(closed))
            _reap_sandbox()
        except Exception:  # a transient error must never kill the reaper
            _log.exception("idle session reaper error")


@asynccontextmanager
async def _lifespan(_: FastMCP) -> AsyncIterator[None]:
    from interact.server_registry import register_server, unregister_server

    reg = register_server()  # record pid+version so `interact doctor` can flag a stale long-lived server
    reaper = asyncio.create_task(_idle_session_reaper(config.session_idle_ttl))
    try:
        yield
    finally:
        reaper.cancel()
        with suppress(asyncio.CancelledError):
            await reaper
        await _sessions.close_all()
        _close_sandbox()
        unregister_server(reg)


def _instructions() -> str:
    from interact import __version__

    return (
        f"interact v{__version__} — drives a browser and desktop windows by vision/refs over MCP. "
        "Act by `ref` (from get_interactive_elements / get_page_state / screenshot), a CSS `selector`, "
        "accessible `name`, or `x,y` — whichever fits. "
        "Browser automation works on Linux, macOS and Windows; native desktop automation "
        "(launch_app, target=<window>/screen/nested) is Linux-only today — off Linux those tools "
        "return a clear message and you should use the browser target instead. "
        "DESKTOP: to drive a native app, use interact's own tools — never shell out to xdotool/wmctrl. "
        "For an app that fights the window manager, runs in the background, or is GPU-rendered and "
        "screen-grabs black (Flutter/Electron/games/emulators), launch it with `launch_app(\"<cmd>\")` "
        "— just the binary/command, no env tricks needed (the sandbox forces software GL itself so a "
        "GPU app renders instead of capturing black) — and drive it via `target=\"nested:<title>\"`, an "
        "isolated, occlusion-proof display. If `launch_app` isn't in your tool list, your interact "
        "server is out of date: ask the user to reconnect/restart the interact MCP server to load it "
        "(don't fall back to raw shell automation). "
        "If sandbox launches start failing (e.g. rc=1 for every app after many launches), the display "
        "is respawned automatically on the next `launch_app`, or call `reset_sandbox` to force a clean "
        "one — keep using the sandbox, don't switch to driving the real desktop. "
        "If interact itself errors in a way that blocks you, behaves unexpectedly, or is missing a "
        "capability you needed, call `report_issue` — it sends the problem to interact's maintainers so "
        "it gets fixed. That's the channel for feedback about the tool (not about the site you automate). "
        "If its result says a prefilled issue page was opened in the browser, tell the user to press "
        "Submit there; never copy report files into repos."
    )


mcp = FastMCP("interact", lifespan=_lifespan, instructions=_instructions())


async def _capture(mgr: BrowserManager, scope: str | None = None, tab: int | None = None):
    page = await mgr.get_page(tab)  # tab=None → the session's active tab (#30)
    state = await PageState.capture(page, scope=scope)
    return state


async def _scan_elements(
    mgr: BrowserManager,
    tab: int | None = None,
    scope: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[InteractiveElement]:
    """The DOM scan behind every browser ref — pure ``page.evaluate`` over the page's own DOM,
    NO VLM. It sets ``data-interact-ref`` attributes and returns the elements, so refs are
    model-agnostic (they work with any configured model, or none) and free to surface widely.
    The element map is registered so a following run_actions can act by these refs."""
    page = await mgr.get_page(tab)
    result = await page.evaluate(
        _ANNOTATE_JS, {"scope": scope, "limit": limit, "nextRef": mgr._ref_counter}
    )
    mgr._ref_counter = result["nextRef"]  # advance the session's monotonic ref counter (#35)
    elements = [
        InteractiveElement(
            index=int(raw["ref"][1:]),  # ref "eN" ↔ index N, both stable across scans in a session
            ref=raw["ref"],
            role=raw["tag"],
            name=raw["name"],
            x=raw["x"],
            y=raw["y"],
            width=raw["width"],
            height=raw["height"],
        )
        for raw in result["elements"]
    ]
    mgr.set_element_map(tab, elements)
    return elements


async def _annotate_page(
    mgr: BrowserManager,
    tab: int = 0,
    scope: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[bytes, list[InteractiveElement]]:
    elements = await _scan_elements(mgr, tab, scope, limit)
    page = await mgr.get_page(tab)
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
            model=model_override,
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
    timeout: float | None = None,
    debug_dir: str | None = None,
    session: str = _DEFAULT_SESSION,
) -> str:
    """Navigate to a URL and return page content. Browser-only — requires a session, not a window.

    scope: CSS selector to restrict to a page sub-tree.
    wait: "networkidle", "load", "domcontentloaded", or a CSS selector (waits for visibility, 10s timeout).
    timeout: max milliseconds to wait for navigation. Default uses the 10s context default; raise it
        for slow dev servers that compile routes on first hit (e.g. 60000 for a cold Next.js route).
    query: when set, returns vision analysis instead of text summary.
    debug_dir: when set, dump inputs/outputs/screenshots to this directory for debugging.
    """
    config.refresh()  # ~/.interact/config.env is the source of truth: pick up live edits per call
    inv = Debug.new_invocation_dir(debug_dir, "navigate")
    Debug.dump_input(inv, {"tool": "navigate", "url": url, "query": query, "scope": scope,
                           "wait": wait, "timeout": timeout, "session": session},
                     _resolved_config(None, "image"))
    mgr = _sessions.get(session)
    page = await mgr.get_page()
    await page.goto(url, **({"timeout": timeout} if timeout is not None else {}))
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


async def _analyze_interaction_frames(frames: list[bytes], query: str | None) -> str:
    """Analyse the per-step frames of an interaction as an ordered sequence, so a model sees what
    each action produced — not just the end state. One frame per step is captured during the run;
    here it's sampled down to config.video_max_frames (evenly) to bound cost, then sent to the
    video model with the query."""
    from interact.vision import evenly_sampled

    sampled = evenly_sampled(frames, config.video_max_frames)
    media = [MediaItem.from_bytes(f, "image", "image/png") for f in sampled]
    context = (
        f"{len(sampled)} screenshots captured in order during an interaction — each is the page/"
        "window state right after one step. Read them as a sequence to see what happened."
    )
    r = await analyze_media(
        media,
        context,
        config,
        query or "Describe what happened across these frames, step by step.",
        model=config.resolve_model("video"),
    )
    return f"\n\n[recording: {len(sampled)} frames] {_fmt_timing(r)}"


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
                           "session": session}, _resolved_config(None, "component"))
    win, mgr, err = _resolve_target(target, session)
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
        result += await _analyze_interaction_frames(frames, query)
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
    target: str | None = None,
    session: str = _DEFAULT_SESSION,
    model: str | None = None,
):
    """Capture the current page or a desktop window.

    Default (target unset): operates on browser session "default".
    target=<window title>: captures a desktop window. target="screen"/"screen:<index>": the whole
    desktop or one monitor (use list_desktop_windows to discover windows + monitor indexes).
    target="file:<path>": ANALYZE an existing image file (with query) instead of capturing — for an
    artifact produced out-of-band; this never writes, so it can't clobber the file.
    A desktop target and a non-default session are mutually exclusive.

    Returns depend on parameters:
    - No selector/element, no query: page title + visible text content (browser) or, for a
      desktop window, already-detected interactive elements as a numbered ref list if any exist
      (else metadata + a pointer to get_interactive_elements). screenshot never runs VLM grounding.
    - No selector/element, with query: full screenshot analyzed by VLM.
    - With selector/element, no query: element metadata (browser only).
    - With selector/element, with query: cropped element screenshot analyzed by VLM (browser only).

    element: integer index from get_interactive_elements (priority over selector).
    selector: CSS selector targeting one element (browser only).
    query: question for VLM visual analysis of the captured content.
    scope: CSS selector to restrict text extraction to a sub-tree (browser only).
    path: OUTPUT sink — saves the captured PNG here (overwrites any existing file, and says so). To
        ANALYZE an existing image, use target="file:<path>", not path.
    return_image: when True, return the raw screenshot bytes as an MCP ImageContent alongside the text,
        so the calling agent can SEE the pixels directly (not just a VLM summary).
    model: override the configured VLM model for this call. Uses the VS Code configured model when not set.
    """
    config.refresh()  # source of truth before we snapshot the resolved config
    inv = Debug.new_invocation_dir(debug_dir, "screenshot")
    Debug.dump_input(inv, {"tool": "screenshot", "query": query, "scope": scope, "selector": selector,
                           "element": element, "target": target, "session": session, "model": model},
                     _resolved_config(model, "image"))
    # target="file:<path>" analyzes an EXISTING image instead of capturing (no clobber, #44).
    file_bytes, ferr = _resolve_image_source(target)
    if ferr:
        Debug.dump_output(inv, ferr)
        return ferr
    if file_bytes is not None:
        src = target.strip()[5:]
        label = f"Image file: {src}"
        if query:
            r = await _vlm(file_bytes, label, query, model_override=model)
            text = f"{label}\n{_fmt_timing(r)}"
        else:
            import io as _io
            from PIL import Image as _PILImage
            w, h = _PILImage.open(_io.BytesIO(file_bytes)).size
            text = f"{label} ({w}x{h}) — pass query=… to analyze it, or use measure_ui for exact pixels."
        Debug.save("capture", file_bytes, ext="png", invocation_id=inv)
        out = [text, Image(data=file_bytes, format="png")] if return_image else text
        Debug.dump_output(inv, out)
        return out
    win, mgr, err = _resolve_target(target, session)
    if err:
        Debug.dump_output(inv, err)
        return err
    # If `path` already exists we're about to OVERWRITE it with this capture — surface that so the
    # result can't be mistaken for an analysis of the prior file (#44). To analyze a file, use
    # target="file:<path>" above; `path` is an OUTPUT sink.
    overwrote_path = bool(path) and Path(path).exists()
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
        elif query:
            img_bytes, description = await _capture_desktop(
                win, query, path, model_override=model
            )
            text = f"{_desktop_label(win)}\n{description}"
        else:
            # No query → just capture. screenshot NEVER runs VLM grounding (that's
            # get_interactive_elements' job, and a VLM call here would be slow + wrong). If a
            # detection already exists for this window, surface those refs so the capture is
            # actionable; otherwise return metadata and point the agent at the detect tool.
            img_bytes = win.capture()
            if path:
                _save_to_path(path, img_bytes)
            # Surface cached refs ONLY if they belong to the frame just captured — after a navigation
            # the live frame's signature differs, so we don't list a prior screen's refs on a screen
            # that's no longer shown (the screenshot↔elements desync, #19).
            from interact.detect import _page_signature
            cached = DesktopElement.cached_for(win.wid, _page_signature(img_bytes))
            if cached:
                text = f"{_desktop_label(win)}\n{DesktopElement.format_list(cached)}"
            else:
                text = (
                    f"{_desktop_label(win)}\n{_desktop_context(win)}\n"
                    "(call get_interactive_elements to detect clickable elements and act by [ref])"
                )
    elif element is not None or selector is not None:
        text = _session_response(
            session, await _element_screenshot(mgr, mgr.active_tab, selector, element, query, path)
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
            # No query → no VLM. Surface the page's refs (pure DOM scan) so the capture is
            # actionable: the agent can click/type by `ref` without a follow-up detect call.
            elements = await _scan_elements(mgr, scope=scope)
            refs = (
                f"\n\nInteractive elements (act by ref in run_actions):\n"
                f"{format_element_list(elements)}"
                if elements
                else ""
            )
            text = _session_response(session, state.text_summary() + refs)
    if overwrote_path:
        text += f"\n(note: overwrote existing file {path} with this capture)"
    if img_bytes is not None:
        Debug.save("capture", img_bytes, ext="png", invocation_id=inv)
    result = [text, Image(data=img_bytes, format="png")] if (return_image and img_bytes is not None) else text
    Debug.dump_output(inv, result)
    return result


async def _capture_target_png(win: DesktopWindow | None, mgr, scope: str | None) -> bytes:
    """PNG bytes for a resolved target — a desktop window/screen (its own capture) or the browser page."""
    if win:
        return win.capture()
    state = await _capture(mgr, scope)
    return base64.b64decode(state.screenshot_base64)


async def _resolve_capture(target, session, scope, path, reference, inv):
    """Shared capture path for review_ui / verify_ui: a ``file:<path>`` target or a live capture,
    saved to ``path`` if given, plus an optional ``reference`` image. Returns
    ``(img_bytes, context, ref_bytes, elements, err_or_None)`` — on error the caller returns the string.

    ``elements`` is interact's detected element list for a BROWSER target (the reliable, no-VLM DOM-ref
    scan), used to GROUND the critique and flag a hallucinated ref. Empty for a desktop/file target,
    where no equally-reliable list exists."""
    file_bytes, ferr = _resolve_image_source(target)  # target="file:<path>" → judge a saved image (#44)
    if ferr:
        return None, None, None, [], ferr
    elements: list = []
    if file_bytes is not None:
        img, context = file_bytes, f"Image file: {target.strip()[5:]}"
    else:
        win, mgr, err = _resolve_target(target, session)
        if err:
            return None, None, None, [], err
        img = await _capture_target_png(win, mgr, scope)
        context = _desktop_label(win) if win else "Browser page"
        if win is None and mgr is not None:  # browser target → DOM ref list to anchor the critique on
            try:
                elements = await _scan_elements(mgr, scope=scope)
            except Exception:
                elements = []  # never fail a capture because the grounding scan hiccuped
    if path:
        _save_to_path(path, img)
    Debug.save("capture", img, ext="png", invocation_id=inv)
    ref_bytes = None
    if reference:
        try:
            ref_bytes = Path(reference).read_bytes()
        except OSError as e:
            return None, None, None, [], f"ERROR: could not read reference image {reference!r} — {e}"
    return img, context, ref_bytes, elements, None


def _quality_plan(quality: str | None, model: str | None) -> tuple[str | None, bool, str | None]:
    """Resolve a quality tier to ``(effective_model, strict, error)``. An explicit ``model`` wins over
    the tier's model. ``strict`` (critical only) drops a finding citing a ref the scan never detected —
    a deterministic precision boost for a pre-ship sign-off. ``error`` is set on an unknown tier."""
    if quality is None:
        return model or None, False, None
    q = quality.strip().lower()
    if q not in QUALITY_TIERS:
        return None, False, f"ERROR: quality must be one of {', '.join(QUALITY_TIERS)} (got {quality!r})"
    return (model or config.resolve_quality_model(q) or None), q == "critical", None


@mcp.tool()
async def review_ui(
    focus: str | None = None,
    reference: str | None = None,
    target: str | None = None,
    session: str = _DEFAULT_SESSION,
    scope: str | None = None,
    path: str | None = None,
    model: str | None = None,
    quality: str | None = None,
) -> str:
    """Capture the UI and return a STRUCTURED critique of what's WRONG with it — low-contrast or
    unreadable text, overflow/clipping, truncation, misalignment, broken/empty/error states, black or
    occluded regions, tiny tap targets, off-theme colors — so you can JUDGE a UI's quality without
    hand-writing a vision prompt. Use it after a change to confirm the result looks right, or to hunt
    defects on any screen.

    Findings come back severity-sorted, one per line as `[critical|major|minor]/<category> location:
    issue → fix`; a clean screen reports no defects. Works on any target like screenshot: unset/
    "browser" = the browser session, a window title = a desktop window, "screen"/"nested[:title]" =
    the sandbox or whole desktop.

    focus: optional extra emphasis (e.g. "the background should be warm sand, not purple"; "check the
        bottom nav isn't black/occluded") — narrows the review WITHOUT replacing the built-in rubric.
    reference: path to a reference/target image (a design or a prior good build). When set, the review
        judges how the capture DIVERGES from this reference (wrong accent, missing nav, layout drift),
        instead of against a generic ideal — the reliable way to catch a build that's subtly off.
    path: save the reviewed PNG here. Requires a configured vision model (same as screenshot's query).
    quality: pick the model by STAKES, not by name — "low"/"medium" use a cheap sovereign self-host
        model, "high"/"critical" the best frontier model; "critical" also drops findings whose element
        interact can't confirm (highest precision, for a pre-ship sign-off). Unset = the configured/
        auto model. An explicit model= still overrides this.
    """
    config.refresh()
    eff_model, strict, qerr = _quality_plan(quality, model)
    if qerr:
        return qerr
    inv = Debug.new_invocation_dir(None, "review_ui")
    Debug.dump_input(inv, {"tool": "review_ui", "focus": focus, "reference": reference,
                           "target": target, "session": session, "model": model, "quality": quality},
                     _resolved_config(eff_model, "image"))
    img, context, ref_bytes, elements, err = await _resolve_capture(target, session, scope, path, reference, inv)
    if err:
        Debug.dump_output(inv, err)
        return err
    grounding = format_grounding(elements) if elements else None  # anchor findings to the real elements
    valid_refs = {e.ref for e in elements if getattr(e, "ref", None)} or None
    try:
        if ref_bytes is not None:  # reference first, build second — matches the compare rubric
            r = await _vlm(ref_bytes, context, build_review_prompt(focus, compare=True, grounding=grounding),
                           response_format=UIReview, model_override=eff_model, extra_images=[img])
        else:
            r = await _vlm(img, context, build_review_prompt(focus, grounding=grounding),
                           response_format=UIReview, model_override=eff_model)
    except Exception as e:  # never crash the agent's flow on a vision hiccup
        return f"ERROR: review_ui vision call failed — {e}"
    review = parse_review(r.text)
    if review and strict and valid_refs is not None:  # critical: drop a finding citing a phantom ref
        review.findings = [f for f in review.findings if not (f.ref and f.ref not in valid_refs)]
    body = format_review(review, valid_refs) if review else r.text  # graceful: raw VLM text if the schema didn't parse
    model_tag = f" {r.model}" if r.model else ""
    out = f"{context}\n{body}\n(VLM:{model_tag} {r.elapsed:.1f}s)"
    Debug.dump_output(inv, out)
    return out


@mcp.tool()
async def verify_ui(
    requirements: list[str],
    target: str | None = None,
    reference: str | None = None,
    focus: str | None = None,
    session: str = _DEFAULT_SESSION,
    scope: str | None = None,
    path: str | None = None,
    model: str | None = None,
    quality: str | None = None,
) -> str:
    """Judge a UI against your LITERAL requirements — one PASS/FAIL per requirement, each anchored to
    the exact element it judged. The acceptance complement to review_ui (which DISCOVERS defects): hand
    it the checklist a freeform critique glosses ("the coin pill shows a GOLD coin, not a flame"; "the
    bottom nav has exactly 4 tabs"; "the FAB does not overlap the tab bar") and it tests each to the
    letter — presence is not enough, the form/color/count/state must match.

    Captures like review_ui — target unset/"browser" = the page; a window title; "screen";
    "nested[:title]"; or "file:<path>" to verify a saved image. Pass a reference image to judge each
    requirement against a target design. For a hard form-defect, confirm the number with measure_ui.

    requirements: the literal requirements to check, each judged PASS / FAIL / UNCLEAR with evidence.
    focus: optional extra emphasis layered onto the rubric.
    reference: a target/design image to judge the build against.
    quality: pick the model by STAKES — "low"/"medium" use a cheap sovereign model, "high"/"critical"
        the best frontier; "critical" downgrades any PASS resting on an element interact can't confirm.
        Unset = configured/auto. An explicit model= overrides this.
    """
    config.refresh()
    if not requirements:
        return "ERROR: verify_ui needs at least one requirement to check."
    eff_model, strict, qerr = _quality_plan(quality, model)
    if qerr:
        return qerr
    inv = Debug.new_invocation_dir(None, "verify_ui")
    Debug.dump_input(inv, {"tool": "verify_ui", "requirements": requirements, "target": target,
                           "reference": reference, "model": model, "quality": quality},
                     _resolved_config(eff_model, "image"))
    img, context, ref_bytes, elements, err = await _resolve_capture(target, session, scope, path, reference, inv)
    if err:
        Debug.dump_output(inv, err)
        return err
    grounding = format_grounding(elements) if elements else None  # anchor each check to the real elements
    valid_refs = {e.ref for e in elements if getattr(e, "ref", None)} or None
    try:
        if ref_bytes is not None:  # reference first, build second
            r = await _vlm(ref_bytes, context, build_verify_prompt(requirements, focus, compare=True, grounding=grounding),
                           response_format=VerifyReport, model_override=eff_model, extra_images=[img])
        else:
            r = await _vlm(img, context, build_verify_prompt(requirements, focus, grounding=grounding),
                           response_format=VerifyReport, model_override=eff_model)
    except Exception as e:
        return f"ERROR: verify_ui vision call failed — {e}"
    report = parse_verify(r.text)
    if report and strict and valid_refs is not None:  # critical: a PASS citing a phantom ref can't stand
        for c in report.checks:
            if c.ref and c.ref not in valid_refs and c.verdict == "pass":
                c.verdict = "unclear"
        report.all_pass = all(c.verdict == "pass" for c in report.checks)
    body = format_verify(report, valid_refs) if report else r.text  # graceful: raw VLM text if schema didn't parse
    model_tag = f" {r.model}" if r.model else ""
    out = f"{context}\n{body}\n(VLM:{model_tag} {r.elapsed:.1f}s)"
    Debug.dump_output(inv, out)
    return out


@mcp.tool()
async def measure_ui(
    target: str | None = None,
    region: str | None = None,
    point: str | None = None,
    session: str = _DEFAULT_SESSION,
    scope: str | None = None,
    path: str | None = None,
) -> str:
    """DETERMINISTIC pixel measurement of a UI — exact colors + WCAG contrast, NO VLM (no spend, fully
    reproducible). Use it for a number you can trust instead of a model's guess: the contrast ratio of
    text vs background, the exact color at a point, or the biggest empty band on screen.

    Captures like screenshot/review_ui — target unset/"browser" = the page; a window title; "screen";
    "nested[:title]"; or "file:<path>" to measure an existing image. Then:
    - region="x,y,w,h": dominant colors in that box, the two-color WCAG contrast ratio (PASS/FAIL for
      AA-normal 4.5, AA-large 3.0, AAA 7.0), and the largest uniform band inside it.
    - point="x,y": the exact color (hex) at that pixel.
    - neither: whole-image palette + the largest uniform (empty) band.

    Pairs with review_ui: the VLM flags a suspect ("this text looks low-contrast") → measure_ui
    confirms the actual ratio. Coordinates are image pixels (as screenshot / get_interactive_elements
    report them).
    """
    config.refresh()
    inv = Debug.new_invocation_dir(None, "measure_ui")
    Debug.dump_input(inv, {"tool": "measure_ui", "target": target, "region": region,
                           "point": point, "session": session})
    reg = _parse_int_tuple(region, 4, "region")
    if isinstance(reg, str):
        return reg
    pt = _parse_int_tuple(point, 2, "point")
    if isinstance(pt, str):
        return pt
    file_bytes, ferr = _resolve_image_source(target)  # target="file:<path>" → measure a saved image
    if ferr:
        return ferr
    if file_bytes is not None:
        img, label = file_bytes, f"Image file: {target.strip()[5:]}"
    else:
        win, mgr, err = _resolve_target(target, session)
        if err:
            return err
        img = await _capture_target_png(win, mgr, scope)
        label = _desktop_label(win) if win else "Browser page"
    if path:
        _save_to_path(path, img)
    try:
        result = measure(img, region=reg, point=pt)
    except Exception as e:
        return f"ERROR: measure_ui failed — {e}"
    out = f"{label}\n{format_measure(result)}"
    Debug.dump_output(inv, out)
    return out


@mcp.tool()
async def get_interactive_elements(
    scope: str | None = None,
    query: str | None = None,
    element: int | None = None,
    limit: int = DEFAULT_LIMIT,
    tab: int | None = None,
    debug_dir: str | None = None,
    target: str | None = None,
    session: str = _DEFAULT_SESSION,
    method: str = "default",
    model: str | None = None,
) -> str:
    """List the interactive elements with numbered badges + their details; act on them by the
    returned `ref`/`element` in run_actions.

    Default (target unset): browser session "default" — sets data-interact-ref attributes via a
    pure DOM scan (no VLM). get_page_state and screenshot return these refs too, so you often
    already have them without a separate call. target=<window title>: VLM-detects elements in a
    desktop window;
    target="screen"/"screen:<index>": VLM-detects across the whole desktop or one monitor.
    A desktop target and a non-default session are mutually exclusive (list_desktop_windows lists them).

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
    config.refresh()  # source of truth before we snapshot the resolved config
    inv = Debug.new_invocation_dir(debug_dir, _DBG_ELEMENTS)
    Debug.dump_input(inv, {"tool": "get_interactive_elements", "query": query, "scope": scope,
                           "element": element, "limit": limit, "tab": tab, "target": target,
                           "session": session, "method": method, "model": model},
                     _resolved_config(model, "component"))
    win, mgr, err = _resolve_target(target, session)
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
    """Get current page URL, title, accessibility tree, focused element, visible text, and the
    page's interactive elements as a numbered `ref` list — so you can act by `ref` in run_actions
    immediately, no separate get_interactive_elements call needed. Refs come from a pure DOM scan
    (no VLM, works with any model). scope: CSS selector to restrict to a page sub-tree."""
    config.refresh()
    mgr = _sessions.get(session)
    state = await _capture(mgr, scope)
    elements = await _scan_elements(mgr, scope=scope)
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
        sessions = _sessions.active()
        if not sessions:
            return "No active sessions."
        lines = []
        for s in sessions:
            idle = _sessions.idle_seconds(s)
            lines.append(f"  {s}" + (f" — idle {idle:.0f}s" if idle is not None else " — no browser open"))
        ttl = config.session_idle_ttl
        if ttl > 0:
            lines.append(f"(idle sessions auto-close after {ttl}s; set INTERACT_SESSION_IDLE_TTL=0 to disable)")
        return "\n".join(lines)
    if action == "close":
        await _sessions.close(name)
        return _session_response(name, f"Session '{name}' closed.")
    if not path:
        return f"ERROR: action={action!r} requires `path` (the session-state file)"
    mgr = _sessions.get(name)
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
    mgr = _sessions.get(session)
    page = await mgr.get_page()
    response = await page.context.request.get(url)
    data = await response.body()
    _save_to_path(path, data)
    return _session_response(session, f"Downloaded {len(data)} bytes to {path}")


@mcp.tool()
async def transcribe(
    path: str,
    query: str | None = None,
    model: str | None = None,
) -> str:
    """Transcribe an audio (or audio-bearing) file to text, and optionally answer a question about it.

    Point it at a local file `path` — a clip you grabbed with download_asset, or a recording you
    saved with record(path=...). Accepts mp3/wav/m4a/webm/ogg/flac and mp4/mov (the audio track is
    used). Returns the transcript; with `query`, returns an answer about the audio instead.

    Audio understanding is acoustic (it HEARS the clip — tone, speakers, music, sound events) when the
    audio model can take audio in chat (Gemini, gpt-4o-audio); with a transcription-only model
    (Whisper, gpt-4o-transcribe) the query is answered over the transcript. Set the model with the
    `audio.model` setting / INTERACT_AUDIO_MODEL, or override per-call with `model`.

    path: local audio/media file to read.
    query: optional question about the audio (omit for a plain transcript).
    model: override the configured audio model for this call.
    """
    config.refresh()
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        return f"ERROR: could not read audio file {path!r} — {e}"
    mime = _audio_mime(path)
    audio_model = config.resolve_model("audio", model or "")
    name = Path(path).name

    # Acoustic understanding when the model can hear the clip directly; otherwise fall through to
    # transcript-based answering below (so Whisper-style transcription-only models still serve a query).
    if query and is_audio_model(audio_model) and not is_transcription_only_model(audio_model):
        try:
            r = await _vlm(data, f"Audio file: {name}", query, "audio", mime, model_override=audio_model)
            return _fmt_timing(r)
        except Exception as e:
            return f"ERROR: audio understanding failed on {audio_model} — {e}"

    try:
        r = await transcribe_audio(data, model=audio_model, mime_type=mime)
    except Exception as e:
        return f"ERROR: transcription failed on {audio_model} — {e}"
    transcript = r.text
    if not query:
        return f"{transcript}\n(transcribed:{(' ' + r.model) if r.model else ''} {r.elapsed:.1f}s)"

    answer = await analyze_media(
        [], f"Transcript of {name}:\n{transcript}", config, query,
        model=config.resolve_model("image"),
    )
    return f"{answer.text}\n\n--- transcript ---\n{transcript}\n(VLM: {answer.model} {answer.elapsed:.1f}s)"


@mcp.tool()
async def get_logs(
    source: Literal["network", "console"],
    clear: bool = False,
    limit: int = DEFAULT_LIMIT,
    session: str = _DEFAULT_SESSION,
) -> str:
    """Return captured browser logs (last `limit` entries). source="network" → requests
    (method/status/url), source="console" → console messages + errors. clear=True flushes after reading."""
    mgr = _sessions.get(session)
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


@mcp.tool()
async def list_desktop_windows() -> str:
    """List desktop targets for the `target` param: each connected monitor (target="screen" for
    the whole desktop, target="screen:<name>" e.g. screen:DP-1, or target="screen:<index>") and
    each open window. Target a window by its title, or — when a title isn't unique — by its id
    shown here as target="wid:<id>" (the unambiguous selector)."""
    from interact.desktop_backend import desktop_supported

    if not desktop_supported():
        # macOS/Windows: the portable backend drives the whole screen; per-window enum is Linux-only.
        pb = _get_portable()
        return (
            f'Screen (the only desktop target on this OS): target="screen" — {pb.screen_w}x'
            f"{pb.screen_h}. Per-window targeting + the launch_app sandbox are Linux-only (#24); "
            "browser automation works fully (omit `target`)."
        )
    monitors = DesktopWindow.monitors()
    windows = DesktopWindow.all()
    if not monitors and not windows:
        return _NO_WINDOWS_MSG
    parts = []
    if monitors:
        # Offer the connector name (DP-1, eDP-1) as the target: indices reorder across sessions /
        # display-manager restarts, the connector is stable (#1.6).
        mon_lines = "\n".join(
            f"  target=\"screen:{m['name']}\" (or screen:{m['index']}) — {m['w']}x{m['h']} at {m['x']},{m['y']}"
            for m in monitors
        )
        parts.append(
            f'Screens (target="screen" = all {len(monitors)} combined; screen:<name> is stable '
            f"across sessions):\n{mon_lines}"
        )
    if windows:
        parts.append(f"Windows (target=<title>):\n{DesktopWindow.listing(windows)}")
    if _sandbox is not None:
        nested = "\n".join(f'  target="nested:{n}"' for _, n in _sandbox.list_windows())
        parts.append(f"Sandbox windows (isolated display; launch_app to add):\n{nested or '  (empty)'}")
    return "\n\n".join(parts)


def _flutter_software_render(argv: list[str]) -> tuple[list[str], str]:
    """A Flutter Linux bundle's GPU compositing — notably a `BackdropFilter`/blur (a `ConvexAppBar`
    blurred bottom bar) — renders as a solid black strip under the sandbox's software GL (llvmpipe),
    so the nav is invisible and untappable (#28). Flutter's Skia CPU rasteriser bypasses GL entirely
    and renders it correctly, so add `--enable-software-rendering` for a detected Flutter bundle.
    Idempotent; a no-op for non-Flutter commands. Returns (argv, note-for-the-result)."""
    import re

    if "--enable-software-rendering" in argv:
        return argv, ""
    exe = next((t for t in argv if t != "env" and not re.match(r"^\w+=", t)), None)
    if not exe:
        return argv, ""
    try:
        bundle = Path(exe).resolve().parent
    except (OSError, RuntimeError):
        return argv, ""
    is_flutter = (bundle / "data" / "flutter_assets").is_dir() or (
        bundle / "lib" / "libflutter_linux_gtk.so"
    ).exists()
    if not is_flutter:
        return argv, ""
    return (
        [*argv, "--enable-software-rendering"],
        " (added --enable-software-rendering: a Flutter bundle's blur renders black under the "
        "sandbox's software GL, so its Skia CPU rasteriser is used instead)",
    )


@mcp.tool()
async def launch_app(
    command: str, wait: float = 6.0, size: str | None = None, device: str | None = None
) -> str:
    """Launch an app in interact's isolated sandbox display and drive it there.

    The sandbox is a clean, WM-less X display the agent owns — non-intrusive (it never touches the
    user's real windows, cursor, or focus) and occlusion-proof. Use it when a window must be driven
    reliably regardless of what the user is doing, or when a GPU/desktop app won't screen-grab on
    the real desktop. After launching, drive it with the normal tools using target="nested:<title>"
    (one window) or target="nested" (the whole sandbox screen): screenshot, run_actions, etc.

    Sizing the display: the default is 1280x800 (desktop-shaped). A MOBILE/phone app laid out for
    portrait looks wrong there — pass device="phone" (or "tablet"/"desktop") for a correctly-shaped
    screen, or size="WxH" (e.g. "412x915") for an exact resolution. The launched window is fitted to
    fill the display. Changing the size respawns the shared sandbox (any other app in it is dropped).

    Transient popups — menus, Qt/QComboBox drop-downs, tooltips — open as SEPARATE override-redirect
    windows that a single-window capture (target="nested:<title>") doesn't include; capture the whole
    sandbox screen (target="nested") to see/act on them, or drive the widget by keyboard (arrows +
    Enter). A blurred bar (Flutter BackdropFilter) can render as a black strip under software GL —
    reach its controls via in-app routing or run on a real GPU.

    command: the shell command to run (e.g. "xterm", "flutter run -d linux", a built binary's path).
    wait: seconds to wait for a window to appear before returning.
    size: nested display resolution as "WxH" (overrides device + the default).
    device: a display shape — "phone" (412x915), "tablet" (820x1180), or "desktop" (1280x800).
    """
    import asyncio
    import shlex

    if unsupported := _desktop_unsupported():
        return unsupported
    config.refresh()
    resolved_size, size_err = _resolve_nested_size(size, device)
    if size_err:
        return size_err
    try:
        backend = _get_sandbox(resolved_size)
    except RuntimeError as e:  # Xephyr/Xvfb not installed
        return f"ERROR: sandbox unavailable — {e}"
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return f"ERROR: could not parse command ({e})"
    if not argv:
        return "ERROR: empty command"

    argv, flutter_note = _flutter_software_render(argv)
    proc = await asyncio.to_thread(backend.spawn, argv)
    deadline = asyncio.get_event_loop().time() + wait
    windows: list[tuple[int, str]] = []
    while asyncio.get_event_loop().time() < deadline:
        if proc.poll() is not None and proc.returncode != 0:
            tail = ""
            if hasattr(backend, "proc_output"):
                tail = await asyncio.to_thread(backend.proc_output, proc)
            detail = f"\nIts output:\n{tail}" if tail else ""
            return (f"App exited immediately (rc={proc.returncode}) — the command failed, not the "
                    f"sandbox (the display was healthy and is kept up for retries).{detail}")
        windows = await asyncio.to_thread(backend.list_windows)
        if windows:
            break
        await asyncio.sleep(0.3)
    if not windows:
        health = backend.display_health() if hasattr(backend, "display_health") else ""
        health = f" {health}" if health else ""
        return (f"Launched `{command}` in the sandbox but no window appeared within {wait:.0f}s.{flutter_note}{health} "
                f"It may still be starting — retry list_desktop_windows, or raise `wait`.")
    # Fit each new window to fill the (now correctly-shaped) display so a mobile app isn't a small
    # rectangle floating in a big screen — then nudge a software-GL app (Flutter/Electron) once so
    # it starts rendered (a stale black buffer otherwise persists until a configure event makes it
    # repaint). Both best-effort — capture self-heals the repaint the same way if it recurs.
    await asyncio.sleep(0.6)  # let the window reach its real size first
    fit = getattr(backend, "fit_window", None)
    repaint = getattr(backend, "force_repaint", None)
    for _, name in windows:
        if fit is not None:
            await asyncio.to_thread(fit, name)
        if repaint is not None:
            await asyncio.to_thread(repaint, name)
    targets = "\n".join(f'  target="nested:{name}"' for _, name in windows)
    return f"Launched `{command}` in the sandbox.{flutter_note} Drive it with:\n{targets}"


@mcp.tool()
async def reset_sandbox() -> str:
    """Tear down interact's isolated sandbox display — kill every app launched into it and stop the
    nested X server. The next launch_app starts a fresh display.

    Use it when sandbox launches start failing (e.g. after many launch_app cycles a long session can
    leak apps and exhaust the display), or to clear all running sandbox apps between rebuilds. The
    real desktop is unaffected — this only touches the isolated display interact owns. A dead display
    is also respawned automatically on the next launch_app, so this is mainly for a proactive reset."""
    import asyncio

    global _sandbox
    if _sandbox is None:
        return "No sandbox is running. The next launch_app will create a fresh one."
    n = len(getattr(_sandbox, "_procs", []))
    await asyncio.to_thread(_close_sandbox)
    return f"Sandbox reset — stopped the nested display and {n} app(s). The next launch_app respawns it."


@mcp.tool()
async def report_issue(title: str, body: str, kind: str = "bug") -> str:
    """Report a problem, missing capability, or feedback about INTERACT ITSELF — not the site/app
    you're automating — to its maintainers, so it gets fixed. Use it when interact errors in a way
    that blocks you, behaves unexpectedly, or is missing something you needed.

    Files a GitHub issue on interact's repo when gh is authed; otherwise it opens the prefilled
    issue page in the user's browser (they just press Submit — tell them). Don't include
    secrets/credentials; interact appends its version + platform itself.
    kind: bug | limitation | feedback.
    """
    from interact.feedback import report

    return report(title, body, kind)


@mcp.tool()
async def record(
    start: bool = True,
    query: str | None = None,
    duration: float | None = None,
    fps: int | None = None,
    path: str | None = None,
    target: str | None = None,
    session: str = _DEFAULT_SESSION,
) -> str:
    """Record actions as video and optionally analyze with vision.

    Browser (target unset): Two-step — record(start=True), perform actions, then record(start=False).
    Desktop (target=<window title>): records for duration seconds, then returns.
    A desktop target and a non-default session are mutually exclusive (list_desktop_windows lists them).

    start: True to begin recording, False to stop and export (browser only).
    query: question for VLM visual analysis of the recording.
    duration: recording length in seconds (desktop target, default from config).
    fps: frames per second (desktop target, default from config).
    path: save the video file to this path.
    """
    win, mgr, err = _resolve_target(target, session)
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
    if desktop.Motion.is_blank(video_bytes):
        # x11grab read a uniform-black surface — same GPU-surface wall as still capture.
        raise desktop.gpu_surface_error(win.name)
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
async def list_providers() -> str:
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
