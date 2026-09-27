"""Action dispatch and its errors — what run_actions does, and how it fails.

Every subject the batch runner is on the hook for:

- Native JS dialogs (#77): a click gated on a confirm() used to silently no-op because
  Playwright auto-dismisses; dialogs are now visible AND controllable via HandleDialogAction.
- Hover settle (#49): `:hover` DOES latch into a capture; the reporter's "transform: none" was a
  capture mid-transition, so hover now waits for finite CSS transitions / animations to finish
  before returning (without blocking on infinite spinners).
- Batch resilience (#138 / #139 / #140 / #147 / #149 / #162): one failing step keeps earlier
  reports and the batch keeps going; a zero-match selector embeds candidates; wait_for timeout
  attaches page state; navigate classifies unreachable vs interact-side; sleep ceiling covers a
  slow app start.
- Playwright wording that explains nothing gets rephrased with what to do next (opaque
  "execution context was destroyed" → "the page navigated, use wait_for").
- Desktop scroll (#76): scroll honours x/y (and ref) — a zoomable widget's wheel goes to the
  pointer, so position IS the target — falling back to window centre only when unanchored.
- Desktop action wait timing (#122/#133/#135): a per-action `wait` elapses before the NEXT
  capture, a selector-shaped wait refuses before sending input rather than misreading it as a
  duration, and a screenshot wait elapses before each capture in a batch.
- Action reporting & annotation (#81/#88): the report strings dispatch attaches to a completed
  action, and the containment lookup behind a coordinate action's "cached detection says" hint.
"""

import time
from unittest.mock import AsyncMock, patch

import pytest
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from interact.actions import (
    ClickAction,
    EvaluateJsAction,
    HandleDialogAction,
    HoverAction,
    ScreenshotAction,
    ScrollAction,
    WaitForAction,
)
from interact.actions.dispatch import (
    _classify_navigate_failure,
    _execute_browser_action,
    _run_actions_browser,
)
from interact.actions.models import SleepAction, _click_selector
from interact.actions.models import EvaluateJsAction as _EvalModel  # real class the monkeypatch targets
from interact.browser import BrowserManager
from interact.desktop import DesktopWindow
from interact.server import _run_actions_desktop

from tests.support import browser_manager, ready_or_skip


# =============================================================================================
# Dialogs (#77)
# =============================================================================================


_DIALOG_PAGE = """
<button id="go" onclick="window.__ok = window.confirm('Vraiment supprimer ?')">del</button>
<button id="ask" onclick="window.__name = window.prompt('Nom ?')">ask</button>
"""


async def _dialog_page(mgr: BrowserManager):
    await ready_or_skip(mgr)
    page = await mgr.get_page()
    await page.set_content(_DIALOG_PAGE)
    return page


@pytest.mark.asyncio
async def test_unhandled_dialog_is_dismissed_but_reported():
    mgr = browser_manager()
    try:
        page = await _dialog_page(mgr)
        out = await _run_actions_browser(
            mgr, [ClickAction(selector="#go")], None, None, None, "default"
        )
        assert await page.evaluate("() => window.__ok") is False  # dismissed, as before
        assert "Vraiment supprimer ?" in out and "dismiss" in out  # ...but now VISIBLE
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_handle_dialog_accepts_the_next_confirm():
    mgr = browser_manager()
    try:
        page = await _dialog_page(mgr)
        out = await _run_actions_browser(
            mgr,
            [HandleDialogAction(action="accept"), ClickAction(selector="#go")],
            None, None, None, "default",
        )
        assert await page.evaluate("() => window.__ok") is True
        assert "accept" in out and "Vraiment supprimer ?" in out
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_handle_dialog_answers_a_prompt():
    mgr = browser_manager()
    try:
        page = await _dialog_page(mgr)
        await _run_actions_browser(
            mgr,
            [HandleDialogAction(action="accept", prompt_text="Eloise"),
             ClickAction(selector="#ask")],
            None, None, None, "default",
        )
        assert await page.evaluate("() => window.__name") == "Eloise"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_arming_is_one_shot():
    mgr = browser_manager()
    try:
        page = await _dialog_page(mgr)
        await _run_actions_browser(
            mgr,
            [HandleDialogAction(action="accept"), ClickAction(selector="#go"),
             ClickAction(selector="#go")],
            None, None, None, "default",
        )
        assert await page.evaluate("() => window.__ok") is False  # 2nd dialog → default dismiss
    finally:
        await mgr.close()


