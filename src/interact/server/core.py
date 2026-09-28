"""Core of the ``interact`` MCP server package: the shared ``FastMCP`` instance every tool
registers on, the server lifespan, tool-facing instructions, the browser
:class:`SessionRegistry`, and small dependency-free helpers (path/mime/label formatting) every
other submodule leans on.

Split by COHESION — ``vlm``/``sandbox``/``targets``/``capture`` hold private helpers, ``tools_*``
hold the ``@mcp.tool`` surfaces — so no file is a 2000-line monolith. ``server/__init__``
re-exports the whole public + test-patched surface, so both ``srv._vlm`` and ``from
interact.server import _scan_elements`` keep resolving. Cross-module calls to a monkeypatched
helper are MODULE-QUALIFIED (``vlm._vlm(...)``) so a test patching it on its home module is
seen at the call site; same-module calls stay bare.
"""

import asyncio
import functools
import hashlib
import inspect
import json
import logging
import threading
import weakref
from typing import Literal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from interact.browser import SessionRegistry
from interact.debug_utils import _CURRENT_INV, Debug, resolve_output_path
from interact.desktop import CaptureError
from interact.runtime import (  # noqa: F401 — breaker re-exported for tests/vlm
    breaker,
    config,
)
from interact.upgrade.quiet import QuietPoint
from interact.vision.core import VisionError

_log = logging.getLogger("interact")

_log.info(
    "Model criteria: image=%s, component=%s, video=%s",
    config.criteria_for("image"),
    config.criteria_for("component"),
    config.criteria_for("video"),
)

_sessions = SessionRegistry(config)
#: The SHARED browser session. Selecting it is now a deliberate act — see ``_AUTO_SESSION``.
_DEFAULT_SESSION = "default"
#: The value every session parameter DEFAULTS to, i.e. what a caller that passed nothing sends.
#: It is not a session name: :func:`_resolve_session` turns it into this connection's own minted
#: name before any tool body runs. A distinct sentinel is the only way to tell "omitted" from
#: "explicitly default" — FastMCP fills every parameter from its default before calling the tool,
#: so a default of ``"default"`` makes the two indistinguishable.
_AUTO_SESSION = "auto"
_NO_WINDOWS_MSG = "No desktop windows detected (X11/maim required)."
# js/ lives at the package root (interact/js), one level up from this subpackage.
_ANNOTATE_JS = (Path(__file__).parent.parent / "js" / "annotate_elements.js").read_text()

_DBG_ELEMENTS = "get_interactive_elements"
_DBG_ACTIONS = "run_actions"
_MAX_FALLBACKS = 3


#: Caller-supplied output path rule (#120) lives in debug_utils, beside the dir it anchors on;
#: this is the name every saving tool imports.
_resolve_save_path = resolve_output_path


def _save_to_path(path: str, data: bytes) -> Path:
    """Write ``data`` where :func:`_resolve_save_path` puts ``path``; returns that absolute path."""
    dest = _resolve_save_path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def _saved_note(dest: Path, data: bytes) -> str:
    """The one sentence a saving tool ends its reply with — the absolute file + its size."""
    return f"Saved to {dest} ({len(data)} bytes)"


_AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".webm": "audio/webm", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".mpeg": "audio/mpeg", ".mpga": "audio/mpeg",
}


def _audio_mime(path: str) -> str:
    """MIME for an audio/media file, by extension (default mp3)."""
    return _AUDIO_MIME.get(Path(path).suffix.lower(), "audio/mpeg")


def _parse_int_tuple(s: str | None, n: int, name: str):
    """Parse "a,b,…" into an ``n``-int tuple: the tuple, ``None`` if unset, or an
    ``"ERROR: …"`` string if malformed (caller returns that straight to the agent)."""
    if not s:
        return None
    try:
        vals = tuple(int(p.strip()) for p in s.split(",") if p.strip())
    except ValueError:
        return f"ERROR: {name} must be {n} integers like '{','.join(['0'] * n)}', got {s!r}"
    if len(vals) != n:
        return f"ERROR: {name} needs {n} integers (got {len(vals)}: {s!r})"
    return vals


# The shared "default" session is a mailbox: another caller can navigate it or close its tab
# between two of YOUR calls. Used to be silent — discovered later as a stale-ref timeout or an
# unopened page (#96/#98/#99/#101). We remember the URL each caller left it on, and report the
# difference when it moved on its own.
_session_url_baseline: dict[str, str] = {}
_session_drift_note: dict[str, str] = {}
_session_shared_warned: set[str] = set()


