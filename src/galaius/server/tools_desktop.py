"""Desktop + sandbox MCP tools: list_desktop_windows, launch_app, reset_sandbox, record. The
record tool and its per-surface halves (desktop vs browser) live here beside the launch/reset
surfaces that own the sandbox."""

import asyncio
import json
import math
import os
import shlex
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Literal, cast
from pydantic import BaseModel, Field

from mcp.types import CallToolResult, ContentBlock, TextContent
from mcp.server.fastmcp.utilities.types import Image
from galaius import desktop
from galaius.browser import BrowserManager
from galaius.config.settings import Config
from galaius.desktop import DesktopWindow
from galaius.desktop.nested import KillReport
from galaius.launch import (
    _resolve_nested_size, apply_launch_rewrites, needs_shell, split_env_assignments,
)
from galaius.models import supports_native_video_inline
from galaius.processes import run_isolated_process
from galaius.server import core, sandbox, targets, vlm
from galaius.server.core import _AUTO_SESSION, _NO_WINDOWS_MSG, _session_response, config, mcp
from galaius.vision import MediaAnalysis, MediaItem, RecordingCapture, RecordingResult
from galaius.vision.types import VLMResult
from galaius.vision.session import sample_video_frames


class _VideoStream(BaseModel):
    avg_frame_rate: str = "0/0"


class _VideoFormat(BaseModel):
    duration: str | None = None


class _VideoProbe(BaseModel):
    streams: list[_VideoStream] = Field(default_factory=list)
    format: _VideoFormat = Field(default_factory=_VideoFormat)


async def _record_metadata(video: bytes) -> tuple[float | None, float | None]:
    """Read encoded cadence independently of requested/resampled cadence; absent stays unknown."""
    try:
        with tempfile.TemporaryDirectory(prefix="galaius-record-probe-") as directory:
            source = Path(directory) / "capture"
            source.write_bytes(video)
            code, output, _ = await run_isolated_process(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=avg_frame_rate:format=duration", "-of", "json", str(source)],
                cwd=Path(directory), env={"PATH": os.environ.get("PATH", "")},
                timeout=min(10, config.media_timeout), output_limit=65536,
            )
            if code:
                return None, None
            probe = _VideoProbe.model_validate_json(output)
            try:
                duration = float(probe.format.duration) if probe.format.duration else None
            except ValueError:
                duration = None
            try:
                rate = float(Fraction(probe.streams[0].avg_frame_rate)) if probe.streams else None
            except (ValueError, ZeroDivisionError):
                rate = None
            return (
                rate if rate is not None and math.isfinite(rate) and rate > 0 else None,
                duration if duration is not None and math.isfinite(duration) and duration > 0 else None,
            )
    except (OSError, ValueError, OverflowError, TimeoutError):
        return None, None


async def _record_response(
    video: bytes, *, fps: int, mime: str, path: str | None, context: str, query: str | None
) -> RecordingResult:
    dest = core._save_to_path(path, video) if path else None
    measured_fps, duration = await _record_metadata(video)
    truncation: list[bool] = []
    timestamp_basis: list[Literal["source_pts", "derived_cadence"]] = []
    sampled: list[tuple[bytes, float]] = []
    try:
        sampled = await sample_video_frames(
            MediaItem.from_bytes(video, "video", mime), cast(Config, config),
            fps=fps, frame_cap=config.video_max_frames,
            _truncation=truncation,
            _timestamp_basis=timestamp_basis,
        )
        timestamps = [timestamp for _, timestamp in sampled]
        gaps = [right - left for left, right in zip(timestamps, timestamps[1:])]
        observation = "observed" if len({data for data, _ in sampled}) > 1 else "not_observed"
        capture = RecordingCapture(
            status="captured", artifact=str(dest) if dest else None, requested_fps=fps,
            max_frame_gap=max(gaps) if gaps else None,
            timestamp_basis=timestamp_basis[0] if timestamp_basis else "derived_cadence",
            observation=observation,
            frames_truncated=truncation[0] if truncation else False,
            frame_timestamps=timestamps,
        )
    except Exception:
        capture = RecordingCapture(
            status="captured", artifact=str(dest) if dest else None, requested_fps=fps,
            timestamp_basis="derived_cadence", observation="indeterminate",
        )
    capture.measured_fps = measured_fps
    capture.duration = duration
    if not query:
        response = RecordingResult(capture=capture, analysis=MediaAnalysis(
            status="not_requested", eligible=None, attempted=False, input_kind="none",
            sample_timestamps=[],
        ))
        response._frame_bytes = [data for data, _ in sampled]
        return response
    result = await vlm._vlm(video, context, query, "video", mime)
    analysis = MediaAnalysis(
        status=result.dispatch_status,
        eligible=result.dispatch_eligible,
        attempted=result.dispatch_attempted,
        input_kind=(
            "native_video" if result.video_sampled is False
            else "sampled_frames" if result.dispatch_status == "completed" else "none"
        ),
        sample_timestamps=result.video_sample_timestamps, text=result.text,
    )
    response = RecordingResult(capture=capture, analysis=analysis)
    response._frame_bytes = [data for data, _ in sampled]
    return response


