import pytest
from pydantic import TypeAdapter, ValidationError

from interact.actions import (
    AnyAction,
    AnnotateAction,
    BROWSER_ONLY_ACTIONS,
    ClickAction,
    ClickElementAction,
    CloseTabAction,
    CompareAction,
    DragAction,
    EvaluateJsAction,
    HoverAction,
    HttpRequestAction,
    KeyPressAction,
    NavigateAction,
    NewTabAction,
    ScreenshotAction,
    ScrollAction,
    SwitchTabAction,
    TypeTextAction,
    UploadFileAction,
    WaitForAction,
)

adapter = TypeAdapter(list[AnyAction])


def test_click_no_target_allowed():
    action = ClickAction()
    assert action.ref is None
    assert action.selector is None
    assert action.x is None
    assert action.element is None
    assert action.name is None


def test_click_partial_coordinates():
    with pytest.raises(ValidationError):
        ClickAction(x=100)


def test_scroll_invalid_amount():
    with pytest.raises(ValidationError):
        ScrollAction(amount=0)


def test_wait_for_invalid_timeout():
    with pytest.raises(ValidationError):
        WaitForAction(selector="#el", timeout=0)


def test_discriminated_union_from_dict():
    raw = [
        {"type": "click", "selector": "#btn"},
        {"type": "hover", "selector": "#link"},
        {"type": "navigate", "url": "https://example.com"},
        {"type": "screenshot"},
        {"type": "type_text", "selector": "#input", "text": "hello"},
        {"type": "scroll", "direction": "up", "amount": 5},
        {"type": "drag", "from_x": 0, "from_y": 0, "to_x": 100, "to_y": 100},
        {"type": "evaluate_js", "script": "document.title"},
        {"type": "wait_for", "selector": "#loading", "state": "hidden"},
        {
            "type": "upload_file",
            "selector": "input[type=file]",
            "path": "/tmp/file.txt",
        },
        {"type": "new_tab"},
        {"type": "switch_tab", "index": 1},
        {"type": "close_tab"},
        {"type": "http_request", "url": "https://example.com"},
        {"type": "annotate"},
        {"type": "click_element", "element": 3},
        {"type": "key_press", "key": "Enter"},
        {"type": "compare", "steps": [1, 2], "query": "diff?"},
    ]
    actions = adapter.validate_python(raw)
    assert len(actions) == 18
    expected_types = [
        ClickAction,
        HoverAction,
        NavigateAction,
        ScreenshotAction,
        TypeTextAction,
        ScrollAction,
        DragAction,
        EvaluateJsAction,
        WaitForAction,
        UploadFileAction,
        NewTabAction,
        SwitchTabAction,
        CloseTabAction,
        HttpRequestAction,
        AnnotateAction,
        ClickElementAction,
        KeyPressAction,
        CompareAction,
    ]
    for action, expected in zip(actions, expected_types):
        assert isinstance(action, expected)


def test_mutates_flag():
    assert ClickAction(selector="#x").mutates is True
    assert HoverAction(selector="#x").mutates is False
    assert TypeTextAction(selector="#x", text="hi").mutates is True
    assert ScrollAction().mutates is True
    assert DragAction(from_x=0, from_y=0, to_x=1, to_y=1).mutates is True
    assert NavigateAction(url="https://x.com").mutates is True
    assert EvaluateJsAction(script="1+1").mutates is True
    assert ScreenshotAction().mutates is False
    assert WaitForAction(selector="#x").mutates is False
    assert UploadFileAction(selector="input", path="/f").mutates is True
    assert UploadFileAction(selector="input", path="/f.txt").mutates is True
    assert NewTabAction().mutates is False
    assert SwitchTabAction().mutates is False
    assert CloseTabAction().mutates is False
    assert HttpRequestAction(url="https://x.com").mutates is False
    assert AnnotateAction().mutates is False
    assert ClickElementAction(element=1).mutates is True
    assert KeyPressAction(key="Enter").mutates is True
    assert CompareAction(steps=[1], query="q").mutates is False


def test_unknown_type_rejected():
    with pytest.raises(ValidationError):
        adapter.validate_python([{"type": "unknown_action"}])


def test_browser_only_actions_set():
    assert "navigate" in BROWSER_ONLY_ACTIONS
    assert "evaluate_js" in BROWSER_ONLY_ACTIONS
    assert "new_tab" in BROWSER_ONLY_ACTIONS
    assert "click" not in BROWSER_ONLY_ACTIONS
    assert "scroll" not in BROWSER_ONLY_ACTIONS


@pytest.mark.parametrize(
    "kwargs, valid",
    [
        ({"name": "OK"}, True),
        ({"x": 10, "y": 20}, True),
        ({}, True),
        ({"name": "OK", "selector": "#btn"}, False),
        ({"x": 10}, False),
    ],
    ids=[
        "name-only",
        "coords-only",
        "no-target",
        "name-plus-selector",
        "partial-coords",
    ],
)
def test_click_mutual_exclusion(kwargs, valid):
    if valid:
        action = ClickAction(**kwargs)
        assert action.type == "click"
    else:
        with pytest.raises(ValidationError):
            ClickAction(**kwargs)


