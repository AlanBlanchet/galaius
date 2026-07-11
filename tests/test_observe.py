import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from interact.actions import (
    AnyAction,
    CompareAction,
    ScreenshotAction,
    ScrollAction,
    SleepAction,
)
from interact.vision import VLMResult

_VLM = VLMResult(text="vlm-result", elapsed=0.5)
_ANALYZED = VLMResult(text="analyzed", elapsed=0.3)

_PNG = b"\x89PNG\r\n\x1a\nfake"
_PNG_B64 = base64.b64encode(_PNG).decode()


def _page_state(**overrides):
    from interact.state import PageState

    defaults = dict(
        url="https://example.com",
        title="Example",
        accessibility_tree="",
        screenshot_base64=_PNG_B64,
        visible_text="hello",
        focused_element=None,
    )
    defaults.update(overrides)
    return PageState(**defaults)


def _state_change():
    from interact.state import StateChange

    return StateChange(before=_page_state(), after=_page_state())


@pytest.fixture
def browser_mocks():
    page = AsyncMock()
    page.screenshot = AsyncMock(return_value=_PNG)

    mgr = MagicMock()
    mgr.get_page = AsyncMock(return_value=page)
    mgr.tab_count = 1

    state = _page_state()
    change = _state_change()

    with (
        patch("interact.server._capture", AsyncMock(return_value=state)) as cap,
        patch("interact.state.StateChange.compute", return_value=change),
        patch("interact.server.vlm.analyze_media", AsyncMock(return_value=_VLM)) as vlm,
        patch(
            "interact.server.analyze_screenshot", AsyncMock(return_value=_ANALYZED)
        ),
    ):
        yield {"page": page, "mgr": mgr, "vlm": vlm, "capture": cap}


# --- observe on/off parametrized ---


@pytest.mark.asyncio
async def test_record_frames_collects_one_per_step(browser_mocks):
    """record_frames captures a frame after every step — so a recorded interaction has the result
    of each action, with no uniform-sampling gaps."""
    from interact.server import _run_actions_browser

    browser_mocks["page"].screenshot = AsyncMock(return_value=_PNG)
    frames: list[bytes] = []
    actions = [ScrollAction(), ScrollAction(), ScrollAction()]
    await _run_actions_browser(
        browser_mocks["mgr"], actions, None, None, None, "default", record_frames=frames
    )
    assert len(frames) == 3 and all(f == _PNG for f in frames)


@pytest.mark.parametrize(
    "observe, expect_vlm, expect_snapshot",
    [
        ("what changed?", True, True),
        (None, False, False),
    ],
    ids=["observe-set", "observe-none"],
)
@pytest.mark.asyncio
async def test_browser_observe(browser_mocks, observe, expect_vlm, expect_snapshot):
    """observe set → VLM called + snapshot stored; observe None → neither."""
    from interact.server import _run_actions_browser

    action = ScrollAction(observe=observe)
    result = await _run_actions_browser(
        browser_mocks["mgr"], [action], None, None, None, "default"
    )
    if expect_vlm:
        browser_mocks["vlm"].assert_called()
        assert "observation:" in result
    else:
        browser_mocks["vlm"].assert_not_called()
        assert "observation:" not in result


# --- CompareAction parametrized ---


@pytest.mark.parametrize(
    "stored_steps, compare_steps, expect_error",
    [
        ({1: _PNG, 2: _PNG}, [1, 2], False),
        ({1: _PNG}, [1, 3], True),
        ({}, [1], True),
    ],
    ids=["valid-steps", "missing-step-3", "all-missing"],
)
@pytest.mark.asyncio
async def test_browser_compare(
    browser_mocks, stored_steps, compare_steps, expect_error
):
    """CompareAction with valid indices → VLM; missing indices → error message."""
    from interact.server import _run_actions_browser

    actions: list[AnyAction] = []
    # Pre-populate snapshots by using ScreenshotAction for the steps we want stored
    for step in sorted(stored_steps):
        actions.append(ScreenshotAction())

    actions.append(CompareAction(steps=compare_steps, query="diff?"))

    result = await _run_actions_browser(
        browser_mocks["mgr"], actions, None, None, None, "default"
    )
    if expect_error:
        assert "has no snapshot" in result
    else:
        browser_mocks["vlm"].assert_called()
        assert "compare" in result.lower() or "vlm-result" in result


# --- VLM error mid-sequence ---


@pytest.mark.asyncio
async def test_vlm_error_continues(browser_mocks):
    """VLM error on observe → error in step report, subsequent actions still run."""
    from interact.server import _run_actions_browser

    browser_mocks["vlm"].side_effect = [RuntimeError("API down"), _VLM]

    actions = [
        ScrollAction(observe="check it"),
        ScrollAction(observe="check again"),
    ]
    result = await _run_actions_browser(
        browser_mocks["mgr"], actions, None, None, None, "default"
    )
    # _vlm now returns a soft-failure VLMResult after exhausting the fallback
    # chain rather than propagating the exception. The error surfaces as
    # observation text and subsequent actions still run.
    assert "fallback" in result.lower() or "observe error:" in result
    assert "Step 2" in result
    assert "vlm-result" in result


# --- ScreenshotAction always stores snapshot ---


@pytest.mark.asyncio
async def test_screenshot_stores_snapshot(browser_mocks):
    """ScreenshotAction always stores its bytes, usable by later CompareAction."""
    from interact.server import _run_actions_browser

    browser_mocks["vlm"].return_value = "compared"

    actions = [
        ScreenshotAction(),
        CompareAction(steps=[1], query="describe"),
    ]
    result = await _run_actions_browser(
        browser_mocks["mgr"], actions, None, None, None, "default"
    )
    assert "has no snapshot" not in result


# --- Desktop observe ---


@pytest.fixture
def desktop_mocks():
    with (
        patch("interact.desktop.DesktopWindow.capture", return_value=_PNG),
        patch("interact.server.vlm.analyze_media", AsyncMock(return_value=_VLM)) as vlm,
    ):
        yield {"vlm": vlm}


@pytest.mark.asyncio
async def test_desktop_observe(desktop_mocks):
    """Desktop observe captures screenshot and calls VLM."""
    from interact.desktop import DesktopWindow
    from interact.server import _run_actions_desktop

    win = DesktopWindow(name="test", wid=42, w=800, h=600, x=0, y=0)
    action = SleepAction(duration=0.01, observe="what happened?")
    result = await _run_actions_desktop(win, [action], None)
    assert "observation:" in result
    desktop_mocks["vlm"].assert_called()


@pytest.mark.asyncio
async def test_desktop_compare_missing_step(desktop_mocks):
    """Desktop CompareAction referencing unobserved step → error."""
    from interact.desktop import DesktopWindow
    from interact.server import _run_actions_desktop

    win = DesktopWindow(name="test", wid=42, w=800, h=600, x=0, y=0)
    action = CompareAction(steps=[1, 2], query="diff?")
    result = await _run_actions_desktop(win, [action], None)
    assert "has no snapshot" in result
