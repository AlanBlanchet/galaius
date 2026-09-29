"""`wait` accepts a DURATION on every action, not only bare seconds (#97, #63).

`wait: "8s"` worked on navigate, but `wait: "1500ms"` on a click fell through to
`Page.wait_for_selector` and threw `Unexpected token "1500ms" while parsing css selector`. One
field carrying two grammars is easy to trip over mid-batch, so every duration literal an agent
plausibly writes is parsed as a duration; a selector still means a selector. The pure grammar
lives in `_parse_wait_seconds`; `srv._wait` is the integration point that consults it (a bare
number sleeps, a selector still waits for visibility).

The `WaitForAction` step itself has the same shape: client logs (2x/24h) hit a hard pydantic
error on `{"type":"wait_for","timeout":2000,"selector":null}` — a bare `wait_for` means "pause
for `timeout` ms" on any surface, not a validation failure.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from galaius.actions.models import WaitForAction
from galaius.desktop import DesktopWindow
from galaius.server import _run_actions_desktop
from galaius.server.capture import _parse_wait_seconds


@pytest.fixture
def desktop_spies():
    """A desktop window whose input/geometry calls are observable, with the ATSPI state diff
    stubbed out (no live session in unit tests)."""
    with (
        patch.object(DesktopWindow, "click", new_callable=AsyncMock) as click,
        patch.object(DesktopWindow, "resize", new_callable=AsyncMock, create=True) as resize,
        patch("galaius.actions.dispatch.DesktopState") as state,
    ):
        state.capture.return_value = None
        yield click, resize


@pytest.fixture
def srv():
    import galaius.server as _srv
    from galaius.server import breaker

    breaker.clear()
    _srv.config.component_criteria = "cap.gui_grounding"
    with patch.object(_srv.Debug, "save"):
        yield _srv
    _srv.config.clear_overrides()  # drop the transient override so it can't leak into later tests
    breaker.clear()


@pytest.mark.parametrize(
    "text, expected",
    [
        ("3", 3.0),           # bare number = seconds (#63)
        ("8s", 8.0),
        (" 2.5s ", 2.5),
        ("1500ms", 1.5),      # the reported failure
        ("500ms", 0.5),
        ("250MS", 0.25),      # case-insensitive
        ("1m", 60.0),
        ("0.5", 0.5),
    ],
)
def test_duration_literals_parse(text, expected):
    assert _parse_wait_seconds(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    ["button", "#id", ".cls", "text=Save", "div > span", "networkidle", "", "sms", "ms"],
)
def test_selectors_and_load_states_are_not_durations(text):
    assert _parse_wait_seconds(text) is None


def test_a_negative_duration_is_not_a_duration():
    assert _parse_wait_seconds("-2s") is None  # never sleep on a nonsense value


# ── type_text into whatever is already focused (#97, second half) ────────────────────────────
# "click the field, then type" had to repeat the selector, because type_text errored without a
# ref/selector. After a click the field IS focused, so typing at the keyboard is the natural act.


@pytest.mark.asyncio
async def test_type_text_without_a_target_types_into_the_focused_element():
    from galaius.actions import TypeTextAction

    typed = []

    class _Kb:
        async def type(self, text, **kw):
            typed.append(text)

        async def press(self, key):
            typed.append(f"<{key}>")

    class _Page:
        keyboard = _Kb()

    await TypeTextAction(text="hello").execute(_Page())
    assert "hello" in typed


# ── srv._wait: the integration point that consults _parse_wait_seconds (#63) ─────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("value, secs", [("3", 3.0), ("0.5", 0.5), ("2s", 2.0)])
async def test_numeric_wait_sleeps_instead_of_selector_matching(srv, monkeypatch, value, secs):
    """#63: agents keep passing wait="3" meaning 3 seconds; parsing it as a CSS selector throws
    'Error while parsing css selector "3"' (37+ times in the client logs). A bare number (or "Ns")
    now sleeps that many seconds."""
    from unittest.mock import AsyncMock, MagicMock

    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(srv.asyncio, "sleep", fake_sleep)
    page = MagicMock()
    page.wait_for_selector = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    await srv._wait(page, value)
    assert slept == [secs]
    page.wait_for_selector.assert_not_called()


@pytest.mark.asyncio
async def test_selector_wait_still_waits_for_visibility(srv):
    from unittest.mock import AsyncMock, MagicMock

    page = MagicMock()
    page.wait_for_selector = AsyncMock()
    await srv._wait(page, "#status")
    page.wait_for_selector.assert_called_once()


# ── WaitForAction: a bare wait_for is a pause, not a validation error (client logs) ──────────


def test_bare_wait_for_is_accepted():
    action = WaitForAction(timeout=2000)
    assert action.selector is None and action.text is None


def test_wait_for_still_rejects_both_selector_and_text():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WaitForAction(selector="#el", text="hi")


@pytest.mark.asyncio
async def test_bare_wait_for_pauses_for_the_timeout():
    with patch("galaius.actions.models.asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await WaitForAction(timeout=2000).execute(MagicMock())
    sleep.assert_awaited_once_with(2.0)
    assert result == "waited 2000ms (no condition given)"


@pytest.mark.asyncio
async def test_bare_wait_for_runs_on_the_desktop_surface(desktop_spies):
    win = DesktopWindow(name="app", wid=42, w=1200, h=800, x=0, y=0)
    with patch("galaius.actions.models.asyncio.sleep", new_callable=AsyncMock) as sleep:
        report = await _run_actions_desktop(win, [WaitForAction(timeout=500)], None)
    sleep.assert_any_await(0.5)  # the runner's own inter-step sleeps share this patched module
    assert "waited 500ms" in report
    assert "browser-only" not in report


@pytest.mark.asyncio
async def test_selector_wait_for_stays_browser_only_on_desktop(desktop_spies):
    win = DesktopWindow(name="app", wid=42, w=1200, h=800, x=0, y=0)
    report = await _run_actions_desktop(win, [WaitForAction(selector="#el")], None)
    assert "browser-only" in report
