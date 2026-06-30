"""Keyboard delivery into a toolkit field (#59): a focusing click leaves Flutter's text-input
connection not-yet-ready, so XTEST keystrokes are dropped non-deterministically. dispatch verifies
the keys registered (band-scoped pixel diff at the focus point) and re-types if they didn't."""
import io

import pytest
from PIL import Image

from interact.dispatch import _field_changed, _type_desktop


def _png(size=(500, 400), block=None) -> bytes:
    """A grayscale PNG, optionally with a black rectangle ``block`` = (x0, y0, x1, y1)."""
    img = Image.new("L", size, 255)
    if block:
        x0, y0, x1, y1 = block
        for x in range(x0, x1):
            for y in range(y0, y1):
                img.putpixel((x, y), 0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_field_changed_detects_typed_text():
    # A field that filled with glyphs (a big block in the band around the focus point) → changed.
    before = _png()
    after = _png(block=(60, 80, 240, 105))  # ~180×25 px of "text" in the band
    assert _field_changed(before, after, 150, 92) is True


def test_field_changed_ignores_caret_blink():
    # A 2-px caret toggling on is far below the threshold → NOT a change (so we never double-type).
    before = _png()
    after = _png(block=(62, 80, 64, 100))
    assert _field_changed(before, after, 150, 92) is False


def test_field_changed_identical_is_false():
    same = _png()
    assert _field_changed(same, same, 150, 92) is False


def test_field_changed_size_mismatch_is_true():
    assert _field_changed(_png((500, 400)), _png((400, 300)), 150, 92) is True


class _FakeWin:
    """Records type/clear calls; ``capture`` returns a sentinel (the real diff is monkeypatched)."""

    def __init__(self):
        self._backend = object()  # truthy → the verified-retry path
        self.typed: list[str] = []
        self.keys: list[str] = []

    def capture(self) -> bytes:
        return b""

    async def type_text(self, text: str) -> None:
        self.typed.append(text)

    async def press_key(self, key: str) -> None:
        self.keys.append(key)


@pytest.fixture
def _no_sleep(monkeypatch):
    async def _noop(_):
        return None

    monkeypatch.setattr("interact.dispatch.asyncio.sleep", _noop)


@pytest.mark.asyncio
async def test_type_desktop_single_shot_when_registered(monkeypatch, _no_sleep):
    monkeypatch.setattr("interact.dispatch._field_changed", lambda *a: True)  # landed first try
    win = _FakeWin()
    await _type_desktop(win, "hello", 150, 92)
    assert win.typed == ["hello"]  # no retry
    assert win.keys == []  # nothing cleared


@pytest.mark.asyncio
async def test_type_desktop_retries_when_dropped(monkeypatch, _no_sleep):
    seq = iter([False, True])  # dropped once, then lands
    monkeypatch.setattr("interact.dispatch._field_changed", lambda *a: next(seq))
    win = _FakeWin()
    await _type_desktop(win, "hello", 150, 92)
    assert win.typed == ["hello", "hello"]  # exactly one retry
    assert win.keys == ["ctrl+a", "Delete"]  # field cleared before the retry (no accumulation)


@pytest.mark.asyncio
async def test_type_desktop_gives_up_after_max_retries(monkeypatch, _no_sleep):
    monkeypatch.setattr("interact.dispatch._field_changed", lambda *a: False)  # never registers
    win = _FakeWin()
    await _type_desktop(win, "hi", 150, 92)
    assert win.typed == ["hi", "hi", "hi"]  # initial + 2 retries, then stops (no infinite loop)


@pytest.mark.asyncio
async def test_type_desktop_no_focus_point_is_single_shot(_no_sleep):
    # No (fx, fy) — e.g. the browser/real-display path or a bare type — must not try to diff/retry.
    win = _FakeWin()
    await _type_desktop(win, "hello", None, None)
    assert win.typed == ["hello"]
    assert win.keys == []