# =============================================================================================
# Hover settle (#49)
# =============================================================================================


@pytest.mark.asyncio
async def test_hover_settles_transition_so_an_immediate_capture_sees_the_final_state():
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            "<style>#b{display:inline-block;width:50px;height:50px;background:#39c;"
            "transition:transform .5s} #b:hover{transform:scale(1.2)}</style><div id=b></div>"
        )
        await HoverAction(selector="#b").execute(page)
        # No sleep: settle_animations already waited for the .5s transition to finish.
        t = await page.evaluate("() => getComputedStyle(document.getElementById('b')).transform")
        assert t == "matrix(1.2, 0, 0, 1.2, 0, 0)"  # the FINAL scale, not a mid-transition matrix
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_hover_does_not_block_on_an_infinite_animation():
    """A spinner (infinite animation) must not hold hover for the full settle timeout — infinite
    animations are filtered out, so the settle returns promptly."""
    mgr = browser_manager()
    await ready_or_skip(mgr)
    try:
        page = await mgr.get_page()
        await page.set_content(
            "<style>@keyframes s{to{transform:rotate(360deg)}}"
            "#b{width:30px;height:30px;background:#c33;animation:s 1s linear infinite}</style>"
            "<div id=b></div>"
        )
        t0 = time.monotonic()
        await HoverAction(x=10, y=10).execute(page)
        assert time.monotonic() - t0 < 0.7  # did NOT wait the ~1s settle timeout on the spinner
    finally:
        await mgr.close()


# =============================================================================================
# Batch resilience (#138 / #139 / #140 / #147 / #149 / #162)
# =============================================================================================


class _Boom:
    """Playwright-shaped stand-in for a step that raises during execute()."""
    type = "navigate"
    url = "https://example.invalid/"

    def __init__(self, message: str, *, timeout: bool = False):
        self._message = message
        self._timeout = timeout

    async def execute(self, page):
        if self._timeout:
            raise PlaywrightTimeout(self._message)
        raise PlaywrightError(self._message)


@pytest.mark.asyncio
async def test_a_failing_step_keeps_earlier_reports_and_the_batch_keeps_going():
    mgr = browser_manager()
    try:
        await ready_or_skip(mgr)
        page = await mgr.get_page()
        await page.set_content("<h1>hi</h1>")
        out = await _run_actions_browser(
            mgr,
            [
                ScreenshotAction(),
                EvaluateJsAction(script="throw new Error('boom')"),
                ScreenshotAction(),
            ],
            None, None, None, "default",
        )
        assert "Step 1" in out  # earlier result survived (#139)
        assert "Step 2" in out and "ERROR" in out and "boom" in out  # failing step reports, not raises
        assert "Step 3" in out  # batch kept going after the failure (#138)
        assert "Final state" in out  # tail summary still built, not lost with the exception
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_zero_match_selector_embeds_closest_candidates_not_a_separate_call():
    mgr = browser_manager()
    try:
        await ready_or_skip(mgr)
        page = await mgr.get_page()
        await page.set_content('<button id="save">Save changes</button>')
        with pytest.raises(ValueError) as exc:
            await _click_selector(page, "#does-not-exist")
        msg = str(exc.value)
        assert "0 matched" in msg
        assert "Closest candidates" in msg
        assert "Save changes" in msg  # the actual candidate, embedded — not a pointer to go look
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_wait_for_timeout_attaches_the_final_page_state():
    mgr = browser_manager()
    try:
        await ready_or_skip(mgr)
        page = await mgr.get_page()
        await page.set_content("<title>Checkout</title><body>Cart is empty</body>")
        with pytest.raises(ValueError) as exc:
            await _execute_browser_action(
                WaitForAction(text="never-appears-xyz", timeout=200), page
            )
        msg = str(exc.value)
        assert "Page state:" in msg
        assert "Cart is empty" in msg  # the actual page content, not a pointer to go check it
    finally:
        await mgr.close()