def _record_tool_result(result: RecordingResult) -> RecordingResult:
    frame_bytes = result._frame_bytes
    if len(frame_bytes) != len(result.capture.frame_timestamps):
        frame_bytes = []
        result = result.model_copy(update={
            "capture": result.capture.model_copy(update={
                "frame_timestamps": [], "observation": "indeterminate",
            }),
        })
    content: list[ContentBlock] = [
        TextContent(type="text", text=json.dumps(result.model_dump(mode="json")))
    ]
    content.extend(
        Image(data=frame, format="jpeg").to_image_content()
        for frame in frame_bytes
    )
    return cast(RecordingResult, CallToolResult(
        content=content, structuredContent=result.model_dump(mode="json"), isError=False,
    ))


def _video_model() -> str:
    """The model a recording will actually be judged by, or "" if it can't be resolved — the
    caveat below is diagnostics and must never be the thing that breaks a record call."""
    try:
        return config.resolve_model("video")
    except Exception:
        return ""


def _sampling_caveat(
    model: str | None = None, result: VLMResult | None = None
) -> str:
    """The resolution floor of a video verdict, stated as part of the verdict itself.

    A recording judged by a non-native-video model is ffmpeg-sampled at ``video.fps`` and capped
    at ``video.max_frames``, so an effect finer than one sampling interval CANNOT appear — a real
    100ms-per-element stagger ladder was once reported flatly absent, contradicted by the page's
    own ``document.getAnimations()`` (#86). The model wasn't wrong about its frames; the answer
    was presented without the floor that produced it. Naming the floor turns a false negative
    into an honest "below what this can resolve"."""
    if result is not None and result.video_sampled is False:
        return ""
    if (
        result is None
        and not config.media_sessions_enabled()
        and model
        and supports_native_video_inline(model)
    ):
        return ""
    timestamps = result.video_sample_timestamps if result is not None else []
    interval_ms = (
        round(max(b - a for a, b in zip(timestamps, timestamps[1:])) * 1000)
        if len(timestamps) > 1
        else round(1000 / max(1, config.video_fps))
    )
    return (
        f"\n\n[sampling floor: the largest gap between analyzed frames is ~{interval_ms}ms, "
        "so anything shorter between steps may not be resolved and can read as simultaneous. "
        f"For CSS timing (stagger, delay, duration) use evaluate_js with document.getAnimations() "
        f"— deterministic and exact — rather than a recording.]"
    )


@mcp.tool(category="desktop")
async def list_desktop_windows() -> str:
    """List desktop targets for the `target` param: each connected monitor (target="screen" for
    the whole desktop, target="screen:<name>" e.g. screen:DP-1, or target="screen:<index>") and
    each open window. Target a window by its title, or — when a title isn't unique — by its id
    shown here as target="wid:<id>" (the unambiguous selector)."""
    from galaius.desktop.backend import desktop_supported

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
        # Offer the connector name (DP-1, eDP-1) as the target: indices reorder across
        # sessions/display-manager restarts, connector is stable (#1.6).
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
        nested = "\n".join(
            f'  target="nested:{n}" (or target="nested:wid:{w}")'
            for w, n in sandbox._sandbox.list_windows()
        )
        parts.append(f"Sandbox windows (isolated display; launch_app to add):\n{nested or '  (empty)'}")
        # A URL a sandboxed app opened was CONTAINED, not sent to the user's real browser (#83) —
        # say so, else the control looks broken since nothing visibly happens.
        opened = getattr(sandbox._sandbox, "opened_urls", None)
        if opened is not None and (urls := opened()):
            listed = "\n".join(f"  {u}" for u in urls[-5:])
            parts.append(
                "URLs a sandboxed app asked to open (contained — NOT sent to your real browser):\n"
                f"{listed}"
            )
    return "\n\n".join(parts)