@pytest.mark.parametrize(
    "ref, expected_element",
    [
        ("e0", 0),
        ("e5", 5),
        ("e42", 42),
        ("e999", 999),
    ],
)
def test_click_ref_to_element(ref, expected_element):
    action = ClickAction(ref=ref)
    assert action.element == expected_element
    assert action.ref is None


@pytest.mark.parametrize(
    "ref",
    ["some-ref", "button", "el5", "E42"],
    ids=["word", "plain", "prefix-mismatch", "uppercase"],
)
def test_click_ref_non_element_kept(ref):
    action = ClickAction(ref=ref)
    assert action.ref == ref
    assert action.element is None


def test_type_text_role_requires_name():
    with pytest.raises(ValidationError):
        TypeTextAction(selector="#in", text="hi", role="text")
    action = TypeTextAction(selector="#in", text="hi", name="Search", role="text")
    assert action.name == "Search"


def test_drag_role_requires_name():
    with pytest.raises(ValidationError):
        DragAction(from_x=0, from_y=0, to_x=1, to_y=1, role="button")
    action = DragAction(from_x=0, from_y=0, to_x=1, to_y=1, name="item", role="button")
    assert action.name == "item"


def test_drag_with_refs():
    action = DragAction(from_ref="e1", to_ref="e2")
    assert action.from_ref == "e1"
    assert action.to_ref == "e2"
    assert action.from_x is None


def test_drag_missing_to():
    with pytest.raises(ValidationError):
        DragAction(from_x=0, from_y=0)


def test_type_text_with_ref():
    action = TypeTextAction(ref="e15", text="hello")
    assert action.ref == "e15"
    assert action.selector is None


def test_type_text_no_target_allowed_for_desktop():
    # Desktop use: no ref/selector needed — validation deferred to execute()
    action = TypeTextAction(text="hello")
    assert action.ref is None
    assert action.selector is None
    assert action.text == "hello"


def test_upload_file_with_ref():
    action = UploadFileAction(ref="e7", path="/some/file.txt")
    assert action.ref == "e7"
    assert action.selector is None


def test_upload_file_missing_target():
    with pytest.raises(ValidationError):
        UploadFileAction(path="/some/file.txt")


def test_observe_field_on_action():
    action = ScrollAction(observe="what changed?")
    assert action.observe == "what changed?"
    action2 = ClickAction(selector="#btn", observe=None)
    assert action2.observe is None


def test_compare_action():
    action = CompareAction(steps=[1, 3], query="diff?")
    assert action.type == "compare"
    assert action.steps == [1, 3]
    assert action.query == "diff?"
    assert action.mutates is False


def test_screenshot_with_selector():
    action = ScreenshotAction(selector="#hero-image", query="describe this")
    assert action.selector == "#hero-image"
    assert action.query == "describe this"
    assert action.element is None


def test_screenshot_with_element():
    action = ScreenshotAction(element=5, query="what color is it?")
    assert action.element == 5
    assert action.selector is None


def test_screenshot_element_via_union():
    raw = [{"type": "screenshot", "selector": "div.card", "element": 3}]
    actions = adapter.validate_python(raw)
    assert len(actions) == 1
    assert isinstance(actions[0], ScreenshotAction)
    assert actions[0].selector == "div.card"
    assert actions[0].element == 3


def test_annotate_with_scope():
    action = AnnotateAction(scope="#main")
    assert action.scope == "#main"


def test_annotate_custom_limit():
    action = AnnotateAction(limit=10)
    assert action.limit == 10


def test_click_element_validates():
    action = ClickElementAction(element=3)
    assert action.element == 3
    assert action.type == "click_element"
    assert action.mutates is True


def test_key_press_defaults():
    action = KeyPressAction(key="Enter")
    assert action.type == "key_press"
    assert action.key == "Enter"


def test_key_press_in_union():
    raw = [{"type": "key_press", "key": "Control+c"}]
    actions = adapter.validate_python(raw)
    assert len(actions) == 1
    assert isinstance(actions[0], KeyPressAction)
    assert actions[0].key == "Control+c"


def test_key_press_mutates():
    assert KeyPressAction(key="Escape").mutates is True


def test_drag_steps_custom():
    action = DragAction(from_x=0, from_y=0, to_x=100, to_y=100, steps=10)
    assert action.steps == 10


# ── scroll anchoring (#76): the wheel goes to the widget under the pointer, so a scroll can be
# targeted like a click; the browser path moves/hovers to the anchor before wheeling ─────────


def test_scroll_accepts_an_anchor():
    a = ScrollAction(x=700, y=750, direction="down", amount=5)
    assert (a.x, a.y) == (700, 750)
    assert ScrollAction(ref="e3").ref == "e3"
    assert ScrollAction().x is None  # unanchored stays valid (center / current-pointer default)


def test_scroll_partial_coordinates_rejected():
    with pytest.raises(ValidationError):
        ScrollAction(x=100)


@pytest.mark.asyncio
async def test_browser_scroll_moves_to_the_anchor_before_wheeling():
    calls: list = []

    class _Mouse:
        async def move(self, x, y):
            calls.append(("move", x, y))

        async def wheel(self, dx, dy):
            calls.append(("wheel", dx, dy))

    class _Page:
        mouse = _Mouse()

    await ScrollAction(x=120, y=340, direction="down", amount=2).execute(_Page())
    assert calls == [("move", 120, 340), ("wheel", 0, 300), ("wheel", 0, 300)]
