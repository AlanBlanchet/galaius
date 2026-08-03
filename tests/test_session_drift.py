"""The shared "default" session is a shared mailbox: a concurrent caller can navigate or close
the tab out from under you, and nothing said so — you found out several calls later as a stale-ref
timeout or a page you never opened (reported four separate times in one day: #96/#98/#99/#101).

So: remember the URL a caller left a session on, and if the session has MOVED by the time that
caller comes back — something only another caller (or a self-redirect) can do — say so in the
response, on every tool, not just get_page_state.
"""

import pytest

from interact.server import core


@pytest.fixture(autouse=True)
def _clean_baselines():
    core._session_url_baseline.clear()
    core._session_shared_warned.clear()
    yield
    core._session_url_baseline.clear()


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
