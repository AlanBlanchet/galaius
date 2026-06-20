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
