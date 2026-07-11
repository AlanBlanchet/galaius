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
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from interact.browser import SessionRegistry
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


def _session_response(session: str, body: str) -> str:
    return f"[session: {session}]\n{body}"


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
        "MOTION — an animation, a transition, 'did it move smoothly' — is invisible to screenshots: "
        "use record (start=True → act → start=False, works for browser AND desktop targets; "
        "duration= for a fixed clip). SOUND the app makes: sandbox recordings carry the app's audio "
        "— record(path=…) then transcribe(path=…) to hear/read what it said or played. "
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


def main():
    mcp.run(transport="stdio")
