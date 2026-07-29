"""#81 + #88: a raw x,y desktop action must be LITERAL.

`_resolve_action_coords` used to SNAP a raw x,y onto any previously-detected element whose cached
box contained the point — replacing the coordinates with that element's centre AND labelling the
step as that element. After a layout change (a resize, a navigation) the cache is stale, so the
click both moved off the pixel the caller asked for and was reported as an unrelated widget.
Reported twice (#81, #88 item 2) as actively misleading.

Also #88 item 1: when an action resolves BY REF and the cached detection predates a layout change,
the step report must SAY so, instead of silently clicking whatever now sits at that ref's old box.
"""

from unittest.mock import AsyncMock, patch

import pytest

from interact.actions.models import ClickAction, HoverAction
from interact.desktop import DesktopWindow
from interact.desktop.element import DesktopElement
from interact.server import _run_actions_desktop

WID = 77
# A cached element whose box CONTAINS (130, 1180) — the snap trigger. Its centre (300, 1200) is
# what the buggy resolver used to click instead.
CACHED = DesktopElement(index=12, ref="e12", role="button", name="Canvas",
                        x=100, y=1150, w=400, h=100)


@pytest.fixture
def desktop_spies():
    with (
        patch.object(DesktopWindow, "click", new_callable=AsyncMock) as click,
        patch.object(DesktopWindow, "hover", new_callable=AsyncMock) as hover,
        patch("interact.actions.dispatch.DesktopState") as state,
    ):
        state.capture.return_value = None
        DesktopElement.store(WID, [CACHED])
        try:
            yield click, hover
        finally:
            DesktopElement.invalidate(WID)


def _stale(reason: str | None):
    """Stub `DesktopElement.detection_stale` (the main thread's geometry-drift check)."""
    return patch.object(DesktopElement, "detection_stale", staticmethod(lambda wid, win: reason),
                        create=True)


@pytest.fixture
def win():
    return DesktopWindow(name="app", wid=WID, w=1200, h=1400, x=0, y=0)


@pytest.mark.parametrize("reason", [None, "window was 1200x800 at detection, now 900x1400"])
@pytest.mark.asyncio
async def test_raw_coordinates_are_never_snapped_to_a_cached_element(desktop_spies, win, reason):
    click, _ = desktop_spies
    with _stale(reason):
        await _run_actions_desktop(win, [ClickAction(x=130, y=1180)], None)
    click.assert_awaited_once_with(130, 1180, 1)  # NOT the cached element's centre (300, 1200)


@pytest.mark.asyncio
async def test_raw_hover_is_never_snapped_either(desktop_spies, win):
    _, hover = desktop_spies
    with _stale(None):
        await _run_actions_desktop(win, [HoverAction(x=130, y=1180)], None)
    hover.assert_awaited_once_with(130, 1180)


@pytest.mark.asyncio
async def test_coordinate_report_states_the_actual_coordinates(desktop_spies, win):
    with _stale(None):
        report = await _run_actions_desktop(win, [ClickAction(x=130, y=1180)], None)
    assert "clicked at (130,1180)" in report
    assert "cursor=" in report  # the cursor-shape suffix survives


@pytest.mark.asyncio
async def test_a_fresh_cached_element_is_a_hedged_annotation_only(desktop_spies, win):
    with _stale(None):
        report = await _run_actions_desktop(win, [ClickAction(x=130, y=1180)], None)
    assert "cached detection says" in report
    assert "button" in report and "Canvas" in report
    # …but the step is still reported as a coordinate click, never as "clicked [12] button".
    assert "clicked [12]" not in report


@pytest.mark.asyncio
async def test_a_stale_detection_warns_instead_of_annotating(desktop_spies, win):
    reason = "window was 1200x800 at detection, now 1200x1400"
    with _stale(reason):
        report = await _run_actions_desktop(win, [ClickAction(x=130, y=1180)], None)
    assert reason in report
    assert "cached detection says" not in report  # a stale guess is worse than none


@pytest.mark.asyncio
async def test_no_cached_element_reports_bare_coordinates(win):
    with (
        patch.object(DesktopWindow, "click", new_callable=AsyncMock),
        patch("interact.actions.dispatch.DesktopState") as state,
        _stale(None),
    ):
        state.capture.return_value = None
        report = await _run_actions_desktop(win, [ClickAction(x=5, y=6)], None)
    assert "clicked at (5,6)" in report
    assert "cached detection says" not in report


@pytest.mark.asyncio
async def test_a_ref_resolved_click_warns_when_the_detection_is_stale(desktop_spies, win):
    """#88 item 1: refs that predate a layout change name the WRONG widget — say so."""
    click, _ = desktop_spies
    reason = "window was 1200x800 at detection, now 1200x1400"
    with _stale(reason):
        report = await _run_actions_desktop(win, [ClickAction(ref="e12")], None)
    click.assert_awaited_once_with(CACHED.center_x, CACHED.center_y, 1)  # still clicks the ref
    assert reason in report


@pytest.mark.asyncio
async def test_a_ref_resolved_click_is_quiet_when_the_detection_is_fresh(desktop_spies, win):
    with _stale(None):
        report = await _run_actions_desktop(win, [ClickAction(ref="e12")], None)
    assert "clicked [12] button: 'Canvas'" in report
    assert "stale" not in report
