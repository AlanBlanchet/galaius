"""Capture freshness + input-delivery fixes:
- #19: a screenshot must not surface element refs detected on a screen that's no longer shown
  (cached refs are guarded by the live frame's content signature).
- #17: the file saved by a query screenshot is the exact frame that was analysed, even on VLM error.
- #12/#13: scroll/drag on a real-desktop window focus the window first and emit a fine, time-spread
  pointer path so a Flutter/GTK surface actually consumes the gesture.
"""

from unittest.mock import AsyncMock

import pytest

from interact.desktop import DesktopElement, DesktopWindow, _DRAG_STEPS


# --- #19: cached refs are only valid for the frame they were detected on ---


def test_cached_for_returns_refs_only_when_signature_matches():
    wid = 9911
    els = [DesktopElement(index=0, x=1, y=2, w=3, h=4, role="button", name="Quiz")]
    DesktopElement.merge_into(wid, els, "sigA")  # detected on screen A

    assert DesktopElement.cached_for(wid, "sigA"), "same frame → refs surfaced"
    assert DesktopElement.cached_for(wid, "sigB") is None, "navigated away → stale refs withheld (#19)"


# --- #17: query screenshot saves the analysed frame, even if the VLM errors ---


@pytest.mark.asyncio
async def test_media_response_saves_file_even_when_vlm_errors(monkeypatch, tmp_path):
    import interact.server as srv

    saved = {}
    monkeypatch.setattr(srv, "_save_to_path", lambda p, d: saved.update(path=p, data=d))

    async def boom(*a, **k):
        raise RuntimeError("vlm down")

    monkeypatch.setattr(srv, "_vlm", boom)
    out = str(tmp_path / "shot.png")
    with pytest.raises(RuntimeError):
        await srv._media_response(b"FRAMEBYTES", "ctx", query="what is this?", path=out)
    assert saved == {"path": out, "data": b"FRAMEBYTES"}, "file must be the analysed frame, written even on error"


# --- #12/#13: scroll focuses the window before the wheel; drag is fine + time-spread ---


def _win() -> DesktopWindow:
    return DesktopWindow(name="aino", wid=4242, x=0, y=0, w=400, h=800)


@pytest.mark.asyncio
async def test_scroll_focuses_window_before_wheel(monkeypatch):
    """A Flutter/GTK window silently drops a synthetic wheel unless the window holds focus; scroll
    must raise+focus before sending wheel buttons (#12/#13). Clicks worked already because they
    self-focus — scroll didn't."""
    win = _win()
    calls: list[str] = []
    monkeypatch.setattr(win, "_activate", AsyncMock(side_effect=lambda: calls.append("activate")))
    monkeypatch.setattr(win, "_focus", AsyncMock(side_effect=lambda: calls.append("focus")))
    monkeypatch.setattr(win, "_mousemove", AsyncMock(side_effect=lambda x, y: calls.append("move")))
    monkeypatch.setattr(win, "_clickbtn", AsyncMock(side_effect=lambda b: calls.append(f"wheel{b}")))

    await win.scroll(200, 400, "down", amount=3)

    assert calls.index("focus") < calls.index("wheel5"), "must focus before the first wheel event"
    assert calls.count("wheel5") == 3, "one wheel-down per unit of amount"


@pytest.mark.asyncio
async def test_backend_keyboard_focuses_resolved_wid_not_title():
    """On the sandbox backend, keyboard input must focus the EXACT window this DesktopWindow
    resolved to (its wid) — the same window clicks act on — never re-search by title, which can
    pick a hidden helper window so 'clicks work but typing doesn't' (#25)."""
    calls: list[tuple] = []

    class FakeBackend:
        def focus_wid(self, wid):
            calls.append(("focus_wid", wid))

        def focus(self, name):
            calls.append(("focus_by_name", name))

        def type_text(self, text):
            calls.append(("type", text))

    win = DesktopWindow(name="Payload", wid=99, x=0, y=0, w=400, h=800)
    win._backend = FakeBackend()
    await win.type_text("hi")

    assert ("focus_wid", 99) in calls
    assert all(c[0] != "focus_by_name" for c in calls), "must not re-resolve focus by title"
    assert calls.index(("focus_wid", 99)) < calls.index(("type", "hi")), "focus before typing"


@pytest.mark.asyncio
async def test_drag_emits_fine_time_spread_path(monkeypatch):
    """Flutter recognises a drag/fling only from a continuous, time-spread pointer path — many small
    moves (float-interpolated, not pixel-quantized) with per-step delays, not a couple of teleports
    (#12/#13)."""
    win = _win()
    moves: list[tuple[int, int]] = []
    sleeps: list[float] = []
    monkeypatch.setattr(win, "_activate", AsyncMock())
    monkeypatch.setattr(win, "_run", AsyncMock())
    monkeypatch.setattr(win, "_mousemove", AsyncMock(side_effect=lambda x, y: moves.append((x, y))))

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr("interact.desktop.asyncio.sleep", fake_sleep)
    await win.drag(200, 700, 200, 100, steps=_DRAG_STEPS)

    step_moves = moves[1:]  # first move is the initial positioning
    assert len(step_moves) >= 24, f"drag must be many small steps, got {len(step_moves)}"
    ys = [y for _, y in step_moves]
    assert ys == sorted(ys, reverse=True), "y must descend smoothly toward the target"
    assert len(set(ys)) >= 20, "float interpolation → distinct intermediate points, not quantized dupes"
    assert any(0 < d < 0.05 for d in sleeps), "per-step delay spreads the path over time for Flutter"
