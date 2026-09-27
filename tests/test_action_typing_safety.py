"""A typed action that delivers the wrong text — or silently swallows part of it — must SAY so
in a place the caller (an agent reading the tool result, or a test reading the schema) actually
reads.

Formerly split across `test_type_delivery_report` (#93: desktop `type_text` / click-then-type /
key_press all left a Flutter TextField unchanged and still reported success) and
`test_mode_prefix_guidance` (#114: `clear_first`'s default silently eats a one-character mode
prefix like VS Code's command-palette ">", and the tool's own MCP schema is the only place an
agent will see the warning before it burns a round-trip). Different action surfaces (desktop
band-diff retry vs browser keyboard dispatch), same failure class — kept as two labelled blocks.
"""

import asyncio
import json

import pytest

from interact.actions import dispatch


# ── Desktop: a type that never reached the field must say so (#93) ─────────────────────────


class _Win:
    """A desktop window whose field never changes — the #93 shape: focus lands, glyphs don't."""

    name = "App"

    def __init__(self, changes: bool):
        self._backend = object()
        self._changes = changes
        self.typed: list[str] = []
        self.keys: list[str] = []

    def capture(self) -> bytes:
        return b"frame"

    async def type_text(self, text: str):
        self.typed.append(text)

    async def press_key(self, key: str):
        self.keys.append(key)


@pytest.fixture
def _fast(monkeypatch):
    monkeypatch.setattr(dispatch, "_TYPE_RENDER", 0)
    monkeypatch.setattr(dispatch, "_TYPE_FOCUS_SETTLE", 0)


def test_a_type_that_never_lands_returns_a_warning(_fast, monkeypatch):
    monkeypatch.setattr(dispatch, "_field_changed", lambda *a: False)
    win = _Win(changes=False)
    warning = asyncio.run(dispatch._type_desktop(win, "hello", 100, 200))
    assert warning, "an undelivered type must report, not return silently"
    low = warning.lower()
    assert "not appear" in low or "did not" in low
    # It must be actionable: say how many attempts were made, and what to try instead.
    assert f"{dispatch._TYPE_RETRIES + 1} attempts" in warning
    assert "key_press" in warning
    assert len(win.typed) == 1 + dispatch._TYPE_RETRIES


def test_a_type_that_lands_reports_nothing(_fast, monkeypatch):
    monkeypatch.setattr(dispatch, "_field_changed", lambda *a: True)
    win = _Win(changes=True)
    assert asyncio.run(dispatch._type_desktop(win, "hello", 100, 200)) is None
    assert win.typed == ["hello"], "a landed type must never be re-sent"


def test_an_unverifiable_type_reports_nothing(_fast):
    """No focus point (a bare type with no target) → the band diff can't run, so there is nothing
    to warn about; stay quiet rather than guess."""
    win = _Win(changes=False)
    assert asyncio.run(dispatch._type_desktop(win, "hello", None, None)) is None


# ── Browser: clear_first's default silently eats a mode prefix (#114) ──────────────────────
#
# Deliberately not inferred away: preserving a leading character that "looks like" a mode would
# mean this tool carrying one editor's prefix alphabet, and would refuse to clear a field whose
# content genuinely begins with it. So the fix is that the tool SAYS so — which is only true
# while the text actually reaches the schema an agent reads, hence tests rather than a comment.


@pytest.mark.asyncio
async def test_the_mode_prefix_trap_is_in_the_schema_agents_read():
    from interact.server import mcp

    tools = await mcp.list_tools()
    run_actions = next(t for t in tools if t.name == "run_actions")
    schema = json.dumps(run_actions.inputSchema)

    assert "command palette" in schema, "the concrete case an agent will hit is not described"
    assert "clear_first=false" in schema, "the escape hatch is not named"


@pytest.mark.asyncio
async def test_clear_first_still_means_replace_the_whole_field():
    """The documented behaviour, asserted — so a later 'helpful' tweak that starts preserving a
    prefix has to change this test on purpose rather than by accident."""
    from unittest.mock import AsyncMock, MagicMock

    from interact.actions import TypeTextAction

    page = MagicMock()
    page.keyboard.press = AsyncMock()
    page.keyboard.type = AsyncMock()

    await TypeTextAction(text="Interact: Show Team").execute(page)

    page.keyboard.press.assert_awaited_once_with("ControlOrMeta+a")
    page.keyboard.type.assert_awaited_once_with("Interact: Show Team")


@pytest.mark.asyncio
async def test_clear_first_off_appends_instead():
    from unittest.mock import AsyncMock, MagicMock

    from interact.actions import TypeTextAction

    page = MagicMock()
    page.keyboard.press = AsyncMock()
    page.keyboard.type = AsyncMock()

    await TypeTextAction(text="Show Team", clear_first=False).execute(page)

    page.keyboard.press.assert_not_awaited()  # the ">" the shortcut inserted survives
    page.keyboard.type.assert_awaited_once_with("Show Team")
