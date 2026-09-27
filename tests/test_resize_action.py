"""#84: a native `resize` action for a desktop/nested window post-launch.

Before this, there was no way to resize a desktop/nested window post-launch — the reporter
shelled out to `xdotool windowsize`. `resize` is desktop-only; on the browser the equivalent is
`emulate_device`, and the action must point the caller there rather than silently no-op.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import TypeAdapter, ValidationError

from interact.actions.dispatch import _run_actions_browser
from interact.actions.models import DESKTOP_ONLY_ACTIONS, AnyAction, ResizeAction
from interact.desktop import DesktopWindow
from interact.server import _run_actions_desktop

adapter = TypeAdapter(list[AnyAction])


@pytest.fixture
def desktop_spies():
    """A desktop window whose input/geometry calls are observable, with the ATSPI state diff
    stubbed out (no live session in unit tests)."""
    with (
        patch.object(DesktopWindow, "click", new_callable=AsyncMock) as click,
        patch.object(DesktopWindow, "resize", new_callable=AsyncMock, create=True) as resize,
        patch("interact.actions.dispatch.DesktopState") as state,
    ):
        state.capture.return_value = None
        yield click, resize


def test_resize_in_the_action_union():
    [action] = adapter.validate_python([{"type": "resize", "width": 800, "height": 600}])
    assert isinstance(action, ResizeAction)
    assert (action.width, action.height) == (800, 600)


@pytest.mark.parametrize("kwargs", [{"width": 0, "height": 600}, {"width": 800, "height": -1}])
def test_resize_rejects_non_positive_dimensions(kwargs):
    with pytest.raises(ValidationError):
        ResizeAction(**kwargs)


def test_resize_is_desktop_only():
    assert "resize" in DESKTOP_ONLY_ACTIONS


@pytest.mark.asyncio
async def test_desktop_resize_calls_the_window_and_reports_before_after(desktop_spies):
    _, resize = desktop_spies
    win = DesktopWindow(name="app", wid=42, w=1200, h=800, x=0, y=0)

    async def _shrink(w, h):
        win.w, win.h = w, h
        return True

    resize.side_effect = _shrink
    report = await _run_actions_desktop(win, [ResizeAction(width=900, height=600)], None)
    resize.assert_awaited_once_with(900, 600)
    assert "1200x800" in report and "900x600" in report


@pytest.mark.asyncio
async def test_desktop_resize_reports_a_refusal(desktop_spies):
    _, resize = desktop_spies
    resize.return_value = False
    win = DesktopWindow(name="screen", wid=0, w=1920, h=1080, x=0, y=0)
    report = await _run_actions_desktop(win, [ResizeAction(width=900, height=600)], None)
    assert "cannot be resized" in report


@pytest.mark.asyncio
async def test_browser_rejects_resize_and_points_at_emulate_device():
    from interact.actions.dispatch import _run_actions_browser

    mgr = MagicMock()
    mgr.active_tab = 0
    mgr.get_page = AsyncMock(return_value=MagicMock())
    mgr.drain_dialog_log.return_value = []
    with (
        patch("interact.server._capture", new_callable=AsyncMock) as capture,
        patch("interact.server._session_response", side_effect=lambda s, r: r),
    ):
        capture.return_value = MagicMock(title="t", url="u", visible_text="v")
        report = await _run_actions_browser(
            mgr, [ResizeAction(width=900, height=600)], None, None, None, "default"
        )
    assert "desktop-only" in report and "emulate_device" in report
