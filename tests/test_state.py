import io

from unittest.mock import patch

import pytest
from PIL import Image

from interact.state import (
    DesktopState,
    InteractiveElement,
    PageState,
    StateChange,
    annotate_screenshot,
    format_element_list,
)


def _make_state(**overrides) -> PageState:
    defaults = {
        "url": "https://example.com",
        "title": "Example",
        "accessibility_tree": "{}",
        "screenshot_base64": "abc123",
        "visible_text": "Hello world",
        "focused_element": None,
    }
    return PageState(**(defaults | overrides))


def test_no_changes():
    before = _make_state()
    after = _make_state()
    change = StateChange.compute(before, after)
    assert change.description == ""


@pytest.mark.parametrize(
    "field, before_val, after_val, expected",
    [
        (
            "url",
            "https://example.com/a",
            "https://example.com/b",
            ["https://example.com/a", "https://example.com/b"],
        ),
        ("title", "Page A", "Page B", ["Page A", "Page B"]),
        (
            "focused_element",
            "INPUT(search)",
            "BUTTON(submit)",
            ["INPUT(search)", "BUTTON(submit)"],
        ),
        ("visible_text", "Hello", "Hello World", ["World"]),
        ("visible_text", "Hello World", "Hello", ["World"]),
    ],
)
def test_field_change(field, before_val, after_val, expected):
    before = _make_state(**{field: before_val})
    after = _make_state(**{field: after_val})
    change = StateChange.compute(before, after)
    for text in expected:
        assert text in change.description


def test_multiple_changes():
    before = _make_state(url="https://a.com", title="A", visible_text="old content")
    after = _make_state(url="https://b.com", title="B", visible_text="new content")
    change = StateChange.compute(before, after)
    assert "URL:" in change.description
    assert "Title:" in change.description
    assert "new" in change.description


def _make_element(index: int, ref: str | None = None, **kw) -> InteractiveElement:
    defaults = dict(
        role="button", name=f"Element {index}", x=10.0, y=20.0, width=80.0, height=40.0
    )
    return InteractiveElement(index=index, ref=ref, **(defaults | kw))


def _make_png(width: int = 200, height: int = 100) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(240, 240, 240)).save(buf, format="PNG")
    return buf.getvalue()


# --- annotate_screenshot ---


def test_annotate_screenshot_produces_valid_png():
    elements = [
        _make_element(1, x=5, y=5, width=30, height=20),
        _make_element(2, x=50, y=10, width=40, height=30),
    ]
    result = annotate_screenshot(_make_png(), elements)
    img = Image.open(io.BytesIO(result))
    assert img.format == "PNG"
    assert img.size == (200, 100)


# --- format_element_list ---


def test_format_element_list():
    elements = [_make_element(1, ref="e10"), _make_element(2)]
    text = format_element_list(elements)
    assert "[1]" in text
    assert "[2]" in text
    assert "ref=e10" in text
    assert "ref=None" in text
    assert "button" in text


# --- DesktopState ---


def _make_desktop_state(**overrides) -> DesktopState:
    defaults = {
        "window_name": "Text Editor",
        "visible_text": "File Edit View\nUntitled Document",
        "focused_element": "entry: Search",
    }
    return DesktopState(**(defaults | overrides))


def test_desktop_no_changes():
    before = _make_desktop_state()
    after = _make_desktop_state()
    result = DesktopState.compute_change(before, after)
    assert result == ""


@pytest.mark.parametrize(
    "before_kw, after_kw, expected_fragments",
    [
        pytest.param(
            {"focused_element": "entry: Search"},
            {"focused_element": "push button: OK"},
            ["Focus:"],
            id="focus-changed",
        ),
        pytest.param(
            {"visible_text": "Hello"},
            {"visible_text": "Hello World"},
            ["New text:"],
            id="text-added",
        ),
        pytest.param(
            {"visible_text": "Hello World"},
            {"visible_text": "Hello"},
            ["Removed text:"],
            id="text-removed",
        ),
        pytest.param(
            {"focused_element": "entry: Search", "visible_text": "Draft saved"},
            {"focused_element": "push button: Send", "visible_text": "Message sent"},
            ["Focus:", "New text:"],
            id="multiple-changes",
        ),
    ],
)
def test_desktop_compute_change(before_kw, after_kw, expected_fragments):
    before = _make_desktop_state(**before_kw)
    after = _make_desktop_state(**after_kw)
    result = DesktopState.compute_change(before, after)
    for fragment in expected_fragments:
        assert fragment in result


def test_desktop_capture_calls_atspi():
    with (
        patch(
            "interact.desktop.atspi.AtSpi.window_text", return_value="Menu Bar\nContent area"
        ) as mock_text,
        patch(
            "interact.desktop.atspi.AtSpi.focused_element", return_value="entry: URL bar"
        ) as mock_focus,
    ):
        state = DesktopState.capture("Firefox")

    mock_text.assert_called_once_with("Firefox")
    mock_focus.assert_called_once_with("Firefox")
    assert state.window_name == "Firefox"
    assert state.visible_text == "Menu Bar\nContent area"
    assert state.focused_element == "entry: URL bar"


def test_format_element_list_shows_the_tooltip_description():
    # An icon-only toolbar is unreadable without its tooltips; the popup can't render in a nested
    # capture, so the listing carries the AT-SPI description (Qt's toolTip) instead (#75).
    el = _make_element(1, ref="e1")
    el.description = "Run the reconstruction"
    text = format_element_list([el])
    assert "Run the reconstruction" in text
    assert "tip" in text
