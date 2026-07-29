"""A session whose tab list went EMPTY must SELF-RECOVER, visibly (#89).

Reported symptom: mid-task, after successful navigate/evaluate_js/screenshot calls, session
"default" answered every later call with "Tab 0 does not exist — 0 tab(s) open" (and, once the
active tab clamped past the end, "Tab -1 does not exist — 0 tab(s) open"). No prior error — the
session was simply wedged for the rest of the task.

Two distinct causes produce that same zero-page state, and the agent must be told WHICH:
  (a) the browser/context DIED (crash / disconnect) — the handles are stale, relaunch is needed;
  (b) the context is alive but has NO pages — the last tab was closed, plausibly by a concurrent
      caller, since "default" is shared per the tool docs.

Real Chromium (headless, no VLM/key); self-skips where no browser is provisioned.
"""

import pytest

import interact.server as srv
from interact.actions import NavigateAction
from interact.browser import BrowserManager
from interact.config import Config


def _mgr() -> BrowserManager:
    return BrowserManager(Config(headless=True, browser_type="chromium"))


async def _ready(mgr: BrowserManager) -> None:
    try:
        await mgr.ensure_ready()
    except Exception as exc:  # no browser provisioned (bare CI)
        pytest.skip(f"no launchable chromium: {exc}")


async def _close_every_tab(mgr: BrowserManager) -> None:
    """Cause (b): another caller closed the last tab — a live context with zero pages."""
    for page in list(mgr._context.pages):
        await page.close()
    assert mgr.tab_count == 0


async def _kill_browser(mgr: BrowserManager) -> None:
    """Cause (a): the browser process went away under the session (crash / disconnect)."""
    await mgr._browser.close()
    assert not mgr._browser.is_connected()


@pytest.mark.asyncio
async def test_get_page_opens_a_fresh_tab_when_the_last_one_was_closed():
    """(b) get_page must not raise "Tab 0 does not exist — 0 tab(s) open"; it opens a tab."""
    mgr = _mgr()
    await _ready(mgr)
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
    mgr = _mgr()
    await _ready(mgr)
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
    mgr = _mgr()
    await _ready(mgr)
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
    mgr = _mgr()
    await _ready(mgr)
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
    mgr = _mgr()
    await _ready(mgr)
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
    mgr = _mgr()
    await _ready(mgr)
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
    mgr = _mgr()
    await _ready(mgr)
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
    mgr = _mgr()
    await _ready(mgr)
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
    mgr = _mgr()
    await _ready(mgr)
    try:
        await mgr.get_page()
        await _close_every_tab(mgr)
        monkeypatch.setattr(srv.targets, "_resolve_target", lambda target, session: (None, mgr, None))

        out = await srv.run_actions([NavigateAction(url="data:text/html,<title>GGG</title>")])
        assert "no open tabs" in out.lower()
        assert mgr.tab_count >= 1
    finally:
        await mgr.close()