@mcp.tool(category="desktop")
async def launch_app(
    command: str,
    wait: float = 6.0,
    size: str | None = None,
    device: str | None = None,
    cwd: str | None = None,
    replace: bool = True,
) -> str:
    """Launch an app in galaius's isolated sandbox display and drive it there.

    The sandbox is a clean, WM-less X display the agent owns — non-intrusive (it never touches the
    user's real windows, cursor, or focus) and occlusion-proof. Use it when a window must be driven
    reliably regardless of what the user is doing, or when a GPU/desktop app won't screen-grab on
    the real desktop. After launching, drive it with the normal tools using target="nested:<title>"
    (one window) or target="nested" (the whole sandbox screen): screenshot, run_actions, etc.

    Sizing the display: the default is 1280x800 (desktop-shaped). A MOBILE/phone app laid out for
    portrait looks wrong there — pass device="phone" (or "tablet"/"desktop") for a correctly-shaped
    screen, or size="WxH" (e.g. "412x915") for an exact resolution. The launched window is fitted to
    fill the display. Changing the size respawns the shared sandbox (any other app in it is dropped).

    Scrolling a FLUTTER surface: use `scroll` (the wheel), not `drag`. Flutter's default
    ScrollBehavior excludes the mouse from `dragDevices` on desktop, so a mouse DRAG on a
    scrollable (a ListView, a DraggableScrollableSheet) is ignored by the framework itself — a
    real mouse behaves the same way, so this is not something automation can work around. `drag`
    is still right for reordering, sliders, and canvas gestures.

    Transient popups — menus, Qt/QComboBox drop-downs, tooltips — open as SEPARATE override-redirect
    windows that a single-window capture (target="nested:<title>") doesn't include; capture the whole
    sandbox screen (target="nested") to see/act on them, or drive the widget by keyboard (arrows +
    Enter). A blurred bar (Flutter BackdropFilter) can render as a black strip under software GL —
    reach its controls via in-app routing or run on a real GPU.

    command: the command to run (e.g. "xterm", "flutter run -d linux", a built binary's path).
        Shell syntax works too — "cd /my/proj && uv run app" runs via bash — but prefer `cwd=`
        for a project directory (a plain command keeps the launch rewrites, e.g. Flutter's
        software-GL flag, which shell commands bypass). A `VAR=value` prefix ("FLUTTER_DEBUG=1
        ./bundle/app") sets that variable for the launch and keeps the rewrites too.
    wait: seconds to wait for a window to appear before returning.
    size: nested display resolution as "WxH" (overrides device + the default).
    device: a display shape — "phone" (412x915), "tablet" (820x1180), or "desktop" (1280x800).
    cwd: working directory to launch in (so "uv run app" finds the project without a cd).
    replace: stop whatever was launched into the sandbox before starting this app (the default).
        The sandbox is shared, so relaunching used to ADD an instance rather than replace one:
        several live copies of the same app accumulated and captures composited a stray widget
        from an older instance over the current window. Pass replace=False to run two apps side
        by side on one display.
    """
    if unsupported := targets._desktop_unsupported():
        return unsupported
    config.refresh()
    resolved_size, size_err = _resolve_nested_size(size, device)
    if size_err:
        return size_err
    if cwd is not None:
        cwd_path = Path(cwd).expanduser()
        if not cwd_path.is_dir():
            return f"ERROR: cwd {cwd!r} is not a directory"
        cwd = str(cwd_path)
    try:
        backend = sandbox._get_sandbox(resolved_size)
    except RuntimeError as e:  # Xephyr/Xvfb not installed
        return f"ERROR: sandbox unavailable — {e}"
    env: dict[str, str] | None = None
    if needs_shell(command):
        argv, flutter_note = ["bash", "-c", command], ""
    else:
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return f"ERROR: could not parse command ({e})"
        # `FOO=bar app` is shell phrasing with no shell marker: exec'd verbatim it died on a
        # program named 'FOO=bar' (#117). Assignments become the launch env, command stays on the
        # exec path, rewrites below still apply.
        assignments, argv = split_env_assignments(argv)
        if not argv:
            if not assignments:
                return "ERROR: empty command"
            return (f"ERROR: no command after the assignments — `{command}` sets "
                    f"{', '.join(assignments)} but names no program to run; put it after them, "
                    f"e.g. `{command} app`")
        env = assignments or None
        argv, flutter_note = apply_launch_rewrites(argv, getattr(backend, "display", ":?"))
    # An identical command already running is almost never a second app wanted: it's a retried
    # tool call. Spawning anyway once produced two same-titled windows, target="nested:<title>"
    # silently alternating between them — ~20 actions landed on the invisible one (#87). Point the
    # caller at what's already there instead.
    running = getattr(backend, "running_command", None)
    if running is not None and running(argv) is not None:
        windows = await asyncio.to_thread(backend.list_windows)
        existing = "\n".join(f'  target="nested:{n}" (or target="nested:wid:{w}")' for w, n in windows)
        return (
            f"`{command}` is ALREADY running in the sandbox — not launching a second copy (two "
            f"same-titled windows make target=\"nested:<title>\" ambiguous, so actions can land on "
            f"the wrong one). Drive the running app with:\n{existing}\n"
            f"To restart it, call reset_sandbox first, or launch a genuinely different command."
        )
    report = KillReport(killed=0)
    if replace:
        kill_apps = getattr(backend, "kill_apps", None)
        if kill_apps is not None:
            report = await asyncio.to_thread(kill_apps)
    if report.survivors:
        return (
            f"ERROR: replace stopped {report.killed} app(s) and swept {report.swept} leftover "
            f"process(es), but successor was not launched. SURVIVED: "
            f"{report.describe_survivors()}. Call reset_sandbox before retrying."
        )
    replaced_note = (
        f" Replaced {report.killed} app(s) already in the sandbox." if report.killed else ""
    )
    if report.swept:
        replaced_note += (
            f" Swept {report.swept} leftover process(es) an earlier launch had left on the "
            "sandbox display."
        )
    # An exec that can't start once raised straight out of the tool, reaching the agent as
    # FastMCP's generic exception text instead of a guided ERROR — prefix consumed now, so the
    # name shown is the real command (#117).
    env_note = f" with {shlex.join(f'{k}={v}' for k, v in env.items())} set" if env else ""
    tried = f"`{shlex.join(argv)}`{env_note}"
    try:
        proc = await asyncio.to_thread(backend.spawn, argv, cwd, env)
    except FileNotFoundError:
        return (f"ERROR: {argv[0]!r} is not an executable on the sandbox PATH (nor an existing "
                f"path) — nothing was launched. Tried {tried}. Pass the binary's absolute path, or "
                f"cwd= with a path relative to it (e.g. ./build/app).")
    except PermissionError:
        return (f"ERROR: {argv[0]!r} exists but is not executable — `chmod +x` it first. Nothing "
                f"was launched; tried {tried}.")
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
    # Fit each new window to fill the (now correctly-shaped) display, so a mobile app isn't a
    # small rectangle floating in a big screen — then nudge a software-GL app (Flutter/Electron)
    # once so it starts rendered (a stale black buffer otherwise persists until a configure event
    # repaints it). Both best-effort — capture self-heals the repaint the same way if it recurs.
    await asyncio.sleep(0.6)  # let the window reach its real size first
    fit = getattr(backend, "fit_window", None)
    repaint = getattr(backend, "force_repaint", None)
    for _, name in windows:
        if fit is not None:
            await asyncio.to_thread(fit, name)
        if repaint is not None:
            await asyncio.to_thread(repaint, name)
    # Offer the window ID alongside the title: an app with no per-instance title makes
    # target="nested:<title>" ambiguous the moment a second window exists — wid: cannot drift (#87).
    targets_out = "\n".join(
        f'  target="nested:{name}"  (unambiguous: target="nested:wid:{wid}")' for wid, name in windows
    )
    return (
        f"Launched `{command}` in the sandbox.{flutter_note}{replaced_note} Drive it with:\n{targets_out}"
    )