def _connection() -> object | None:
    """The object that IS this MCP connection, or None outside a live request (CLI / tests).
    The one identity every session helper derives from — caller keys and minted session names
    must come from the same source or they disagree and drift detection breaks."""
    try:
        return mcp.get_context().session
    except Exception:
        return None


def _caller_key(session: str) -> str:
    """Identify the CALLER, not just the session: baseline must be per-connection, or another
    caller's navigate silently rebaselines it and drift is never noticed. Two agents sharing
    ONE connection are indistinguishable at the protocol level — a real limit, and why a shared
    session still gets a nudge."""
    conn = _connection()
    return f"{id(conn)}:{session}" if conn is not None else f"local:{session}"


# Keyed by the connection OBJECT, not its id(): an id is reused once a connection is collected,
# and a new caller inheriting the previous one's tabs/cookies is the exact bug this replaces.
_auto_session_names: "weakref.WeakKeyDictionary[object, str]" = weakref.WeakKeyDictionary()


def _mint_session_name(conn: object) -> str:
    """A short, readable, unguessable name for one connection — ``caller-3f9a``. Widened until
    it collides with no other live caller."""
    taken = set(_auto_session_names.values())
    seed = str(id(conn)).encode()
    for size in (2, 3, 4):
        name = f"caller-{hashlib.blake2s(seed, digest_size=size).hexdigest()}"
        if name not in taken:
            return name
    return f"caller-{id(conn):x}"


def _auto_session_name() -> str | None:
    """This connection's OWN session name, minted once and stable for its whole life; None when
    there is no connection to derive one from (a direct in-process call has no second caller to
    be isolated from, so it keeps the shared session)."""
    conn = _connection()
    if conn is None:
        return None
    try:
        name = _auto_session_names.get(conn)
        if name is None:
            name = _mint_session_name(conn)
            _auto_session_names[conn] = name
        return name
    except TypeError:  # a connection object that can't be weak-referenced
        return _mint_session_name(conn)


def _resolve_session(session: str) -> str:
    """The one rule turning what the caller sent into the session it means. Idempotent: only the
    ``_AUTO_SESSION`` sentinel moves, so every helper that calls it lands on the same name."""
    if session != _AUTO_SESSION:
        return session
    return _auto_session_name() or _DEFAULT_SESSION


def _named_session(session: str) -> bool:
    """True when the caller CHOSE this browser session (not its auto-minted one, not the shared
    mailbox) — what "a desktop target and a browser session are mutually exclusive" really asks."""
    return session not in (_AUTO_SESSION, _DEFAULT_SESSION, _auto_session_name())


def _peek_session_url(session: str) -> str | None:
    """The session's current URL, or None when it has no live page. Side-effect free by
    design — never start a browser just to look."""
    mgr = _sessions.peek(session)
    return mgr.peek_url() if mgr is not None else None


def _observe_session_url(session: str) -> None:
    """Remember where this call left the session — the baseline the next call is compared against."""
    url = _peek_session_url(session)
    if url:
        _session_url_baseline[_caller_key(session)] = url


def _check_session_drift(session: str) -> None:
    """Called as a tool STARTS: did the session move since we last left it? Only another
    caller (or the page redirecting itself) can do that, so it's worth saying out loud."""
    was = _session_url_baseline.get(_caller_key(session))
    now = _peek_session_url(session)
    if was and now and was != now:
        # Cause must FIT this session. A session the caller NAMED almost never has a second
        # caller; blaming one sent a reporter chasing a cross-talk bug that wasn't there (#106)
        # — there, the page moving ITSELF is the likelier explanation. The two sessions the
        # caller did NOT name can genuinely have another caller: "default" is the shared
        # mailbox, and the auto-minted one is per CONNECTION, which several agents (a main
        # thread and its subagents on one stdio server) still share. Different remedies.
        if not _named_session(session):
            remedy = (
                "Omit session= and you get your own isolated session instead — its name comes "
                "back in every reply."
                if session == _DEFAULT_SESSION
                else "This session is yours per MCP CONNECTION, and agents sharing one "
                "connection (a subagent you spawned) share it — pass your own session= name to "
                "split off from them."
            )
            cause = (
                "Another caller shares this session (or the page redirected itself), so refs "
                f"and page state from before may be stale. {remedy}"
            )
        else:
            cause = (
                "The page most likely navigated ITSELF — an auth/route redirect, a client-side "
                "router, or a stripped #hash all do this; a named session rarely has a second "
                "caller. Refs and page state from before may be stale. (If you deliberately "
                "share this session name with another agent, it could also be them.)"
            )
        _session_drift_note[_caller_key(session)] = (
            f"NOTE: this session moved on its own since your last call — you left it on {was}, "
            f"it is now on {now}. {cause}"
        )


