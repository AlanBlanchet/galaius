"""The shared "default" session is a shared mailbox: a concurrent caller can navigate or close
the tab out from under you, and nothing said so — you found out several calls later as a stale-ref
timeout or a page you never opened (reported four separate times in one day: #96/#98/#99/#101).

So: remember the URL a caller left a session on, and if the session has MOVED by the time that
caller comes back — something only another caller (or a self-redirect) can do — say so in the
response, on every tool, not just get_page_state.
"""

from types import SimpleNamespace

import pytest

from interact.server import core, tools_web


@pytest.fixture(autouse=True)
def _clean_baselines():
    _reset()
    yield
    _reset()


def _reset():
    core._session_url_baseline.clear()
    core._session_drift_note.clear()
    core._session_shared_warned.clear()
    core._auto_session_names.clear()
    core._sessions._sessions.clear()  # managers minted below never launched a browser


def _bind(monkeypatch, url):
    """Pretend the live session is sitting on `url` (None = no browser yet)."""
    monkeypatch.setattr(core, "_peek_session_url", lambda session: url)


def test_no_note_when_the_session_has_not_moved(monkeypatch):
    _bind(monkeypatch, "https://app.test/a")
    core._observe_session_url("default")           # end of call 1
    core._check_session_drift("default")           # start of call 2 — same URL
    assert "moved" not in core._session_response("default", "body")


def test_a_concurrent_caller_moving_the_session_is_reported(monkeypatch):
    _bind(monkeypatch, "https://app.test/mine")
    core._observe_session_url("default")           # I left it here
    _bind(monkeypatch, "https://app.test/someone-elses")
    core._check_session_drift("default")           # ...and it moved without me

    out = core._session_response("default", "body")
    assert "https://app.test/mine" in out and "https://app.test/someone-elses" in out
    assert "another caller" in out.lower()


def test_the_note_nudges_toward_a_named_session_only_for_default(monkeypatch):
    _bind(monkeypatch, "a")
    core._observe_session_url("default")
    _bind(monkeypatch, "b")
    core._check_session_drift("default")
    assert "session=" in core._session_response("default", "body")  # how to stop it happening

    _bind(monkeypatch, "a")
    core._observe_session_url("critic1")
    _bind(monkeypatch, "b")
    core._check_session_drift("critic1")
    out = core._session_response("critic1", "body")
    assert "moved" in out and "session=" not in out  # already named — no nudge to repeat


def test_the_note_fires_once_then_clears(monkeypatch):
    _bind(monkeypatch, "a")
    core._observe_session_url("default")
    _bind(monkeypatch, "b")
    core._check_session_drift("default")
    assert "moved" in core._session_response("default", "body")
    assert "moved" not in core._session_response("default", "body")  # not repeated forever


def test_a_session_with_no_browser_yet_is_silent(monkeypatch):
    _bind(monkeypatch, None)
    core._observe_session_url("default")
    core._check_session_drift("default")
    assert "moved" not in core._session_response("default", "body")


def test_the_body_and_session_header_are_preserved(monkeypatch):
    _bind(monkeypatch, None)
    core._session_response("default", "warm-up")  # spend the one-shot shared-session nudge
    assert core._session_response("default", "the body") == "[session: default]\nthe body"


# ── the nudge that actually prevents the contention (#96/#98) ───────────────────────────────
# Every reporter had READ the docs and still used "default" ("I should have used a uniquely-named
# session, per the docs"). An instructions blob read once at connect is not a reminder at the
# moment it matters, so the FIRST use of the shared session says so in-band, once.


def test_first_use_of_the_shared_session_warns_once(monkeypatch):
    _bind(monkeypatch, None)
    core._session_shared_warned.clear()
    first = core._session_response("default", "body")
    assert "shared" in first and "session=" in first
    assert "shared" not in core._session_response("default", "body")  # once, not every call
    core._session_shared_warned.clear()