@mcp.tool(category="desktop")
async def reset_sandbox() -> str:
    """Tear down galaius's isolated sandbox display — kill every app launched into it and stop the
    nested X server. The next launch_app starts a fresh display.

    Use it when sandbox launches start failing (e.g. after many launch_app cycles a long session can
    leak apps and exhaust the display), or to clear all running sandbox apps between rebuilds. The
    real desktop is unaffected — this only touches the isolated display galaius owns. A dead display
    is also respawned automatically on the next launch_app, so this is mainly for a proactive reset."""
    if sandbox._sandbox is None:
        return "No sandbox is running. The next launch_app will create a fresh one."
    n = len(getattr(sandbox._sandbox, "_procs", []))
    await asyncio.to_thread(sandbox._close_sandbox)
    return f"Sandbox reset — stopped the nested display and {n} app(s). The next launch_app respawns it."


@mcp.tool(category="media")
async def record(
    start: bool = True,
    query: str | None = None,
    duration: float | None = None,
    fps: int | None = None,
    path: str | None = None,
    target: str | None = None,
    session: str = _AUTO_SESSION,
) -> RecordingResult:
    """Record actions as video and optionally analyze with vision.

    Browser (target unset): Two-step — record(start=True), perform actions, then record(start=False).
    Starting AND stopping browser recording reload the page. Cookies and localStorage carry over,
    but in-memory state and sessionStorage can reset. Seed the state under test after starting;
    reacquire refs after either transition. The result repeats this warning.
    Desktop (target=<window title> / nested): same two-step by default — record(start=True) begins a
    NON-blocking session and returns at once (so you can drive actions, e.g. tap a control to trigger
    an animation, while it captures), then record(start=False) stops and analyzes. Pass duration= for
    a blocking one-shot clip of fixed length instead (no interleaved actions).
    A desktop target and a session you named are mutually exclusive (list_desktop_windows lists them).

    Sandbox (nested) recordings include the APP'S AUDIO: launched apps play into the sandbox's
    private sink (never the user's speakers), and its monitor is muxed into the mp4 — so
    record(path=...) then transcribe(path=...) hears what the app said. Real-desktop/browser
    recordings stay video-only.

    start: True to begin recording, False to stop and export.
    query: question for VLM visual analysis of the recording.
    duration: fixed clip length in seconds (desktop one-shot mode); omit for a start/stop session.
    fps: requested frames per second, default from config. Desktop capture uses this rate.
        Browser capture uses the recorder's own encoded rate; requested_fps preserves your
        request and measured_fps reports the saved clip's rate when probeable. Sample timestamps
        describe sampled frames, not distinct source observations.
    path: save the video here. A relative path lands under ~/.galaius/out (galaius's output dir),
        never the server's cwd; "~" expands. The reply names the absolute file written.

    SAMPLING LIMIT — read before asking about a FAST animation. Session backends always sample the
    clip into still frames at `video.fps` (default 5/s) and cap it at `video.max_frames`; an API
    backend may send native video when supported. A sampled result cannot see anything shorter than
    one interval (~200ms at the default), so it can report "it happened all at once" even when a
    faster stagger exists. That is a limit of the sampling, NOT evidence the animation is missing
    (#86). For CSS timing claims — a staggered reveal, a transition duration, an animation-delay
    ladder — do not use record at all: ask the page directly with evaluate_js and
    `document.getAnimations()`, reading each animation's `effect.getComputedTiming()`
    (delay/duration) and its keyframes. That is deterministic, free, and exact. Use record for WHAT
    HAPPENED over time at human speed; use getAnimations for sub-second timing.
    """
    if fps is not None and fps <= 0:
        raise ValueError("Recording fps must be positive")
    win, mgr, err = targets._resolve_target(target, session)
    if err:
        return _record_tool_result(RecordingResult(
            capture=RecordingCapture(
                status="unavailable", requested_fps=fps or config.video_fps,
                timestamp_basis="derived_cadence", observation="indeterminate",
            ),
            analysis=MediaAnalysis(
                status="unavailable", eligible=False, attempted=False, input_kind="none", text=err,
            ),
        ))
    if win:
        result = await _record_desktop(win, query, start, duration, fps, path)
    else:
        result = await _record_browser(mgr, start, query, path, session, fps=fps)
    return _record_tool_result(result)


