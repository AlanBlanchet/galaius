"""A desktop type that never reached the field must SAY so.

#93: three separate methods (type_text by ref, click-then-type, per-character key_press) all left
a Flutter TextField's rendered text unchanged, across two different fields. Every call still
reported success — "typed N chars" — so the caller had no signal at all and had to discover the
failure by screenshotting after each attempt, then abandon the UI and hit the app's backend over
HTTP instead. interact already DETECTS this case (it verifies the field band changed and retries,
#59); it just threw the verdict away when the retries were exhausted.
"""

import asyncio

import pytest

from interact.actions import dispatch


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
