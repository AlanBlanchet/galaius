"""#76: run_actions scroll on a desktop/nested target ignored the action's anchor and always
scrolled at the WINDOW CENTER — over a zoomable map viewer instead of the dock the caller aimed
at (X wheel goes to the widget under the pointer, so position IS the target). A scroll must honor
x/y (and ref) like click does, falling back to center only when unanchored."""

from unittest.mock import AsyncMock, patch

import pytest

from interact.actions import ScrollAction
from interact.desktop import DesktopWindow
from interact.server import _run_actions_desktop


@pytest.fixture
def scroll_spy():
    with (
        patch.object(DesktopWindow, "scroll", new_callable=AsyncMock) as spy,
        patch("interact.actions.dispatch.DesktopState") as st,
    ):
        st.capture.return_value = None
        yield spy


@pytest.mark.asyncio
async def test_desktop_scroll_honors_the_given_coordinates(scroll_spy):
    win = DesktopWindow(name="app", wid=42, w=1200, h=800, x=0, y=0)
    await _run_actions_desktop(win, [ScrollAction(x=700, y=750, direction="down", amount=5)], None)
    scroll_spy.assert_awaited_once_with(700, 750, "down", 5)


@pytest.mark.asyncio
async def test_desktop_scroll_defaults_to_window_center(scroll_spy):
    win = DesktopWindow(name="app", wid=42, w=1200, h=800, x=0, y=0)
    await _run_actions_desktop(win, [ScrollAction(direction="up", amount=2)], None)
    scroll_spy.assert_awaited_once_with(600, 400, "up", 2)


@pytest.mark.asyncio
async def test_desktop_scroll_anchors_on_a_ref_element(scroll_spy):
    from interact.desktop.element import DesktopElement

    win = DesktopWindow(name="app", wid=43, w=1200, h=800, x=0, y=0)
    el = DesktopElement(index=3, ref="e3", role="list", name="dock", x=650, y=700, w=100, h=60)
    DesktopElement.store(43, [el])
    try:
        await _run_actions_desktop(win, [ScrollAction(ref="e3", direction="down", amount=3)], None)
    finally:
        DesktopElement.invalidate(43)
    scroll_spy.assert_awaited_once_with(700, 730, "down", 3)  # element center