async def _record_desktop(
    win: DesktopWindow,
    query: str | None,
    start: bool,
    duration: float | None,
    fps: int | None,
    path: str | None,
) -> RecordingResult:
    """Desktop/nested recording. An explicit ``duration`` is a blocking one-shot clip (backward
    compatible). Otherwise it's the browser-style two-step session: ``start=True`` begins capture and
    returns at once so actions can run during it; ``start=False`` stops and analyzes (#61/#62)."""
    actual_fps = fps or config.video_fps

    if duration is None:
        if start:
            win.start_video(actual_fps)
            return RecordingResult(
                capture=RecordingCapture(
                    status="started", requested_fps=actual_fps,
                    timestamp_basis="derived_cadence", observation="indeterminate",
                ),
                analysis=MediaAnalysis(
                    status="not_requested", eligible=None, attempted=False,
                    input_kind="none",
                    text="Recording started; call record(start=False) to stop it.",
                ),
            )
        video_bytes = win.stop_video()
        if video_bytes is None:
            return RecordingResult(
                capture=RecordingCapture(
                    status="unavailable", requested_fps=actual_fps,
                    timestamp_basis="derived_cadence", observation="indeterminate",
                ),
                analysis=MediaAnalysis(
                    status="unavailable", eligible=False, attempted=False,
                    input_kind="none",
                    text="No recording in progress; call record(start=True), or pass duration.",
                ),
            )
        dur_label = "session"
    else:
        video_bytes = win.capture_video(duration, actual_fps)
        dur_label = f"{duration}s"

    context = f"Desktop window recording: {win.name} ({win.w}x{win.h}, {dur_label})"
    return await _record_response(
        video_bytes, fps=actual_fps, mime="video/mp4", path=path, context=context, query=query,
    )


