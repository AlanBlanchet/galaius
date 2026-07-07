"""Resolve-at-the-edge guarantees + the three field-resolution bug fixes they enable.

The unifying rule: a value that must not be empty is resolved once, at the boundary, to a
concrete non-empty value — nothing downstream re-checks for empty. These tests pin that rule
where it bit real other-project runs:

- ``Config.resolve_model`` always yields a usable model id (the 39 "[Vision not configured]"
  returns came from the auto path letting an empty model id flow down to ``analyze_media``).
- ``InteractiveElement`` accepts sub-pixel DOM coords (``get_interactive_elements`` crashed on
  ``y=364.390625`` — strict int vs ``getBoundingClientRect`` floats).
- ``_wrap_js`` makes top-level ``return``/``await`` valid (agents' natural fetch scripts raised
  "Illegal return statement" / "await is only valid in async functions").
- Ambiguous click targeting fails with an actionable, ref-nudging message — not an opaque dump.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from interact.actions import ClickAction, EvaluateJsAction, HoverAction, _wrap_js
from interact.config import Config
from interact.dispatch import _named_locator
from interact.state import InteractiveElement


# --- Config.resolve_model: the single resolution site, never empty downstream ---


def test_resolve_model_override_wins_over_everything():
    cfg = Config(image_model="pinned/model")
    assert cfg.resolve_model("image", override="explicit/override") == "explicit/override"


def test_resolve_model_pin_wins_when_no_override():
    cfg = Config(image_model="pinned/model")
    assert cfg.resolve_model("image") == "pinned/model"


@pytest.mark.parametrize("role", ["image", "component", "video"])
def test_resolve_model_auto_is_never_empty(role):
    """No pin, no override → resolution falls through the role's preference chain to a concrete
    id. This is the exact scenario that returned '[Vision not configured]': model_for() was ''
    and that empty string flowed all the way to analyze_media."""
    cfg = Config(image_model="", component_model="", video_model="")
    resolved = cfg.resolve_model(role)
    assert resolved  # non-empty
    assert resolved in {m.id for m in cfg.chain_for(role).preferences}


# --- InteractiveElement: sub-pixel DOM coords resolved at construction ---


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"x": 364.390625, "y": 10.5, "width": 20.2, "height": 5.7}, (364, 10, 20, 6)),
        ({"x": 0.4, "y": 0.6, "w": 100.0, "h": 50.0}, (0, 1, 100, 50)),
    ],
)
def test_interactive_element_rounds_fractional_coords(raw, expected):
    """getBoundingClientRect returns fractional px; the model rounds at construction rather than
    rejecting (the live crash: 'Input should be a valid integer, got 364.390625')."""
    el = InteractiveElement(role="button", name="x", ref="e1", **raw)
    assert (el.x, el.y, el.w, el.h) == expected


# --- _wrap_js: top-level return/await made valid ---


@pytest.mark.parametrize(
    "script, wrapped",
    [
        ("document.title", False),  # bare expression — passed through, value returned
        ("1 + 1", False),
        ("const r = await fetch('/x'); return r.status", True),  # mid-script return + await
        ("return document.querySelectorAll('a').length", True),  # leading return
        ("const x = 2; x * 2", True),   # statement body — bare page.evaluate would SyntaxError
    ],
)
def test_wrap_js_wraps_only_when_return_or_await_present(script, wrapped):
    out = _wrap_js(script)
    if wrapped:
        assert out.startswith("(async () =>") and out.endswith(")()")
        assert script.strip() in out
    else:
        assert out == script.strip()


def test_evaluate_js_action_uses_wrapper():
    """The action delegates to _wrap_js (no behavioural fork between the two)."""
    action = EvaluateJsAction(script="const r = await f(); return r")
    assert _wrap_js(action.script).startswith("(async () =>")


def test_wrap_js_with_args_is_a_function_expression_taking_args():
    out = _wrap_js("return args.x + 1", has_args=True)
    assert out == "async (args) => { return args.x + 1 }"


@pytest.mark.asyncio
async def test_evaluate_js_passes_args_through_to_page():
    from unittest.mock import AsyncMock, MagicMock

    page = MagicMock()
    page.evaluate = AsyncMock(return_value=3)
    action = EvaluateJsAction(script="return args.x", args={"x": 2})
    result = await action.execute(page)
    assert result == 3
    fn_arg, passed = page.evaluate.call_args.args
    assert fn_arg.startswith("async (args) =>") and passed == {"x": 2}


@pytest.mark.asyncio
async def test_evaluate_js_without_args_passes_no_second_arg():
    from unittest.mock import AsyncMock, MagicMock

    page = MagicMock()
    page.evaluate = AsyncMock(return_value="ok")
    await EvaluateJsAction(script="document.title").execute(page)
    assert len(page.evaluate.call_args.args) == 1  # no args value forwarded


# --- Ambiguous targeting: actionable, ref-nudging error ---


def test_conflicting_targets_error_names_fields_and_nudges_to_ref():
    with pytest.raises(ValueError) as exc:
        ClickAction(selector="button.x", name="Submit")
    msg = str(exc.value)
    assert "selector" in msg and "name" in msg
    assert "ref" in msg  # nudge toward the stable, unique handle


def _page_with_match_count(n: int) -> MagicMock:
    page = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=n)
    nth = MagicMock()
    nth.is_visible = AsyncMock(return_value=True)  # every match visible → genuinely ambiguous
    nth.evaluate = AsyncMock(return_value="button")
    nth.inner_text = AsyncMock(return_value="Access")
    locator.nth = MagicMock(return_value=nth)
    page.get_by_role = MagicMock(return_value=locator)
    page.get_by_text = MagicMock(return_value=locator)
    return page


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 17])
async def test_named_locator_rejects_ambiguous_with_ref_nudge(count):
    """A name/role target that matches 0 or many elements fails with an actionable message
    (the real run hit a 17-match Playwright strict-mode dump) — and points at get_interactive_
    elements + `ref`, the unique-by-construction recovery."""
    page = _page_with_match_count(count)
    with pytest.raises(ValueError) as exc:
        await _named_locator(page, HoverAction(name="Access", role="button"))
    msg = str(exc.value)
    assert "get_interactive_elements" in msg and "ref" in msg
    if count > 1:
        assert str(count) in msg


@pytest.mark.asyncio
async def test_named_locator_returns_locator_when_unique():
    page = _page_with_match_count(1)
    locator = await _named_locator(page, HoverAction(name="Access", role="button"))
    assert locator is page.get_by_role.return_value
