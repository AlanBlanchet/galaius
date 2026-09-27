"""#116: `double_click` is the click primitive with `count=2`, on every surface.

`double_click` was refused on a desktop/nested target as browser-only, and two rapid `click`s
don't coalesce into an OS-level dblclick. It is the click primitive with `count=2` — same
targeting (x,y / ref / element), same `button`, reported as "double-clicked". Validation is
pure here; the live proof that it (like `select_text`) actually opens a DOM Selection lives in
`test_select_text.py` (#32, the shared selection-gated-control behaviour).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from interact.actions import DoubleClickAction
from interact.actions.dispatch import _run_actions_browser
from interact.actions.models import BROWSER_ONLY_ACTIONS
from interact.desktop import DesktopWindow
from interact.desktop.element import DesktopElement
from interact.server import _run_actions_desktop


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


def _fake_page():
    page = MagicMock()
    page.mouse.click = AsyncMock()
    locator = MagicMock()
    locator.click = AsyncMock()
    locator.count = AsyncMock(return_value=1)  # a ref click checks the node still exists (#95)
    page.locator.return_value = locator
    return page, locator


@pytest.mark.parametrize(
    "kwargs,ok",
    [
        ({"ref": "e1"}, True),
        ({"selector": "#ed"}, True),
        ({"x": 10, "y": 20}, True),
        ({}, False),
        ({"x": 10}, False),  # x without y
    ],
)
def test_double_click_validation(kwargs, ok):
    if ok:
        DoubleClickAction(**kwargs)
    else:
        with pytest.raises(ValueError):
            DoubleClickAction(**kwargs)


def test_double_click_is_no_longer_browser_only():
    assert "double_click" not in BROWSER_ONLY_ACTIONS
    assert "select_text" in BROWSER_ONLY_ACTIONS  # its DOM-selection sibling (#32) still is


@pytest.mark.parametrize(
    "button, code, verb",
    [("left", 1, "double-clicked"), ("right", 3, "right-double-clicked")],
)
@pytest.mark.asyncio
async def test_desktop_double_click_is_a_counted_click_with_the_button(
    desktop_spies, button, code, verb
):
    click, _ = desktop_spies
    win = DesktopWindow(name="app", wid=42, w=1200, h=800, x=0, y=0)
    report = await _run_actions_desktop(
        win, [DoubleClickAction(x=10, y=20, button=button)], None
    )
    click.assert_awaited_once_with(10, 20, code, count=2)
    assert f"{verb} at (10,20)" in report
    assert "browser-only" not in report


@pytest.mark.asyncio
async def test_desktop_double_click_resolves_a_ref_through_the_element_map(desktop_spies):
    """A ref resolves as click_element's does — the stored element map, never a second lookup."""
    click, _ = desktop_spies
    win = DesktopWindow(name="app", wid=45, w=1200, h=800, x=0, y=0)
    DesktopElement.store(45, [DesktopElement(index=1, ref="e1", role="button", name="Go",
                                             x=100, y=100, w=40, h=20)])
    try:
        report = await _run_actions_desktop(win, [DoubleClickAction(ref="e1")], None)
    finally:
        DesktopElement.invalidate(45)
    click.assert_awaited_once_with(120, 110, 1, count=2)
    assert "double-clicked [1] button: 'Go'" in report


@pytest.mark.asyncio
async def test_browser_double_click_by_ref_stays_a_double_click():
    """Browser refs are `e{N}`, which click normalizes to an element index and routes through the
    runner's element branch — a double_click taking that branch must still land as ONE dblclick
    (Playwright's click_count=2), never as the single click the branch used to hardcode."""
    page, locator = _fake_page()
    mgr = MagicMock()
    mgr.active_tab = 0
    mgr.get_page = AsyncMock(return_value=page)
    mgr.drain_dialog_log.return_value = []
    mgr.get_element.return_value = MagicMock(ref="e3", playwright_ref='[data-interact-ref="e3"]')
    state = MagicMock(title="t", url="u", visible_text="v")
    with (
        patch("interact.server._capture", new_callable=AsyncMock, return_value=state),
        patch("interact.server._session_response", side_effect=lambda s, r: r),
        patch("interact.actions.dispatch._settle_and_diff", new_callable=AsyncMock,
              return_value=(state, "selection appeared")),
    ):
        report = await _run_actions_browser(
            mgr, [DoubleClickAction(ref="e3", button="right")], None, None, None, "default"
        )
    locator.click.assert_awaited_once_with(button="right", click_count=2)
    assert "right-double-clicked" in report


@pytest.mark.asyncio
async def test_browser_double_click_passes_the_button():
    page, _ = _fake_page()
    page.mouse.dblclick = AsyncMock()
    await DoubleClickAction(x=5, y=6, button="right").execute(page)
    page.mouse.dblclick.assert_awaited_once_with(5, 6, button="right")