def test_a_named_session_is_never_nudged(monkeypatch):
    _bind(monkeypatch, None)
    core._session_shared_warned.clear()
    assert "shared" not in core._session_response("critic1", "body")
    core._session_shared_warned.clear()


# ── who actually moved it (#106) ─────────────────────────────────────────────────────────────
# The note accused "another caller" on ANY session. On a uniquely-named one that reads as a
# cross-talk bug, and a reporter burned round-trips re-verifying in throwaway session names —
# when the far likelier cause is the page navigating itself (an auth redirect, a client-side
# router, a stripped #hash). Lead with the cause that actually fits the session's shape.


def test_a_named_session_blames_the_page_not_a_phantom_caller(monkeypatch):
    # The reported shape: a uniquely-named session whose #hash was stripped by the app itself.
    _bind(monkeypatch, "http://127.0.0.1:3000/#earn")
    core._observe_session_url("lenders-audit")
    _bind(monkeypatch, "http://127.0.0.1:3000/")
    core._check_session_drift("lenders-audit")
    out = core._session_response("lenders-audit", "body").lower()
    assert "moved" in out
    assert "itself" in out and "hash" in out  # the cause that actually fits is named first
    assert "another caller shares this session" not in out  # never asserted as fact


def test_the_default_session_still_names_the_shared_mailbox(monkeypatch):
    _bind(monkeypatch, "https://app.test/mine")
    core._observe_session_url("default")
    _bind(monkeypatch, "https://app.test/someone-elses")
    core._check_session_drift("default")
    out = core._session_response("default", "body").lower()
    assert "another caller shares this session" in out  # the shared session genuinely has this cause


# ── the stale "from" after an error (#95, third part) ────────────────────────────────────────
# The baseline was rebaselined only inside _session_response, i.e. only when a call RETURNED. A
# call that RAISED left the baseline pointing at wherever the session was before it — so the next
# call's note reported drift "from" a page the caller had actually left calls ago.


@pytest.mark.asyncio
async def test_a_call_that_raises_still_rebaselines(monkeypatch):
    @core.instrumented
    async def boom(session: str = "default"):
        raise RuntimeError("tool blew up")

    _bind(monkeypatch, "https://app.test/one")
    core._observe_session_url("default")          # call 1 ended here

    _bind(monkeypatch, "https://app.test/two")    # the failing call navigated, then raised
    with pytest.raises(RuntimeError):
        await boom(session="default")

    # Call 2 starts where the session actually IS — no phantom drift from a pre-error page...
    core._check_session_drift("default")
    assert "moved" not in core._session_response("default", "body")
    # ...because the baseline genuinely advanced, not because the note was merely swallowed:
    # a REAL move after the failed call is still reported, and from the right place.
    _bind(monkeypatch, "https://app.test/three")
    core._check_session_drift("default")
    out = core._session_response("default", "body")
    assert "https://app.test/two" in out and "https://app.test/three" in out


# ── the name the caller never has to invent (#96/#98/#99/#101) ────────────────────────────────
# The nudges above were the best a shared default allowed: they could only ASK the agent to make
# a unique name up. An omitted `session` now means "mint me my own" — derived from the MCP
# connection, reported in the `[session: ...]` prefix every reply already carried, stable for the
# life of that connection. An EXPLICIT session="default" still selects the shared mailbox.


class _Connection:
    """Stands in for one MCP connection — what FastMCP hands back as ``Context.session``."""


def _connect(monkeypatch, conn):
    """Speak to the server as `conn` from here on (FastMCP has no live request in tests)."""
    monkeypatch.setattr(core.mcp, "get_context", lambda: SimpleNamespace(session=conn))


def _session_of(reply: str) -> str:
    """The session a reply says it acted on — the prefix the agent plugs back in."""
    line = next(ln for ln in reply.splitlines() if ln.startswith("[session: "))
    return line[len("[session: "): -1]