def _shared_session_nudge(session: str) -> str | None:
    """Said ONCE per caller, first time it ASKS for the shared session. Omitting ``session`` no
    longer lands here (it mints the caller its own), so this now fires only on an explicit
    ``session="default"`` — a caller that either wants to share or is copying an old recipe
    (#96/#98)."""
    key = _caller_key(session)
    if session != _DEFAULT_SESSION or key in _session_shared_warned:
        return None
    _session_shared_warned.add(key)
    return (
        'NOTE: "default" is a shared browser session — a concurrent caller (a subagent, another '
        "agent on this server) drives the same tabs, so your page can change between calls. "
        "Omit session= instead and you get your own isolated one, named in this prefix."
    )


def _session_response(session: str, body: str) -> str:
    notes = [
        n
        for n in (_session_drift_note.pop(_caller_key(session), None), _shared_session_nudge(session))
        if n
    ]
    _observe_session_url(session)  # rebaseline: this is where this call left it
    metadata = [f"[session: {session}]", *notes]
    # ERROR is a public machine-readable prefix for agents/clients; metadata may enrich the
    # response but must never hide it.
    return "\n".join([body, *metadata] if body.startswith("ERROR:") else [*metadata, body])


def _not_found(what: str) -> str:
    return f"{what} not found — run get_interactive_elements first"


def _desktop_label(win) -> str:
    return f"[window: {win.name}]"


