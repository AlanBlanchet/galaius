"""evaluate_js must surface its return value (#22 / #23).

Two bugs conspired to make the return value come back blank:
1. ``_wrap_js`` double-wrapped a *function* script (``() => { … return x }``) in another async IIFE,
   defining the inner arrow without calling it → page.evaluate returned ``undefined``.
2. the dispatcher reported the change description, not the value.

These pure tests pin the wrapping (the root cause) and the rendering; an integration test exercises
the real page round-trip.
"""

import pytest

from interact.actions import EvaluateJsAction, _wrap_js
from interact.dispatch import _render_js_result


@pytest.mark.parametrize(
    "script,expected",
    [
        # bare expression → passes through so its value is the result
        ("document.title", "document.title"),
        # statement body with return/await → wrapped in an async IIFE
        ("return document.title", "(async () => { return document.title })()"),
        (
            "const r = el.getBoundingClientRect(); return r.width",
            "(async () => { const r = el.getBoundingClientRect(); return r.width })()",
        ),
        # already a function → MUST pass through untouched (the #22/#23 bug: these were re-wrapped)
        ("() => 'hello'", "() => 'hello'"),
        ("() => { return 42 }", "() => { return 42 }"),
        ("(a) => a + 1", "(a) => a + 1"),
        ("x => x * 2", "x => x * 2"),
        ("async () => { await f(); return 1 }", "async () => { await f(); return 1 }"),
        ("function () { return 1 }", "function () { return 1 }"),
        ("async function () { return await g() }", "async function () { return await g() }"),
    ],
)
def test_wrap_js_never_double_wraps_a_function(script, expected):
    assert _wrap_js(script) == expected


def test_wrap_js_with_args_passes_a_function_through():
    # a function script + args → Playwright calls it with args; never re-wrap
    assert _wrap_js("(args) => args.n", has_args=True) == "(args) => args.n"
    # a bare body + args → wrapped so it can read `args`
    assert _wrap_js("return args.n", has_args=True) == "async (args) => { return args.n }"


def test_evaluate_js_action_picks_args_overload():
    assert EvaluateJsAction(script="return 1").args is None
    assert EvaluateJsAction(script="return args.n", args={"n": 3}).args == {"n": 3}


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"a": 1, "b": 2}, '{"a": 1, "b": 2}'),  # JSON, not Python repr with single quotes
        ("hello", '"hello"'),
        (42, "42"),
        (3.5, "3.5"),
        (True, "true"),
        ([1, 2, 3], "[1, 2, 3]"),
        ({"w": 390.0, "h": 844.0}, '{"w": 390.0, "h": 844.0}'),
    ],
)
def test_render_js_result_json_serialises(value, expected):
    assert _render_js_result(value) == expected


def test_render_js_result_none_nudges_to_return():
    out = _render_js_result(None)
    assert "undefined" in out and "return" in out


def test_render_js_result_truncates_huge_values():
    out = _render_js_result("x" * 9000)
    assert len(out) < 9000 and "chars" in out


@pytest.mark.asyncio
async def test_evaluate_js_returns_value_live():
    """End-to-end against real Chromium: the #22/#23 repro shape — a function body that returns —
    must give the value back (it used to come back null). No VLM/key; self-skips in bare CI."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # no browser provisioned (bare CI) → not this test's concern
            pytest.skip(f"no launchable chromium: {exc}")
        page = await browser.new_page()
        await page.set_content("<title>T</title><div style='width:123px;height:45px'></div>")
        geo = await EvaluateJsAction(
            script="() => { const r = document.querySelector('div').getBoundingClientRect(); "
            "return {w: r.width, h: r.height} }"
        ).execute(page)
        assert geo == {"w": 123, "h": 45}
        assert await EvaluateJsAction(script="return document.title").execute(page) == "T"
        assert await EvaluateJsAction(script="document.title").execute(page) == "T"
        assert await EvaluateJsAction(script="() => 'hello'").execute(page) == "hello"
        assert await EvaluateJsAction(script="return args.n*2", args={"n": 21}).execute(page) == 42
        await browser.close()
