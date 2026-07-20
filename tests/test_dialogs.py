"""Native JS dialogs (#77): Playwright auto-dismisses an unhandled confirm/alert/prompt, so a
click whose action is confirm()-gated silently no-ops — the worst default for admin UIs full of
destructive-action confirms. Every dialog is now (a) VISIBLE in the step output (message + what
was done with it), and (b) controllable: a `handle_dialog` step arms the NEXT dialog to be
accepted/dismissed, with `prompt_text` answering a prompt()."""

import pytest

from interact.actions import ClickAction, HandleDialogAction
from interact.actions.dispatch import _run_actions_browser
from interact.browser import BrowserManager
from interact.config import Config

_PAGE = """
<button id="go" onclick="window.__ok = window.confirm('Vraiment supprimer ?')">del</button>
<button id="ask" onclick="window.__name = window.prompt('Nom ?')">ask</button>
"""


def _mgr() -> BrowserManager:
    return BrowserManager(Config(headless=True, browser_type="chromium"))


async def _ready(mgr: BrowserManager):
    try:
        await mgr.ensure_ready()
    except Exception as exc:  # no browser provisioned (bare CI)
        pytest.skip(f"no launchable chromium: {exc}")
    page = await mgr.get_page()
    await page.set_content(_PAGE)
    return page


@pytest.mark.asyncio
async def test_unhandled_dialog_is_dismissed_but_reported():
    mgr = _mgr()
    try:
        page = await _ready(mgr)
        out = await _run_actions_browser(
            mgr, [ClickAction(selector="#go")], None, None, None, "default"
        )
        assert await page.evaluate("() => window.__ok") is False  # dismissed, as before
        assert "Vraiment supprimer ?" in out and "dismiss" in out  # ...but now VISIBLE
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_handle_dialog_accepts_the_next_confirm():
    mgr = _mgr()
    try:
        page = await _ready(mgr)
        out = await _run_actions_browser(
            mgr,
            [HandleDialogAction(action="accept"), ClickAction(selector="#go")],
            None, None, None, "default",
        )
        assert await page.evaluate("() => window.__ok") is True
        assert "accept" in out and "Vraiment supprimer ?" in out
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_handle_dialog_answers_a_prompt():
    mgr = _mgr()
    try:
        page = await _ready(mgr)
        await _run_actions_browser(
            mgr,
            [HandleDialogAction(action="accept", prompt_text="Eloise"),
             ClickAction(selector="#ask")],
            None, None, None, "default",
        )
        assert await page.evaluate("() => window.__name") == "Eloise"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_arming_is_one_shot():
    mgr = _mgr()
    try:
        page = await _ready(mgr)
        await _run_actions_browser(
            mgr,
            [HandleDialogAction(action="accept"), ClickAction(selector="#go"),
             ClickAction(selector="#go")],
            None, None, None, "default",
        )
        assert await page.evaluate("() => window.__ok") is False  # 2nd dialog → default dismiss
    finally:
        await mgr.close()
