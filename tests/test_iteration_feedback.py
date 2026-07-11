"""Second iteration, driven by re-reading other-project usage (cfcd/neostore/home, post-fix):

- `target` replaces the `window`/`session` split with one "what am I driving?" param.
- `get_page_state` / no-query `screenshot` surface refs from a pure DOM scan (no VLM, any model).
- `wait_for` gains a `text` condition — a deterministic alternative to a guessed `sleep`.
- A selector that never matches/acts no longer hangs 30s then dumps Playwright's call log; it
  fails fast with a ref-nudging message (the new `Page.click/hover: Timeout` feedback).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from interact import server as srv
from interact.actions import WaitForAction
from interact.actions.dispatch import _execute_browser_action


@pytest.fixture(autouse=True)
def _desktop_gate_open(monkeypatch):
    # The desktop-target tests below verify Linux resolution logic with mocked backends and run on
    # every CI OS. Pin the Linux path (force desktop_supported() True so a mac/win runner doesn't
    # take the portable-screen branch) and open the unsupported gate; harmless for the browser tests.
    monkeypatch.setattr("interact.desktop.backend.desktop_supported", lambda: True)
    monkeypatch.setattr(srv.targets, "_desktop_unsupported", lambda *a, **k: None)


# --- target: one param, browser default or a desktop window ---


def test_resolve_target_browser_is_the_default(monkeypatch):
    sessions = MagicMock()
    sessions.get.return_value = "MGR"
    monkeypatch.setattr(srv.core, "_sessions", sessions)
    for target in (None, "browser", "  Browser  "):
        win, mgr, err = srv._resolve_target(target, "default")
        assert (win, mgr, err) == (None, "MGR", None)


def test_resolve_target_string_is_a_desktop_window(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(srv.targets, "_find_desktop_window", lambda title: sentinel)
    win, mgr, err = srv._resolve_target("Firefox", "default")
    assert win is sentinel and mgr is None and err is None


def test_resolve_target_desktop_and_named_session_conflict(monkeypatch):
    monkeypatch.setattr(srv.targets, "_find_desktop_window", lambda title: object())
    win, mgr, err = srv._resolve_target("Firefox", "work")
    assert win is None and mgr is None and "Cannot combine" in err


# --- #4: navigate timeout is configurable (slow dev servers compile routes for 15-60s) ---


def _fake_navigate_env(monkeypatch):
    """Stub navigate's collaborators; return the page whose .goto we assert on."""
    page = MagicMock()
    page.goto = AsyncMock()
    mgr = MagicMock()
    mgr.get_page = AsyncMock(return_value=page)
    sessions = MagicMock()
    sessions.get.return_value = mgr
    state = MagicMock()
    state.screenshot_base64 = None
    state.text_summary.return_value = "ok"
    monkeypatch.setattr(srv.core, "_sessions", sessions)
    monkeypatch.setattr(srv.tools_web, "Debug", MagicMock())
    monkeypatch.setattr(srv.capture, "_wait", AsyncMock())
    monkeypatch.setattr(srv.capture, "_capture", AsyncMock(return_value=state))
    return page


@pytest.mark.asyncio
async def test_navigate_forwards_custom_timeout_to_goto(monkeypatch):
    page = _fake_navigate_env(monkeypatch)
    await srv.navigate("http://localhost:3000", timeout=60000)
    assert page.goto.call_args.kwargs.get("timeout") == 60000


@pytest.mark.asyncio
async def test_navigate_without_timeout_uses_the_context_default(monkeypatch):
    """No explicit timeout → don't override; the browser context's default still applies."""
    page = _fake_navigate_env(monkeypatch)
    await srv.navigate("http://localhost:3000")
    assert page.goto.call_args.kwargs.get("timeout") is None


# --- wait_for: a concrete condition instead of a guessed sleep ---


@pytest.mark.parametrize(
    "kwargs, ok",
    [
        ({"selector": "#x"}, True),
        ({"text": "Loaded"}, True),
        ({}, False),  # neither — ambiguous wait
        ({"selector": "#x", "text": "Loaded"}, False),  # both
    ],
)
def test_wait_for_requires_exactly_one_condition(kwargs, ok):
    if ok:
        WaitForAction(**kwargs)
    else:
        with pytest.raises(ValueError):
            WaitForAction(**kwargs)


@pytest.mark.asyncio
async def test_wait_for_text_polls_page_for_substring():
    page = MagicMock()
    page.wait_for_function = AsyncMock()
    msg = await WaitForAction(text="Done").execute(page)
    page.wait_for_function.assert_awaited_once()
    assert "Done" in msg
    assert page.wait_for_function.call_args.kwargs["arg"] == "Done"


# --- selector failure → fast, actionable ref-nudge (not a 30s opaque dump) ---


class _TimingOutAction:
    type = "click"
    selector = "#never-here"

    async def execute(self, page):
        raise PlaywrightTimeout("Timeout 10000ms exceeded")


@pytest.mark.asyncio
async def test_selector_timeout_becomes_ref_nudge():
    with pytest.raises(ValueError) as exc:
        await _execute_browser_action(_TimingOutAction(), MagicMock())
    msg = str(exc.value)
    assert "#never-here" in msg  # names the offending selector
    assert "ref" in msg and "get_interactive_elements" in msg  # the recovery
    assert "Timeout 10000ms exceeded" not in msg  # not the raw Playwright dump


@pytest.mark.asyncio
async def test_successful_action_passes_through():
    action = MagicMock()
    action.execute = AsyncMock(return_value="clicked")
    assert await _execute_browser_action(action, MagicMock()) == "clicked"


class _FailingEval:
    type = "evaluate_js"
    selector = None

    async def execute(self, page):
        raise PlaywrightTimeout("Timeout 10000ms exceeded")


@pytest.mark.asyncio
async def test_non_targeting_action_gets_no_ref_nudge():
    """evaluate_js/navigate timeouts are real, but a `ref` is meaningless for them — don't
    mislead the agent into detecting elements when its script just hung."""
    with pytest.raises(ValueError) as exc:
        await _execute_browser_action(_FailingEval(), MagicMock())
    assert "ref" not in str(exc.value) and "get_interactive_elements" not in str(exc.value)


# --- refs inline: model-agnostic DOM scan ---


@pytest.mark.asyncio
async def test_scan_elements_builds_refs_and_registers_map_without_vlm():
    page = MagicMock()
    page.evaluate = AsyncMock(
        return_value={  # JS returns {elements, nextRef} — the session ref counter (#35)
            "elements": [
                {"ref": "e1", "tag": "button", "name": "OK", "x": 1.4, "y": 2.6, "width": 10.2, "height": 5.0},
                {"ref": "e2", "tag": "a", "name": "Home", "x": 0, "y": 0, "width": 4, "height": 4},
            ],
            "nextRef": 2,
        }
    )
    mgr = MagicMock()
    mgr._ref_counter = 0
    mgr.get_page = AsyncMock(return_value=page)

    elements = await srv._scan_elements(mgr, 0, None)

    assert [e.ref for e in elements] == ["e1", "e2"]
    assert (elements[0].x, elements[0].y, elements[0].w) == (1, 3, 10)  # rounded at the edge
    mgr.set_element_map.assert_called_once_with(0, elements)
    # purely DOM: no screenshot, no model call on this path
    page.screenshot.assert_not_called()
