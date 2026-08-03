"""Core of the ``interact`` MCP server package: the shared ``FastMCP`` instance every tool
registers on, the server lifespan, the tool-facing instructions, the browser
:class:`SessionRegistry`, and the small dependency-free helpers (path/mime/label formatting)
that every other submodule leans on.

The package is split by COHESION — ``vlm`` / ``sandbox`` / ``targets`` / ``capture`` hold the
private helpers, ``tools_*`` hold the ``@mcp.tool`` surfaces — so no single file is a 2000-line
monolith. ``server/__init__`` re-exports the whole public + test-patched surface, so both
``import interact.server as srv; srv._vlm`` and ``from interact.server import _scan_elements``
keep resolving. Cross-module calls to a monkeypatched helper are MODULE-QUALIFIED
(``vlm._vlm(...)``, ``targets._resolve_target(...)``) so a test patching that helper on its home
module is seen at the call site; same-module calls stay bare.
"""

import asyncio
import functools
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from interact.browser import SessionRegistry
from interact.debug_utils import Debug, _CURRENT_INV
from interact.runtime import breaker, config  # noqa: F401 — breaker re-exported for tests/vlm

_log = logging.getLogger("interact")

_log.info(
    "Models: image=%s, component=%s, video=%s",
    config.image_model or "not set",
    config.component_model or "not set",
    config.video_model or "not set",
)

_sessions = SessionRegistry(config)
_DEFAULT_SESSION = "default"
_NO_WINDOWS_MSG = "No desktop windows detected (X11/maim required)."
# js/ lives at the package root (interact/js), one level up from this server/ subpackage.
_ANNOTATE_JS = (Path(__file__).parent.parent / "js" / "annotate_elements.js").read_text()

_DBG_ELEMENTS = "get_interactive_elements"
_DBG_ACTIONS = "run_actions"
_MAX_FALLBACKS = 3


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


# The shared "default" session is a shared mailbox: another caller on the same server can navigate
# it, or close its tab, between two of YOUR calls. That used to be silent — you discovered it several
# calls later as a stale-ref timeout or a page you never opened (#96/#98/#99/#101). We remember the
# URL each caller left a session on, and report the difference when it moved on its own.
_session_url_baseline: dict[str, str] = {}
_session_drift_note: dict[str, str] = {}
_session_shared_warned: set[str] = set()


def _caller_key(session: str) -> str:
    """Identify the CALLER, not just the session: the baseline has to be per-connection, or the
    other caller's own navigate silently rebaselines it and the drift is never noticed. Two agents
    sharing ONE connection are indistinguishable at the protocol level — that limit is real and is
    why the note also nudges toward a uniquely-named session."""
    try:
        return f"{id(mcp.get_context().session)}:{session}"
    except Exception:  # CLI / tests: no live MCP request
        return f"local:{session}"


def _peek_session_url(session: str) -> str | None:
    """The session's current URL, or None when it has no live page. Side-effect free on purpose —
    it must never start a browser just to look."""
    mgr = _sessions.peek(session)
    return mgr.peek_url() if mgr is not None else None


def _observe_session_url(session: str) -> None:
    """Remember where this call left the session — the baseline the next call is compared against."""
    url = _peek_session_url(session)
    if url:
        _session_url_baseline[_caller_key(session)] = url


def _check_session_drift(session: str) -> None:
    """Called as a tool STARTS: did the session move since we last left it? Only another caller
    (or the page redirecting itself) can do that, so it is worth saying out loud."""
    was = _session_url_baseline.get(_caller_key(session))
    now = _peek_session_url(session)
    if was and now and was != now:
        nudge = (
            f" Use a unique session= name to stay isolated (e.g. session=\"{session}-1\")."
            if session == _DEFAULT_SESSION
            else ""
        )
        _session_drift_note[_caller_key(session)] = (
            f"NOTE: this session moved on its own since your last call — you left it on {was}, "
            f"it is now on {now}. Another caller shares this session (or the page redirected "
            f"itself), so refs and page state from before may be stale.{nudge}"
        )


def _shared_session_nudge(session: str) -> str | None:
    """Said ONCE per caller, the first time it uses the shared session. Every reporter of the
    contention bugs had read the instructions and used "default" anyway — a warning at connect
    time is not a reminder at the moment it matters (#96/#98)."""
    key = _caller_key(session)
    if session != _DEFAULT_SESSION or key in _session_shared_warned:
        return None
    _session_shared_warned.add(key)
    return (
        'NOTE: "default" is a shared browser session — a concurrent caller (a subagent, another '
        "agent on this server) drives the same tabs, so your page can change between calls. For "
        'anything beyond a one-shot check, pass your own session= name (e.g. session="critic-1").'
    )