class _SideLoop:
    """A maintenance loop that runs on its OWN thread and event loop, never the request loop.

    Background maintenance here scans the agent registry, which reads every live run's raw
    transcript — megabytes of JSONL, parsed synchronously, once a second. Scheduled on the MCP
    loop that work lands BETWEEN the client's ``initialize`` and the answer to it: the handshake
    spent 22 s of its 30 s connect budget inside one registry pass, and past that budget the
    client drops the server while every spawned agent is still TOLD interact's tools exist
    (#224 — "No such tool available"). Tool calls pay the same tax afterwards. A registry that
    grows must cost latency in its own thread, never in the one answering the client.

    ``body`` is the loop coroutine function: it takes the "keep going" predicate this handle
    flips at shutdown, which is how a thread with no cancellation gets one.
    """

    def __init__(self, name: str, body) -> None:
        self._alive = True
        self._thread = threading.Thread(
            target=lambda: asyncio.run(body(lambda: self._alive)), name=name, daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Ask the loop to end and wait briefly. Daemon thread: a wedged pass never holds
        shutdown, and the loop owns only files the OS closes for us."""
        self._alive = False
        self._thread.join(timeout)


#: What an interact tool is FOR, declared by every tool at registration (`_SessionScopedMCP.tool`)
#: and published to MCP clients in the tool's standard `_meta` (`{"category": ...}`), so a client
#: listing many tools can group them without a name-prefix guess.
ToolCategory = Literal["browser", "desktop", "vision", "media", "agents", "feedback"]


def tool_catalog() -> list[dict[str, str]]:
    """Every registered tool as {server, name, category, description} (the docstring's first
    paragraph), sorted by name — the shape `interact.data`'s `mcp_tools.json` snapshots for readers
    that must not import this server (the web server lists an agent's tools from it; `server` is
    this MCP server's own name, the `mcp__<server>__` part of what an agent calls)."""
    return sorted(
        ({"server": mcp.name, "name": tool.name, "category": (tool.meta or {})["category"], "description": " ".join((tool.description or "").strip().split("\n\n")[0].split())} for tool in mcp._tool_manager.list_tools()),
        key=lambda entry: entry["name"],
    )


def dispatchable_tools() -> frozenset[str]:
    """Every tool name this server can DISPATCH, read off the registry ``call_tool`` looks in.

    The ONE source for what ``mcp__interact__…`` names mean. Anything that LISTS interact's
    tools to a model — the agent-role projections most of all — resolves against this, so a
    listed tool and a callable tool cannot become two different sets (#224). Requires the
    ``interact.server`` package to be imported: the ``tools_*`` modules register on import.
    """
    return frozenset(tool.name for tool in mcp._tool_manager.list_tools())


def undispatchable_tools(names) -> list[str]:
    """Which of ``names`` address THIS server yet cannot be dispatched by it, in order.

    Names belonging to another MCP server, or to the harness itself (``Read``, ``Bash``), are
    not ours to judge and are passed over.
    """
    from interact.agents.policy import TOOL_PREFIX

    known = dispatchable_tools()
    return [n for n in names if n.startswith(TOOL_PREFIX) and n[len(TOOL_PREFIX):] not in known]


@asynccontextmanager
async def _lifespan(_: FastMCP) -> AsyncIterator[None]:
    from interact import live_sources
    from interact.server import sandbox
    from interact.server_registry import register_server, unregister_server

    reg = register_server()  # record pid+version so `interact doctor` can flag a stale long-lived server
    sandbox.install_teardown_handlers()  # tear the display down on SIGTERM too, not only a clean exit
    # Prices/benchmark scores are read off disk by the CLI dashboard and VS Code extension, so
    # something has to WRITE them — this server is alive whenever the user works. Off-thread,
    # best-effort: a slow/missing API never delays a tool call. Opt-out — the only outbound
    # call this server makes on its own initiative.
    if config.refresh_live_data:
        live_sources.refresh_in_background()
    # Keep every live agent's normalised stream current — a detached run's pump dies with the
    # process that started it, so without this the panel's view freezes.
    from interact.agents.run import _mirror_running_runs

    mirror = _SideLoop("interact-run-mirror", _mirror_running_runs)
    # The reaper stays HERE: it closes browser sessions owned by this loop, and touching them
    # from another one is a different bug. It never reads the registry.
    reaper = asyncio.create_task(sandbox._idle_session_reaper(config.session_idle_ttl))
    # Supervised (`interact mcp` under its upgrade relay): once another runtime is active, say
    # whether swapping this worker would lose an open browser session or sandbox.
    quiet = QuietPoint.current()
    reporter = None if quiet is None else _SideLoop("interact-upgrade-report", functools.partial(_report_quiet, quiet))
    try:
        yield
    finally:
        if reporter is not None:
            reporter.stop()
        mirror.stop()
        reaper.cancel()
        with suppress(asyncio.CancelledError):
            await reaper
        await _sessions.close_all()
        sandbox._close_sandbox()
        unregister_server(reg)


async def _report_quiet(quiet: QuietPoint, alive) -> None:
    from interact.server import sandbox  # noqa: PLC0415 - circular: interact.server.sandbox imports this module

    while alive():
        quiet.report(bool(_sessions.active()) or sandbox._sandbox is not None)
        await asyncio.sleep(0.5)


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
        "SESSIONS: omit `session` and you get your OWN isolated browser session — every reply "
        "names it in its `[session: …]` prefix, and it stays yours for this whole connection, so "
        "there is no name to invent and nothing to remember. Pass `session=\"default\"` only when "
        "you deliberately want to SHARE tabs with other callers on this connection. "
        "DESKTOP: to drive a native app, use interact's own tools — never shell out to xdotool/wmctrl. "
        "For an app that fights the window manager, runs in the background, or is GPU-rendered and "
        "screen-grabs black (Flutter/Electron/games/emulators), launch it with `launch_app(\"<cmd>\")` "
        "— just the binary/command, no env tricks needed (the sandbox forces software GL itself so a "
        "GPU app renders instead of capturing black) — and drive it via `target=\"nested:<title>\"`, an "
        "isolated, occlusion-proof display. If `launch_app` isn't in your tool list, your interact "
        "server is out of date: it moves to the newest installed version by itself once no browser "
        "session or sandbox is open (`interact upgrade` shows its state); don't fall back to raw shell automation. "
        "If sandbox launches start failing (e.g. rc=1 for every app after many launches), the display "
        "is respawned automatically on the next `launch_app`, or call `reset_sandbox` to force a clean "
        "one — keep using the sandbox, don't switch to driving the real desktop. "
        "If interact itself errors in a way that blocks you, behaves unexpectedly, or is missing a "
        "capability you needed, call `report_issue` — it sends the problem to interact's maintainers so "
        "it gets fixed. That's the channel for feedback about the tool (not about the site you automate). "
        "If its result says a prefilled issue page was opened in the browser, tell the user to press "
        "Submit there; never copy report files into repos."
    )


class _SessionScopedMCP(FastMCP):
    """FastMCP whose tool registration is the ONE seam session names resolve at.

    A session name has to be settled before a tool body runs, because the body hands it to
    `_sessions.get`, `_session_response`, `_resolve_target` and the drift helpers independently —
    resolve it in any of those and the others disagree. Registration is the only point every
    session tool passes through: `instrumented` covers 12 of them (the `session` tool itself is
    not one), so resolving there would give one caller different session names per tool.

    A parameter OPTS IN by defaulting to ``_AUTO_SESSION`` — that is the marker, not its name, so
    the `session` tool's ``name`` is covered and a future tool cannot forget to join.
    """

    @staticmethod
    def _session_params(fn) -> list[tuple[str, int | None]]:
        """Every ``_AUTO_SESSION``-defaulted parameter with the position it can also arrive at."""
        params = list(inspect.signature(fn).parameters.values())
        positional = [p.name for p in params if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD]
        return [
            (p.name, positional.index(p.name) if p.name in positional else None)
            for p in params
            if p.default == _AUTO_SESSION
        ]

    def tool(self, *args, category: ToolCategory, meta: dict | None = None, **kwargs):
        """Every interact tool names its `ToolCategory`; it travels in the tool's `_meta`."""
        register = super().tool(*args, meta={**(meta or {}), "category": category}, **kwargs)

        def decorator(fn):
            slots = self._session_params(fn)
            if not slots:
                return register(fn)

            def resolved(call_args: tuple, call_kwargs: dict) -> dict:
                for name, index in slots:
                    if index is not None and len(call_args) > index:
                        continue  # passed positionally: already a name the caller chose
                    call_kwargs[name] = _resolve_session(call_kwargs.get(name, _AUTO_SESSION))
                return call_kwargs

            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def wrapper(*a, **kw):
                    return await fn(*a, **resolved(a, kw))

            else:

                @functools.wraps(fn)
                def wrapper(*a, **kw):
                    return fn(*a, **resolved(a, kw))

            return register(wrapper)

        return decorator


mcp = _SessionScopedMCP("interact", lifespan=_lifespan, instructions=_instructions())


def instrumented(fn):
    """Per-call scaffolding shared by every dumping ``@mcp.tool``: refresh live config, open
    the invocation dump dir (``Debug.inv()`` in the body), dump the tool's return value ONCE.
    So no tool repeats ``config.refresh()``/``new_invocation_dir()``, and EVERY return path —
    early errors included — is logged, not just the happy path (measure_ui used to drop error
    returns). Applied UNDER ``@mcp.tool()`` (``functools.wraps`` preserves the signature
    FastMCP introspects for the tool schema); the tool body keeps its own ``dump_input``."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        config.refresh()  # ~/.interact/config.env is source of truth: pick up live edits per call
        # Did a concurrent caller move this session while we were away? Checked here so EVERY
        # session tool reports it, not just get_page_state (#96/#98/#99/#101).
        if not kwargs.get("target"):
            _check_session_drift(_resolve_session(kwargs.get("session") or _AUTO_SESSION))
        inv = Debug.new_invocation_dir(kwargs.get("debug_dir"), fn.__name__)
        token = _CURRENT_INV.set(inv)
        _responded = False  # did _session_response run? (NOT 'did this succeed')
        try:
            result = await fn(*args, **kwargs)
            _responded = True
            Debug.dump_output(inv, result)
            return result
        except (CaptureError, VisionError) as e:
            # A capture that can't produce pixels, or a provider that refused/never answered, is
            # an ANSWER ("that window is gone, try target='screen'") not a transport failure. As
            # an exception, an agent checking for the "ERROR:" prefix won't find one — every
            # other failure here is a readable string. Converted at the one seam every tool
            # passes through, so no capture/page-query tool can forget (#124/#125: page-query
            # path had no fallback chain around it).
            #
            # `_responded` stays False: body raised BEFORE `_session_response`, so baseline
            # never refreshed and drift note never delivered — the state `finally` below exists
            # to settle. Setting it True here would skip that cleanup. A VisionError from a
            # browser page query is the failure that cleanup was waiting for.
            result = f"ERROR: {e}"
            Debug.dump_output(inv, result)
            return result
        finally:
            _CURRENT_INV.reset(token)
            # A call that RAISED never ran _session_response: baseline never refreshed, drift
            # note never delivered. Left alone, that note surfaces in the NEXT call describing a
            # move from before the failed call (#95). Settle both here: drop the undeliverable
            # note and rebaseline to where the session actually is.
            if not kwargs.get("target") and not _responded:
                session_name = _resolve_session(kwargs.get("session") or _AUTO_SESSION)
                _session_drift_note.pop(_caller_key(session_name), None)
                _observe_session_url(session_name)

    return wrapper


def main():
    mcp.run(transport="stdio")
