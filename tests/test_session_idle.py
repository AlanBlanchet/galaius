"""Idle browser-session auto-close: a long-lived MCP server (one per open editor window) must not
pile up Chromium instances that sit open for hours — a left-open page can spin CPU the whole time.
A session whose browser has been idle past the TTL is closed and dropped; it re-opens lazily on the
next use. An active session, or one that never opened a browser, is never touched.
"""

import time
from unittest.mock import AsyncMock

import pytest

from interact.browser import BrowserManager, SessionRegistry
from interact.config import Config


def _cfg(**kw) -> Config:
    return Config(headless=True, browser_type="chromium", **kw)


def _open_mgr(idle_for: float) -> BrowserManager:
    """A manager standing in for one with an open browser last used `idle_for` seconds ago."""
    mgr = BrowserManager(_cfg())
    mgr._browser = AsyncMock()  # a stand-in open Chromium whose .close() is awaitable
    mgr._playwright = None
    mgr._last_active = time.monotonic() - idle_for
    # Default to "nothing worth saving" so close_idle doesn't drive the real save_state path through
    # the mock context (#36). Tests that exercise the stash override this with their own state.
    mgr.save_state = AsyncMock(return_value={})
    return mgr


def test_is_idle_only_when_a_browser_is_open_and_past_ttl():
    assert _open_mgr(idle_for=1000).is_idle(900) is True
    assert _open_mgr(idle_for=0).is_idle(900) is False
    never_opened = BrowserManager(_cfg())  # _browser is None — nothing to reap
    assert never_opened.is_idle(900) is False
    assert never_opened.idle_seconds() is None


@pytest.mark.asyncio
async def test_close_idle_closes_only_stale_sessions():
    reg = SessionRegistry(_cfg())
    fresh, stale = _open_mgr(0), _open_mgr(1000)
    reg._sessions = {"default": fresh, "old": stale}
    stale_browser = stale._browser  # close() nulls _browser, so grab it first
    closed = await reg.close_idle(900)
    assert closed == ["old"]
    assert reg.active() == ["default"]
    stale_browser.close.assert_awaited_once()  # the stale browser was actually closed


@pytest.mark.asyncio
async def test_close_idle_is_a_noop_when_ttl_nonpositive():
    reg = SessionRegistry(_cfg())
    reg._sessions = {"old": _open_mgr(99999)}
    assert await reg.close_idle(0) == []
    assert reg.active() == ["old"]  # TTL<=0 disables auto-close entirely


@pytest.mark.asyncio
async def test_idle_close_stashes_login_and_reopen_restores_it():
    """#36: an idle close must not log the agent out — storage_state is stashed before close and
    handed to the next manager, which restores it lazily."""
    reg = SessionRegistry(_cfg())
    stale = _open_mgr(1000)
    stash = {"cookies": [{"name": "sid", "value": "abc"}], "_url": "https://app/x"}
    stale.save_state = AsyncMock(return_value=stash)
    reg._sessions = {"s": stale}

    await reg.close_idle(900)
    assert reg._stash["s"] == stash  # captured before the browser closed
    assert reg.active() == []

    mgr2 = reg.get("s")  # a returning agent gets a NEW manager…
    assert mgr2 is not stale
    assert mgr2._pending_state == stash  # …pre-loaded with the saved login
    assert "s" not in reg._stash  # consumed


@pytest.mark.asyncio
async def test_ensure_ready_restores_pending_state_instead_of_a_fresh_context():
    mgr = BrowserManager(_cfg())
    mgr._pending_state = {"cookies": []}
    calls: list[str] = []
    mgr.load_state = AsyncMock(side_effect=lambda s: calls.append("load"))
    mgr._ensure_browser = AsyncMock(side_effect=lambda: calls.append("ensure"))
    mgr._new_context = AsyncMock(side_effect=lambda *a, **k: calls.append("newctx"))

    await mgr.ensure_ready()
    assert calls == ["load"]  # restored login, did NOT build a fresh blank context
    assert mgr._pending_state is None  # consumed once


@pytest.mark.asyncio
async def test_stash_is_bounded():
    reg = SessionRegistry(_cfg())
    reg._stash = {f"s{i}": {"cookies": []} for i in range(SessionRegistry._MAX_STASH)}
    stale = _open_mgr(1000)
    stale.save_state = AsyncMock(return_value={"cookies": [{"name": "n"}]})
    reg._sessions = {"new": stale}
    await reg.close_idle(900)
    assert len(reg._stash) == SessionRegistry._MAX_STASH  # stayed bounded (oldest dropped)
    assert "new" in reg._stash


@pytest.mark.asyncio
async def test_use_resets_the_idle_clock():
    """A real browser: any action funnels through get_page, which must refresh last-active so the
    reaper never closes a session that's still in use."""
    mgr = BrowserManager(_cfg())
    try:
        await mgr.ensure_ready()
    except Exception as exc:  # no launchable chromium (bare CI)
        pytest.skip(f"no chromium: {exc}")
    try:
        mgr._last_active = time.monotonic() - 1000  # pretend a long idle
        await mgr.get_page()
        assert mgr.idle_seconds() < 5  # the access reset it
        assert mgr.is_idle(900) is False
    finally:
        await mgr.close()