async def _record_browser(
    mgr: BrowserManager,
    start: bool,
    query: str | None,
    path: str | None,
    session: str,
    fps: int | None = None,
) -> RecordingResult:
    actual_fps = fps if fps is not None else mgr.recording_requested_fps or config.video_fps
    if actual_fps <= 0:
        raise ValueError("Recording fps must be positive")
    if start:
        url, trouble = await mgr.start_recording(fps=actual_fps)
        return RecordingResult(
            capture=RecordingCapture(
                status="started", requested_fps=actual_fps,
                timestamp_basis="derived_cadence", observation="indeterminate",
            ),
            analysis=MediaAnalysis(
                status="not_requested", eligible=None, attempted=False, input_kind="none",
                text="Browser recording started by reloading the page; in-memory state and sessionStorage may reset. Seed test state and reacquire refs now."
                     + (f" {trouble}" if trouble else ""),
            ),
        )
    video_bytes = await mgr.stop_recording()
    if not video_bytes:
        return RecordingResult(
            capture=RecordingCapture(
                status="unavailable", requested_fps=actual_fps,
                timestamp_basis="derived_cadence", observation="indeterminate",
            ),
            analysis=MediaAnalysis(
                status="unavailable", eligible=False, attempted=False,
                input_kind="none",
                text="No video data captured. Browser recording stopped by reloading the page; in-memory state and sessionStorage may reset. Reacquire refs before continuing.",
            ),
        )
    response = await _record_response(
        video_bytes, fps=actual_fps, mime="video/webm", path=path,
        context="Browser recording", query=query,
    )
    warning = "Browser recording stopped by reloading the page; in-memory state and sessionStorage may reset. Reacquire refs before continuing."
    response.analysis.text = warning + (f"\n{response.analysis.text}" if response.analysis.text else "")
    return response
