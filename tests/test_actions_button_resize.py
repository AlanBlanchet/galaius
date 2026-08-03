"""#91 (right/middle click), #84 (a native `resize` action) and the client-log `wait_for` bug.

- #91: `ClickAction` had no `button`, so a desktop app's right-click-only context menu was
  unreachable and a browser right-click impossible. The button must reach BOTH surfaces and be
  NAMED in the step report when it isn't left.
- #84: no way to resize a desktop/nested window post-launch — the reporter shelled out to
  `xdotool windowsize`. `resize` is desktop-only; on the browser the equivalent is emulate_device.
- client logs (2x/24h): `{"type":"wait_for","timeout":2000,"selector":null}` was a hard pydantic
  error. A bare wait_for means "pause for `timeout` ms" and works on any surface.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import TypeAdapter, ValidationError

from interact.actions.models import (
    DESKTOP_ONLY_ACTIONS,
    AnyAction,
    ClickAction,
    ClickElementAction,
    ResizeAction,
    WaitForAction,
    _click_selector,
)
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


# --- #91: the button reaches the model, both surfaces, and the report -----------------------


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
    click.assert_awaited_once_with(10, 20, code)


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
    click.assert_awaited_once_with(120, 110, 1)


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


# --- #84: a native resize action -------------------------------------------------------------


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


# --- client logs: a bare wait_for is a pause, not a validation error --------------------------


def test_bare_wait_for_is_accepted():
    action = WaitForAction(timeout=2000)
    assert action.selector is None and action.text is None


def test_wait_for_still_rejects_both_selector_and_text():
    with pytest.raises(ValidationError):
        WaitForAction(selector="#el", text="hi")


@pytest.mark.asyncio
async def test_bare_wait_for_pauses_for_the_timeout():
    with patch("interact.actions.models.asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await WaitForAction(timeout=2000).execute(MagicMock())
    sleep.assert_awaited_once_with(2.0)
    assert result == "waited 2000ms (no selector/text given)"


@pytest.mark.asyncio
async def test_bare_wait_for_runs_on_the_desktop_surface(desktop_spies):
    win = DesktopWindow(name="app", wid=42, w=1200, h=800, x=0, y=0)
    with patch("interact.actions.models.asyncio.sleep", new_callable=AsyncMock) as sleep:
        report = await _run_actions_desktop(win, [WaitForAction(timeout=500)], None)
    sleep.assert_any_await(0.5)  # the runner's own inter-step sleeps share this patched module
    assert "waited 500ms" in report
    assert "browser-only" not in report


@pytest.mark.asyncio
async def test_selector_wait_for_stays_browser_only_on_desktop(desktop_spies):
    win = DesktopWindow(name="app", wid=42, w=1200, h=800, x=0, y=0)
    report = await _run_actions_desktop(win, [WaitForAction(selector="#el")], None)
    assert "browser-only" in report