@pytest.mark.parametrize(
    "message,timed_out,expect",
    [
        ("page.goto: net::ERR_NAME_NOT_RESOLVED at https://x/", False, "unreachable"),
        ("page.goto: net::ERR_CONNECTION_REFUSED at https://x/", False, "unreachable"),
        ("page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://x/", False, "unreachable"),
        ("", True, "unreachable"),  # navigation timeout
    ],
)
def test_navigate_failure_classifies_target_unreachable(message, timed_out, expect):
    msg = _classify_navigate_failure("https://x/", message, timed_out=timed_out)
    assert expect in msg
    assert "not interact" in msg


def test_navigate_failure_classifies_interact_side_separately():
    msg = _classify_navigate_failure("https://x/", "some internal playwright glitch", timed_out=False)
    assert "interact's side" in msg
    assert "unreachable" not in msg


@pytest.mark.asyncio
async def test_navigate_error_path_uses_the_classifier_not_a_generic_valueerror():
    action = _Boom("page.goto: net::ERR_CONNECTION_REFUSED at https://example.invalid/")
    with pytest.raises(ValueError, match="unreachable"):
        await _execute_browser_action(action, page=None)


def test_sleep_duration_ceiling_covers_a_slow_app_start():
    SleepAction(duration=180)  # would have been rejected under the old le=30
    with pytest.raises(ValueError):
        SleepAction(duration=301)


# =============================================================================================
# Playwright wording that explains nothing
# =============================================================================================


class _EvalBoom:
    """Same shape as _Boom, but for evaluate_js (its type controls the rewording branch)."""
    type = "evaluate_js"

    def __init__(self, message: str):
        self._message = message

    async def execute(self, page):
        raise PlaywrightError(self._message)


@pytest.mark.asyncio
async def test_a_navigation_mid_script_says_what_happened_and_what_to_do():
    action = _EvalBoom("Page.evaluate: Execution context was destroyed, most likely because of a navigation.")

    with pytest.raises(ValueError) as exc:
        await _execute_browser_action(action, page=None)

    msg = str(exc.value)
    assert "navigated" in msg, msg
    assert "wait_for" in msg, "the recovery has to be named, not left to be guessed"


@pytest.mark.asyncio
async def test_an_ordinary_failure_is_still_passed_through_trimmed():
    action = _EvalBoom("Page.evaluate: TypeError: x is not a function\n  at line 1\n  call log follows")

    with pytest.raises(ValueError, match="TypeError: x is not a function"):
        await _execute_browser_action(action, page=None)


@pytest.mark.asyncio
async def test_the_real_action_type_reaches_the_message(monkeypatch):
    """Not only the synthetic stand-in: the same path with the real model class."""

    async def boom(self, page):
        raise PlaywrightError("Execution context was destroyed, most likely because of a navigation.")

    monkeypatch.setattr(_EvalModel, "execute", boom)
    with pytest.raises(ValueError, match="evaluate_js: the page navigated"):
        await _execute_browser_action(_EvalModel(script="return 1"), page=None)


# =============================================================================================
# Desktop scroll (#76)
# =============================================================================================


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


# =============================================================================================
# Desktop action wait timing (#122 / #133 / #135)
# =============================================================================================


@pytest.mark.asyncio
async def test_desktop_waits_before_capture_after_input_and_before_final_state(monkeypatch):
    import interact.server as srv
    from interact.actions import ScreenshotAction, KeyPressAction
    from interact.actions import dispatch
    from types import SimpleNamespace

    events = []
    async def sleep(seconds):
        events.append(('wait', seconds))
    async def capture(*args, **kwargs):
        events.append(('capture', None))
        return b'png', 'captured'
    async def key(value):
        events.append(('key', value))
    win = SimpleNamespace(wid=1, name='fixture', w=800, h=600, capture=lambda: b'png', press_key=key)
    monkeypatch.setattr(dispatch.asyncio, 'sleep', sleep)
    monkeypatch.setattr(srv, '_capture_desktop', capture)
    monkeypatch.setattr(srv, '_desktop_label', lambda win: 'fixture')
    await dispatch._run_actions_desktop(win, [ScreenshotAction(wait='1s'), KeyPressAction(key='a', wait='2s')], 'final', wait='3s')
    assert events == [('wait', 1), ('capture', None), ('wait', .1), ('key', 'a'), ('wait', 2), ('wait', .1), ('wait', 3), ('capture', None)]


