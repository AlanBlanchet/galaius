"""Browser session lifecycle — a session stays usable after tabs die or the browser dies.

Two failure shapes:

- A page CRASH (Playwright's `crash` event: renderer OOM / killed, a heavy evaluate_js is a
  common trigger) used to leave the tab as a dead about:blank the next call silently acted on
  (#127). The crash must be recorded, `get_page` must refuse the corpse, and closing the tab
  must clear the record so a fresh tab reusing the freed `id()` never inherits a stale crash.

- A session whose tab list went EMPTY used to answer every later call with
  "Tab 0 does not exist — 0 tab(s) open" for the rest of the task (#89). Two distinct causes
  produce that same zero-page state and the agent must be told WHICH:
    (a) the browser / context DIED (crash / disconnect) — the handles are stale, relaunch;
    (b) the context is alive but has NO pages — the last tab was closed, plausibly by another
        caller since "default" is shared.

The crash tests use mocks (no browser needed). The tab-recovery tests drive real Chromium
headless (self-skip in bare CI, no VLM / key).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import interact.server as srv
from interact.actions import NavigateAction

from tests.support import browser_manager, ready_or_skip


# =============================================================================================
# Page crash — mock-only (no browser launched)
# =============================================================================================


def _fake_page(url: str = "https://example.com/heavy") -> MagicMock:
    page = MagicMock()
    page.url = url
    page.on = MagicMock()
    page.close = AsyncMock()
    page.is_closed.return_value = False
    return page


def test_attach_page_listeners_registers_a_crash_handler():
    mgr = browser_manager()
    page = _fake_page()
    mgr._attach_page_listeners(page)
    events = [call.args[0] for call in page.on.call_args_list]
    assert "crash" in events, "the page must be listened for a Playwright crash, not just dialog/console"


def test_on_crash_records_a_reason_naming_the_page():
    mgr = browser_manager()
    page = _fake_page(url="https://heavy.example/big-table")
    mgr._on_crash(page)
    reason = mgr._crashed_pages[id(page)]
    assert "crash" in reason.lower()
    assert "heavy.example" in reason


def test_on_crash_survives_unreadable_url():
    """The crashed page itself may refuse even a `.url` read — must not raise out of the handler."""
    mgr = browser_manager()
    page = MagicMock()
    type(page).url = property(lambda self: (_ for _ in ()).throw(RuntimeError("gone")))
    mgr._on_crash(page)  # must not raise
    assert id(page) in mgr._crashed_pages


@pytest.mark.asyncio
async def test_get_page_raises_on_a_crashed_tab_instead_of_returning_it():
    """Before the fix, get_page returned the dead about:blank page with no signal anything had
    happened — the next evaluate_js/screenshot silently acted on a crashed tab (#127)."""
    mgr = browser_manager()
    page = _fake_page()
    mgr._ensure_open_tab = AsyncMock()
    mgr._context = SimpleNamespace(pages=[page])

    mgr._on_crash(page)  # simulate Playwright firing the crash event on this page

    with pytest.raises(RuntimeError, match="crashed"):
        await mgr.get_page()


@pytest.mark.asyncio
async def test_get_page_unaffected_for_a_healthy_tab():
    mgr = browser_manager()
    page = _fake_page()
    mgr._ensure_open_tab = AsyncMock()
    mgr._context = SimpleNamespace(pages=[page])

    assert await mgr.get_page() is page


@pytest.mark.asyncio
async def test_close_tab_clears_the_crash_record():
    """A crash record must not outlive the tab it describes — closing it and opening a fresh tab
    that happens to reuse the same freed id() must never inherit a stale crash."""
    mgr = browser_manager()
    page = _fake_page()
    mgr._on_crash(page)
    assert id(page) in mgr._crashed_pages

    mgr._context = SimpleNamespace(pages=[page])
    mgr._active_tab = 0
    await mgr.close_tab(0)

    assert id(page) not in mgr._crashed_pages
    page.close.assert_awaited_once()


# =============================================================================================
# Tab recovery — real Chromium
# =============================================================================================


async def _close_every_tab(mgr) -> None:
    """Cause (b): another caller closed the last tab — a live context with zero pages."""
    for page in list(mgr._context.pages):
        await page.close()
    assert mgr.tab_count == 0


async def _kill_browser(mgr) -> None:
    """Cause (a): the browser process went away under the session (crash / disconnect)."""
    await mgr._browser.close()
    assert not mgr._browser.is_connected()


@pytest.mark.asyncio
async def test_get_page_opens_a_fresh_tab_when_the_last_one_was_closed():
    """(b) get_page must not raise "Tab 0 does not exist — 0 tab(s) open"; it opens a tab."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.goto("data:text/html,<title>AAA</title>")
        await _close_every_tab(mgr)

        page = await mgr.get_page()  # used to raise IndexError
        assert not page.is_closed()
        assert mgr.tab_count == 1
        assert mgr.active_tab == 0  # the stale active tab was re-based, not left pointing past the end
        await page.goto("data:text/html,<title>BBB</title>")  # the recovered page is usable
        assert "BBB" in await page.title()
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_recovery_note_names_the_closed_tab_cause_and_drains_once():
    """(b) The recovery is VISIBLE — the agent is told page state is gone, and told WHY."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        await mgr.get_page()
        await _close_every_tab(mgr)
        await mgr.get_page()

        notes = mgr.drain_recovery_notes()
        assert len(notes) == 1
        note = notes[0].lower()
        assert "no open tabs" in note  # the (b) wording: last tab closed by another caller
        assert "relaunch" not in note  # NOT reported as a crash — the context was alive
        assert mgr.drain_recovery_notes() == []  # drained once, not repeated on every later call
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_get_page_relaunches_a_dead_browser_and_says_so():
    """(a) A disconnected browser is relaunched — and the note says relaunched, not "tab closed"."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        await mgr.get_page()
        await _kill_browser(mgr)

        page = await mgr.get_page()  # used to raise IndexError on the dead context
        assert not page.is_closed()
        assert mgr._browser.is_connected()  # a NEW browser, not the corpse
        await page.goto("data:text/html,<title>CCC</title>")
        assert "CCC" in await page.title()

        note = " ".join(mgr.drain_recovery_notes()).lower()
        assert "relaunch" in note and ("crash" in note or "disconnect" in note)
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_new_tab_works_on_a_zero_tab_session():
    """new_tab is the obvious escape hatch — it must work on a 0-tab session, and open ONE tab."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        await mgr.get_page()
        await _close_every_tab(mgr)

        idx = await mgr.new_tab("data:text/html,<title>DDD</title>")
        assert idx == 0 and mgr.tab_count == 1  # exactly one tab, not a recovery tab + a new one
        assert "DDD" in await (await mgr.get_page()).title()
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_new_tab_relaunches_a_dead_browser():
    """new_tab on a session whose browser died must relaunch rather than raise a Playwright error."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        await mgr.get_page()
        await _kill_browser(mgr)

        idx = await mgr.new_tab("data:text/html,<title>EEE</title>")
        assert mgr._browser.is_connected()
        assert "EEE" in await (await mgr.get_page(idx)).title()
        assert "relaunch" in " ".join(mgr.drain_recovery_notes()).lower()
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_undrained_notes_stay_bounded_and_deduplicated():
    """A caller that never drains (a tool surface not yet wired to the note) must not grow the
    queue over a long session, and one repeated heal must not read as several distinct ones."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        for _ in range(6):
            await mgr.get_page()
            await _close_every_tab(mgr)
        await mgr.get_page()
        assert len(mgr.drain_recovery_notes()) == 1  # same heal, said once
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_a_genuinely_out_of_range_tab_still_errors():
    """Recovery heals an EMPTY session; it must not mask a caller asking for a tab that isn't there."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        await mgr.get_page()
        with pytest.raises(IndexError):
            await mgr.get_page(5)
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_recovery_note_reaches_the_tool_result(monkeypatch):
    """#89 requirement 3: the note the agent SEES. Every browser tool routes through get_page, so
    the note must ride out on the tool's own return value — never be swallowed."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        await mgr.get_page()
        await _close_every_tab(mgr)
        monkeypatch.setattr(srv.core._sessions, "get", lambda s: mgr)

        out = await srv.navigate("data:text/html,<title>FFF</title><button>go</button>")
        assert "no open tabs" in out.lower()
        assert "FFF" in out  # …and the navigation itself still succeeded

        await _close_every_tab(mgr)
        state = await srv.get_page_state()
        assert "no open tabs" in state.lower()

        assert "no open tabs" not in (await srv.get_page_state()).lower()  # once, not on every call
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_run_actions_recovers_and_reports(monkeypatch):
    """run_actions is the hot path the report died on — it must recover and carry the note too."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        await mgr.get_page()
        await _close_every_tab(mgr)
        monkeypatch.setattr(srv.targets, "_resolve_target", lambda target, session: (None, mgr, None))

        out = await srv.run_actions([NavigateAction(url="data:text/html,<title>GGG</title>")])
        assert "no open tabs" in out.lower()
        assert mgr.tab_count >= 1
    finally:
        await mgr.close()
