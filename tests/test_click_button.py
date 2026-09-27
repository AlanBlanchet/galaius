"""#91: `ClickAction.button` (right/middle click) reaches BOTH surfaces and the step report.

`ClickAction` had no `button`, so a desktop app's right-click-only context menu was unreachable
and a browser right-click impossible. The button must reach both the desktop dispatcher (mapped
to its OS click code) and the browser (Playwright's `button=` kwarg), and be NAMED in the step
report when it isn't left.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from interact.actions.models import ClickAction, ClickElementAction, _click_selector
from interact.desktop import DesktopWindow
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


def test_click_button_defaults_to_left():
    assert ClickAction(x=1, y=2).button == "left"


@pytest.mark.parametrize("button", ["left", "right", "middle"])
def test_click_accepts_every_button(button):
    assert ClickAction(x=1, y=2, button=button).button == button


def test_click_rejects_an_unknown_button():
    with pytest.raises(ValidationError):
        ClickAction(x=1, y=2, button="fourth")


@pytest.mark.parametrize("button, code", [("left", 1), ("middle", 2), ("right", 3)])
@pytest.mark.asyncio
async def test_desktop_click_sends_the_mapped_button_code(desktop_spies, button, code):
    click, _ = desktop_spies
    win = DesktopWindow(name="app", wid=42, w=1200, h=800, x=0, y=0)
    await _run_actions_desktop(win, [ClickAction(x=10, y=20, button=button)], None)
    click.assert_awaited_once_with(10, 20, code, count=1)


@pytest.mark.parametrize(
    "button, verb", [("left", "clicked"), ("middle", "middle-clicked"), ("right", "right-clicked")]
)
@pytest.mark.asyncio
async def test_desktop_click_report_names_a_non_left_button(desktop_spies, button, verb):
    win = DesktopWindow(name="app", wid=42, w=1200, h=800, x=0, y=0)
    report = await _run_actions_desktop(win, [ClickAction(x=10, y=20, button=button)], None)
    assert verb in report


@pytest.mark.asyncio
async def test_desktop_click_element_still_defaults_to_left(desktop_spies):
    """click_element carries no `button` field — it must not break the desktop click call."""
    from interact.desktop.element import DesktopElement

    click, _ = desktop_spies
    win = DesktopWindow(name="app", wid=44, w=1200, h=800, x=0, y=0)
    DesktopElement.store(44, [DesktopElement(index=1, ref="e1", role="button", name="Go",
                                             x=100, y=100, w=40, h=20)])
    try:
        await _run_actions_desktop(win, [ClickElementAction(element=1)], None)
    finally:
        DesktopElement.invalidate(44)
    click.assert_awaited_once_with(120, 110, 1, count=1)


def _fake_page():
    page = MagicMock()
    page.mouse.click = AsyncMock()
    locator = MagicMock()
    locator.click = AsyncMock()
    locator.count = AsyncMock(return_value=1)  # a ref click checks the node still exists (#95)
    page.locator.return_value = locator
    return page, locator


@pytest.mark.asyncio
async def test_browser_coordinate_click_passes_the_button():
    page, _ = _fake_page()
    await ClickAction(x=5, y=6, button="right").execute(page)
    page.mouse.click.assert_awaited_once_with(5, 6, button="right")


@pytest.mark.asyncio
async def test_browser_ref_click_passes_the_button():
    page, locator = _fake_page()
    await ClickAction(ref="button-1", button="middle").execute(page)
    locator.click.assert_awaited_once_with(button="middle")


@pytest.mark.asyncio
async def test_browser_selector_click_passes_the_button():
    page, locator = _fake_page()
    locator.count = AsyncMock(return_value=1)
    await _click_selector(page, "#btn", button="right")
    locator.click.assert_awaited_once_with(button="right")