def _session_response(session: str, body: str) -> str:
    notes = [
        n
        for n in (_session_drift_note.pop(_caller_key(session), None), _shared_session_nudge(session))
        if n
    ]
    _observe_session_url(session)  # rebaseline: this is where this call left it
    return "\n".join([f"[session: {session}]", *notes, body])


def _not_found(what: str) -> str:
    return f"{what} not found — run get_interactive_elements first"


def _desktop_label(win) -> str:
    return f"[window: {win.name}]"


@asynccontextmanager
async def _lifespan(_: FastMCP) -> AsyncIterator[None]:
    from interact.server import sandbox
    from interact.server_registry import register_server, unregister_server

    reg = register_server()  # record pid+version so `interact doctor` can flag a stale long-lived server
    reaper = asyncio.create_task(sandbox._idle_session_reaper(config.session_idle_ttl))
    try:
        yield
    finally:
        reaper.cancel()
        with suppress(asyncio.CancelledError):
            await reaper
        await _sessions.close_all()
        sandbox._close_sandbox()
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
        "SEEING & MEDIA — pick the cheapest tool that answers your question: a FACT about the page "
        "(element present, text, count, attribute, URL) → get_page_state / get_interactive_elements "
        "/ evaluate_js, no pixels needed. How it LOOKS → screenshot (add query= only when you need "
        "an interpretation, it costs a VLM call). Judging quality → review_ui (find defects) / "
        "verify_ui (PASS-FAIL your requirements) / measure_ui (deterministic contrast, free). "
        "BATCHING — reading or clicking over MANY elements is ONE run_actions evaluate_js step (a JS "
        "program run against the live page: query → filter → loop → read/act, args + return "
        "value, browser-isolated), NOT N get_interactive_elements→act round-trips. "
        "MOTION & VIDEO — an animation, a transition, 'did it move smoothly', or WHAT HAPPENED over "
        "time is invisible to a still screenshot: record it (start=True → act → start=False, browser "
        "AND desktop; duration= for a fixed clip), then pass query= to have the video model EXPLAIN the "
        "sequence (a native video model watches it; others sample frames). "
        "HEARING — interact can HEAR, not only see: transcribe(path, query=…) turns any local audio OR "
        "video file (a download_asset clip, a record(path=…) capture, any mp3/wav/mp4/mov) into text, or "
        "ANSWERS a question about the sound — speakers, tone, music, spoken words — acoustically when the "
        "audio model can listen (Gemini, gpt-4o-audio). Sandbox recordings carry the launched app's own "
        "audio, so record(path=…) then transcribe(path=…) hears what a native app said or played. "
        "SESSIONS: the \"default\" browser session is SHARED by every caller on this connection — "
        "a subagent running concurrently (a critic, a tester) drives the same tabs, so the page "
        "can change under you between calls. Any concurrent or long-lived workflow should pass its "
        "own unique `session` name (e.g. session=\"critic-payload\"); sessions are fully isolated "
        "browser contexts. "
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


def instrumented(fn):
    """Per-call scaffolding shared by every dumping ``@mcp.tool``: refresh the live config, open the
    invocation dump dir (reachable in the body via ``Debug.inv()``), and dump the tool's return value
    ONCE. So no tool repeats ``config.refresh()`` / ``new_invocation_dir()``, and EVERY return path
    — early error returns included — is logged, not just the happy path (measure_ui used to drop its
    error returns). Applied UNDER ``@mcp.tool()`` (``functools.wraps`` preserves the signature FastMCP
    introspects for the tool schema); the tool body keeps its own tool-specific ``dump_input``."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        config.refresh()  # ~/.interact/config.env is source of truth: pick up live edits per call
        # Before the body runs: did a concurrent caller move this session while we were away?
        # Checked here so EVERY session tool reports it, not just get_page_state (#96/#98/#99/#101).
        if not kwargs.get("target"):
            _check_session_drift(kwargs.get("session") or _DEFAULT_SESSION)
        inv = Debug.new_invocation_dir(kwargs.get("debug_dir"), fn.__name__)
        token = _CURRENT_INV.set(inv)
        try:
            result = await fn(*args, **kwargs)
            Debug.dump_output(inv, result)
            return result
        finally:
            _CURRENT_INV.reset(token)

    return wrapper


def main():
    mcp.run(transport="stdio")
