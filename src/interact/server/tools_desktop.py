"""Desktop + sandbox MCP tools: list_desktop_windows, launch_app, reset_sandbox, record. The
record tool and its per-surface halves (desktop vs browser) live here beside the launch/reset
surfaces that own the sandbox."""

import asyncio

from interact import desktop
from interact.browser import BrowserManager
from interact.desktop import DesktopWindow
from interact.launch import _resolve_nested_size, apply_launch_rewrites
from interact.server import core, sandbox, targets, vlm
from interact.server.core import _DEFAULT_SESSION, _NO_WINDOWS_MSG, _session_response, config, mcp


@mcp.tool()
async def list_desktop_windows() -> str:
    """List desktop targets for the `target` param: each connected monitor (target="screen" for
    the whole desktop, target="screen:<name>" e.g. screen:DP-1, or target="screen:<index>") and
    each open window. Target a window by its title, or — when a title isn't unique — by its id
    shown here as target="wid:<id>" (the unambiguous selector)."""
    from interact.desktop.backend import desktop_supported

    if not desktop_supported():
        # macOS/Windows: the portable backend drives the whole screen; per-window enum is Linux-only.
        pb = sandbox._get_portable()
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
    if sandbox._sandbox is not None:
        nested = "\n".join(f'  target="nested:{n}"' for _, n in sandbox._sandbox.list_windows())
        parts.append(f"Sandbox windows (isolated display; launch_app to add):\n{nested or '  (empty)'}")
    return "\n\n".join(parts)


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
    import shlex

    if unsupported := targets._desktop_unsupported():
        return unsupported
    config.refresh()
    resolved_size, size_err = _resolve_nested_size(size, device)
    if size_err:
        return size_err
    try:
        backend = sandbox._get_sandbox(resolved_size)
    except RuntimeError as e:  # Xephyr/Xvfb not installed
        return f"ERROR: sandbox unavailable — {e}"
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return f"ERROR: could not parse command ({e})"
    if not argv:
        return "ERROR: empty command"

    argv, flutter_note = apply_launch_rewrites(argv, getattr(backend, "display", ":?"))
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
    targets_out = "\n".join(f'  target="nested:{name}"' for _, name in windows)
    return f"Launched `{command}` in the sandbox.{flutter_note} Drive it with:\n{targets_out}"


@mcp.tool()
async def reset_sandbox() -> str:
    """Tear down interact's isolated sandbox display — kill every app launched into it and stop the
    nested X server. The next launch_app starts a fresh display.

    Use it when sandbox launches start failing (e.g. after many launch_app cycles a long session can
    leak apps and exhaust the display), or to clear all running sandbox apps between rebuilds. The
    real desktop is unaffected — this only touches the isolated display interact owns. A dead display
    is also respawned automatically on the next launch_app, so this is mainly for a proactive reset."""
    if sandbox._sandbox is None:
        return "No sandbox is running. The next launch_app will create a fresh one."
    n = len(getattr(sandbox._sandbox, "_procs", []))
    await asyncio.to_thread(sandbox._close_sandbox)
    return f"Sandbox reset — stopped the nested display and {n} app(s). The next launch_app respawns it."


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
    Desktop (target=<window title> / nested): same two-step by default — record(start=True) begins a
    NON-blocking session and returns at once (so you can drive actions, e.g. tap a control to trigger
    an animation, while it captures), then record(start=False) stops and analyzes. Pass duration= for
    a blocking one-shot clip of fixed length instead (no interleaved actions).
    A desktop target and a non-default session are mutually exclusive (list_desktop_windows lists them).

    Sandbox (nested) recordings include the APP'S AUDIO: launched apps play into the sandbox's
    private sink (never the user's speakers), and its monitor is muxed into the mp4 — so
    record(path=...) then transcribe(path=...) hears what the app said. Real-desktop/browser
    recordings stay video-only.

    start: True to begin recording, False to stop and export.
    query: question for VLM visual analysis of the recording.
    duration: fixed clip length in seconds (desktop one-shot mode); omit for a start/stop session.
    fps: frames per second (desktop target, default from config).
    path: save the video file to this path.
    """
    win, mgr, err = targets._resolve_target(target, session)
    if err:
        return err
    if win:
        return await _record_desktop(win, query, start, duration, fps, path)
    return await _record_browser(mgr, start, query, path, session)


async def _record_desktop(
    win: DesktopWindow,
    query: str | None,
    start: bool,
    duration: float | None,
    fps: int | None,
    path: str | None,
) -> str:
    """Desktop/nested recording. An explicit ``duration`` is a blocking one-shot clip (backward
    compatible). Otherwise it's the browser-style two-step session: ``start=True`` begins capture and
    returns at once so actions can run during it; ``start=False`` stops and analyzes (#61/#62)."""
    actual_fps = fps or config.video_fps

    if duration is None:
        if start:
            win.start_video(actual_fps)
            return (
                f"Desktop recording started for '{win.name}'. Drive your actions now, then call "
                f"record(start=False, target=...) to stop and analyze. (For a fixed clip with no "
                f"interleaved actions, pass duration= instead.)"
            )
        video_bytes = win.stop_video()
        if video_bytes is None:
            return (
                f"No recording in progress for '{win.name}'. Call record(start=True, target=...) "
                f"first to begin a session, or pass duration= for a one-shot clip."
            )
        dur_label = "session"
    else:
        video_bytes = win.capture_video(duration, actual_fps)
        dur_label = f"{duration}s"

    if desktop.Motion.is_blank(video_bytes):
        # x11grab read a uniform-black surface — same GPU-surface wall as still capture.
        raise desktop.gpu_surface_error(win.name)
    if path:
        core._save_to_path(path, video_bytes)

    is_static = not desktop.Motion.detect(video_bytes)
    if is_static and not query:
        return (
            f"Recording captured but no motion detected — frames are identical. "
            f"The window content did not change during the {dur_label} recording."
        )

    context = f"Desktop window recording: {win.name} ({win.w}x{win.h}, {dur_label})"
    if is_static:
        context = (
            "WARNING: Recording appears static — no significant motion was detected "
            "between frames. Describe only what you actually observe.\n" + context
        )
    r = await vlm._vlm(video_bytes, context, query, "video", "video/mp4")
    return vlm._fmt_timing(r)


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
        return _session_response(session, "Recording stopped but no video data captured.")
    result = await vlm._media_response(video_bytes, "Browser recording", query, path, "video", "video/webm")
    if result:
        return _session_response(session, result)
    size = len(video_bytes)
    msg = f"Recording stopped. Video captured ({size} bytes)."
    if path:
        msg += f" Saved to {path}."
    return _session_response(session, msg)
