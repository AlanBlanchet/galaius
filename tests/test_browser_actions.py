"""double_click + select_text (#32): actuate a selection-gated control (e.g. a Lexical inline
toolbar) — `drag` dispatches HTML5 drag-and-drop, not a text selection, and two clicks don't
coalesce into a dblclick. Validation is pure; the DOM-selection behaviour is checked live."""

import pytest

from interact.actions import DoubleClickAction, SelectTextAction


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


@pytest.mark.parametrize("kwargs,ok", [({"ref": "e1"}, True), ({"selector": "#x"}, True), ({}, False)])
def test_select_text_validation(kwargs, ok):
    if ok:
        SelectTextAction(**kwargs)
    else:
        with pytest.raises(ValueError):
            SelectTextAction(**kwargs)


@pytest.mark.asyncio
async def test_select_text_and_double_click_make_a_dom_selection_live():
    """End-to-end against real Chromium (self-skips in bare CI): both create a real DOM Selection in
    a contenteditable, the thing a Lexical toolbar needs."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"no launchable chromium: {exc}")
        page = await browser.new_page()
        await page.set_content(
            "<div id=ed contenteditable style='font-size:24px;padding:20px'>hello brave new world</div>"
        )
        await SelectTextAction(selector="#ed").execute(page)
        assert (await page.evaluate("()=>window.getSelection().toString()")).strip() == "hello brave new world"

        await page.evaluate("()=>window.getSelection().removeAllRanges()")
        await DoubleClickAction(selector="#ed").execute(page)
        assert (await page.evaluate("()=>window.getSelection().toString()")).strip() != ""
        await browser.close()