@pytest.mark.asyncio
async def test_two_connections_each_get_their_own_minted_session(monkeypatch):
    _bind(monkeypatch, None)
    _connect(monkeypatch, _Connection())
    mine = _session_of(await tools_web.get_logs(source="network"))
    _connect(monkeypatch, _Connection())
    theirs = _session_of(await tools_web.get_logs(source="network"))

    assert mine.startswith("caller-") and theirs.startswith("caller-")
    assert mine != theirs                                  # no shared mailbox, nothing invented
    assert sorted(core._sessions.active()) == sorted([mine, theirs])  # two real browser sessions


@pytest.mark.asyncio
async def test_one_connection_gets_one_name_across_different_tools(monkeypatch):
    _bind(monkeypatch, None)
    conn = _Connection()
    _connect(monkeypatch, conn)

    first = _session_of(await tools_web.get_logs(source="network"))
    assert _session_of(await tools_web.get_logs(source="console")) == first
    # ...and a tool that names the parameter differently (`name`) resolves to the same session:
    assert f"{first} (yours)" in await tools_web.session(action="list")

    _connect(monkeypatch, conn)  # same connection object, later call
    assert _session_of(await tools_web.get_logs(source="network")) == first


@pytest.mark.parametrize(
    "passed, expected, shared",
    [
        ({}, None, False),                          # omitted -> this caller's own session
        ({"session": "default"}, "default", True),  # explicit -> the shared mailbox, warned once
        ({"session": "critic-1"}, "critic-1", False),
    ],
    ids=["omitted", "explicit-default", "named"],
)
@pytest.mark.asyncio
async def test_what_each_session_argument_resolves_to(monkeypatch, passed, expected, shared):
    _bind(monkeypatch, None)
    _connect(monkeypatch, _Connection())

    reply = await tools_web.get_logs(source="network", **passed)
    assert _session_of(reply) == (expected or core._auto_session_name())
    assert ("shared browser session" in reply) is shared  # the nudge is now opt-in, not the norm


@pytest.mark.asyncio
async def test_an_explicit_default_still_shares_one_browser(monkeypatch):
    _bind(monkeypatch, None)
    _connect(monkeypatch, _Connection())
    assert _session_of(await tools_web.get_logs(source="network", session="default")) == "default"
    _connect(monkeypatch, _Connection())
    assert _session_of(await tools_web.get_logs(source="network", session="default")) == "default"

    assert core._sessions.active() == ["default"]  # one mailbox, deliberately shared


@pytest.mark.asyncio
async def test_a_desktop_target_still_refuses_a_session_the_caller_named(monkeypatch):
    """The minted name is not a session the caller CHOSE, so it must not trip the guard that
    keeps a desktop window and a browser session from being driven by one call."""
    from interact.server import targets

    _connect(monkeypatch, _Connection())
    monkeypatch.setattr(targets, "_find_desktop_window", lambda title: "ERROR: no such window")

    _, _, auto_err = targets._resolve_target("Some Window", core._auto_session_name())
    assert auto_err == "ERROR: no such window"  # reached window resolution, was not refused

    _, _, named_err = targets._resolve_target("Some Window", "critic-1")
    assert "Cannot combine" in named_err


@pytest.mark.parametrize(
    "session, remedy",
    [
        ("default", "Omit session="),          # asked for the mailbox: stop asking
        (None, "pass your own session="),      # the minted one: still one per CONNECTION
    ],
    ids=["explicit-default", "auto-minted"],
)
def test_drift_on_a_session_the_caller_never_named_still_suspects_another_caller(
    monkeypatch, session, remedy
):
    """Minting a name per connection does not make the session single-caller: agents sharing one
    MCP connection (a spawned subagent) share it, so the shared-mailbox cause still fits."""
    _connect(monkeypatch, _Connection())
    session = session or core._auto_session_name()

    _bind(monkeypatch, "https://app.test/mine")
    core._observe_session_url(session)
    _bind(monkeypatch, "https://app.test/someone-elses")
    core._check_session_drift(session)

    out = core._session_response(session, "body")
    assert "another caller shares this session" in out.lower()
    assert remedy in out