@pytest.mark.asyncio
async def test_desktop_selector_wait_refuses_before_sending_input(monkeypatch):
    import interact.server as srv
    from interact.actions import KeyPressAction
    from interact.actions import dispatch
    from types import SimpleNamespace

    win = SimpleNamespace(wid=1, name='fixture', w=800, h=600, capture=lambda: b'png', press_key=AsyncMock())
    monkeypatch.setattr(srv, '_desktop_label', lambda win: 'fixture')
    result = await dispatch._run_actions_desktop(win, [KeyPressAction(key='a', wait='#ready')], None)
    assert 'duration' in result
    win.press_key.assert_not_called()


@pytest.mark.asyncio
async def test_desktop_screenshot_wait_elapses_before_each_capture(monkeypatch):
    import time
    import interact.server as srv
    from interact.actions import ScreenshotAction
    from interact.actions import dispatch
    from types import SimpleNamespace

    captured = []
    async def capture(*args, **kwargs):
        captured.append(time.monotonic())
        return b'fixture', 'captured'
    win = SimpleNamespace(wid=1, name='fixture', w=800, h=600, capture=lambda: b'fixture')
    monkeypatch.setattr(srv, '_capture_desktop', capture)
    monkeypatch.setattr(srv, '_desktop_label', lambda win: 'fixture')
    before = time.monotonic()
    await dispatch._run_actions_desktop(
        win, [ScreenshotAction(wait='50ms'), ScreenshotAction(wait='50ms')], None
    )
    assert captured[0] - before >= .045
    assert captured[1] - captured[0] >= .145  # step settle (100 ms) plus next requested wait


# =============================================================================================
# Action reporting & annotation (#81 / #88)
# =============================================================================================


def test_el_report_never_leaks_coordinates(monkeypatch):
    import interact.actions.dispatch as dispatch

    monkeypatch.setattr(dispatch, "_fmt_cursor", lambda win=None: "default")

    class _El:
        index, role, name, center_x, center_y = 2, "button", "Submit", 137, 451

    report = dispatch._el_report("clicked", _El())
    assert "[2]" in report and "Submit" in report  # ref + name shown
    assert "137" not in report and "451" not in report  # pixel coords NOT leaked


def test_xy_report_states_the_coordinates_it_acted_on(monkeypatch):
    """A coordinate action is reported factually — no prescriptive 'use refs instead' nudge
    (it fights coordinate-capable agents). It DOES state the coordinates: they are the agent's
    own literal input, and omitting them left it unable to tell where the click landed (#81)."""
    import interact.actions.dispatch as dispatch

    monkeypatch.setattr(dispatch, "_fmt_cursor", lambda win=None: "default")
    report = dispatch._xy_report("clicked", 137, 451)
    assert "(137,451)" in report
    assert "hint" not in report.lower()


def test_element_at_finds_the_smallest_containing_box():
    """`_element_at` is the lookup behind the HEDGED "cached detection says: …" annotation on a
    coordinate action — it no longer SNAPS the click onto that element (#81/#88), so this
    covers the containment logic only. Smallest box wins (button > panel)."""
    import interact.actions.dispatch as dispatch
    from interact.desktop import DesktopElement, _element_cache

    wid = 4242
    panel = DesktopElement(index=1, role="panel", name="board", x=0, y=0, w=800, h=800)
    square = DesktopElement(index=2, role="button", name="e4", x=100, y=100, w=100, h=100)
    _element_cache[wid] = [panel, square]
    try:
        assert dispatch._element_at(wid, 150, 150).index == 2
        assert dispatch._element_at(wid, 10, 10).index == 1
        assert dispatch._element_at(wid, 900, 900) is None
    finally:
        _element_cache.pop(wid, None)
