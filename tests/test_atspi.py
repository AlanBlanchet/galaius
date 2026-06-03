"""Tests for AT-SPI accessibility tree integration."""

from unittest.mock import MagicMock, patch

import pytest

from interact.atspi import AtSpi
from interact.desktop import DesktopElement, DesktopWindow


class _MockExtents:
    def __init__(self, x, y, w, h):
        self.x = x
        self.y = y
        self.width = w
        self.height = h


class _MockComponent:
    def __init__(self, x, y, w, h):
        self._extents = _MockExtents(x, y, w, h)

    def get_extents(self, coord_type):
        return self._extents


class _MockAction:
    def __init__(self, actions: list[str]):
        self._actions = actions

    def get_n_actions(self):
        return len(self._actions)

    def get_action_name(self, i):
        return self._actions[i] if i < len(self._actions) else ""


class _MockAccessible:
    def __init__(self, name, role_name, children=None, component=None, action=None):
        self._name = name
        self._role_name = role_name
        self._children = children or []
        self._component = component
        self._action = action

    def get_name(self):
        return self._name

    def get_role_name(self):
        return self._role_name

    def get_child_count(self):
        return len(self._children)

    def get_child_at_index(self, i):
        return self._children[i] if i < len(self._children) else None

    def get_component_iface(self):
        return self._component

    def get_action_iface(self):
        return self._action


def test_atspi_available_returns_bool():
    assert isinstance(AtSpi.available(), bool)


def test_detect_elements_returns_none_when_unavailable():
    with patch("interact.atspi._Atspi", None):
        assert AtSpi.detect_elements("test window") is None


def test_find_element_by_name_returns_none_when_unavailable():
    with patch("interact.atspi._Atspi", None):
        assert AtSpi.find_element_by_name("test window", "OK") is None


def _build_tree(children):
    """Build a mock AT-SPI tree: desktop -> app -> frame -> children."""
    frame = _MockAccessible("Test Window", "frame", children=children)
    app = _MockAccessible("TestApp", "application", children=[frame])
    return _MockAccessible("desktop", "desktop", children=[app])


@pytest.mark.parametrize(
    "name, role, component, expected_included",
    [
        ("OK", "push button", _MockComponent(10, 20, 80, 30), True),
        ("Zero", "push button", _MockComponent(0, 0, 0, 0), False),
        ("Off", "push button", _MockComponent(-200, -200, 10, 10), False),
        ("NoComp", "push button", None, False),
        ("Label", "label", _MockComponent(50, 50, 30, 30), False),
    ],
    ids=["normal", "zero-size", "offscreen", "no-component", "non-interactive-role"],
)
def test_element_filtering(name, role, component, expected_included):
    """Only visible interactive elements with valid bounding boxes are collected."""
    child = _MockAccessible(name, role, component=component)
    desktop_obj = _build_tree([child])

    mock_atspi = MagicMock()
    mock_atspi.CoordType.WINDOW = 0
    mock_atspi.get_desktop.return_value = desktop_obj

    with (
        patch("interact.atspi._Atspi", mock_atspi),
        patch("interact.desktop.DesktopWindow.find", return_value=None),
    ):
        result = AtSpi.detect_elements("Test Window")

    if expected_included:
        assert result is not None
        assert len(result) == 1
        assert result[0].name == name
    else:
        assert result is None


def test_multiple_elements_indexed():
    """Multiple valid elements get sequential 1-based indices."""
    children = [
        _MockAccessible(
            "Save", "push button", component=_MockComponent(10, 10, 80, 30)
        ),
        _MockAccessible(
            "Cancel", "push button", component=_MockComponent(100, 10, 80, 30)
        ),
        _MockAccessible("Search", "entry", component=_MockComponent(10, 50, 200, 25)),
    ]
    desktop_obj = _build_tree(children)

    mock_atspi = MagicMock()
    mock_atspi.CoordType.WINDOW = 0
    mock_atspi.get_desktop.return_value = desktop_obj

    with (
        patch("interact.atspi._Atspi", mock_atspi),
        patch("interact.desktop.DesktopWindow.find", return_value=None),
    ):
        result = AtSpi.detect_elements("Test Window")

    assert result is not None
    assert len(result) == 3
    assert [el.index for el in result] == [1, 2, 3]
    assert [el.name for el in result] == ["Save", "Cancel", "Search"]


def test_find_element_by_name_exact_match():
    """Exact name match takes priority over substring."""
    children = [
        _MockAccessible(
            "Submit Form", "push button", component=_MockComponent(10, 10, 80, 30)
        ),
        _MockAccessible(
            "Submit", "push button", component=_MockComponent(100, 10, 80, 30)
        ),
    ]
    desktop_obj = _build_tree(children)

    mock_atspi = MagicMock()
    mock_atspi.CoordType.WINDOW = 0
    mock_atspi.get_desktop.return_value = desktop_obj

    with (
        patch("interact.atspi._Atspi", mock_atspi),
        patch("interact.desktop.DesktopWindow.find", return_value=None),
    ):
        result = AtSpi.find_element_by_name("Test Window", "Submit")

    assert result is not None
    assert result.name == "Submit"


def test_find_element_by_name_substring_fallback():
    """Falls back to substring match on name or role."""
    children = [
        _MockAccessible(
            "Search box", "entry", component=_MockComponent(10, 10, 200, 25)
        ),
    ]
    desktop_obj = _build_tree(children)

    mock_atspi = MagicMock()
    mock_atspi.CoordType.WINDOW = 0
    mock_atspi.get_desktop.return_value = desktop_obj

    with (
        patch("interact.atspi._Atspi", mock_atspi),
        patch("interact.desktop.DesktopWindow.find", return_value=None),
    ):
        result = AtSpi.find_element_by_name("Test Window", "search")

    assert result is not None
    assert result.name == "Search box"


def test_find_element_by_name_no_match():
    """Returns None when no element matches the selector."""
    children = [
        _MockAccessible("OK", "push button", component=_MockComponent(10, 10, 80, 30)),
    ]
    desktop_obj = _build_tree(children)

    mock_atspi = MagicMock()
    mock_atspi.CoordType.WINDOW = 0
    mock_atspi.get_desktop.return_value = desktop_obj

    with (
        patch("interact.atspi._Atspi", mock_atspi),
        patch("interact.desktop.DesktopWindow.find", return_value=None),
    ):
        assert AtSpi.find_element_by_name("Test Window", "nonexistent") is None


def test_detect_elements_no_matching_window():
    """Returns None when no application matches the window name."""
    desktop_obj = _MockAccessible(
        "desktop",
        "desktop",
        children=[
            _MockAccessible(
                "Other App",
                "application",
                children=[
                    _MockAccessible("Other Window", "frame"),
                ],
            ),
        ],
    )

    mock_atspi = MagicMock()
    mock_atspi.CoordType.WINDOW = 0
    mock_atspi.get_desktop.return_value = desktop_obj

    with patch("interact.atspi._Atspi", mock_atspi):
        assert AtSpi.detect_elements("Nonexistent Window") is None


def test_negative_coords_clamped_to_zero():
    """Elements with slightly negative coords get clamped to 0."""
    child = _MockAccessible(
        "Edge", "push button", component=_MockComponent(-5, -3, 80, 30)
    )
    desktop_obj = _build_tree([child])

    mock_atspi = MagicMock()
    mock_atspi.CoordType.WINDOW = 0
    mock_atspi.get_desktop.return_value = desktop_obj

    with (
        patch("interact.atspi._Atspi", mock_atspi),
        patch("interact.desktop.DesktopWindow.find", return_value=None),
    ):
        result = AtSpi.detect_elements("Test Window")

    assert result is not None
    assert result[0].x == 0
    assert result[0].y == 0
    assert result[0].w == 80


# --- Draggable / action-based detection ---


def test_filler_with_action_detected():
    """Element with role 'filler' and action interface is detected."""
    child = _MockAccessible(
        "DragItem",
        "filler",
        component=_MockComponent(10, 20, 100, 40),
        action=_MockAction(["drag"]),
    )
    desktop_obj = _build_tree([child])

    mock_atspi = MagicMock()
    mock_atspi.CoordType.WINDOW = 0
    mock_atspi.get_desktop.return_value = desktop_obj

    with (
        patch("interact.atspi._Atspi", mock_atspi),
        patch("interact.desktop.DesktopWindow.find", return_value=None),
    ):
        result = AtSpi.detect_elements("Test Window")

    assert result is not None
    assert len(result) == 1
    assert result[0].name == "DragItem"
    assert result[0].role == "filler"


def test_panel_with_child_label_uses_label_name():
    """Panel with child label uses label text as name when panel has no name."""
    label_child = _MockAccessible("Task Card Title", "label")
    panel = _MockAccessible(
        "",
        "panel",
        children=[label_child],
        component=_MockComponent(5, 5, 200, 60),
    )
    desktop_obj = _build_tree([panel])

    mock_atspi = MagicMock()
    mock_atspi.CoordType.WINDOW = 0
    mock_atspi.get_desktop.return_value = desktop_obj

    with (
        patch("interact.atspi._Atspi", mock_atspi),
        patch("interact.desktop.DesktopWindow.find", return_value=None),
    ):
        result = AtSpi.detect_elements("Test Window")

    assert result is not None
    assert len(result) == 1
    assert result[0].name == "Task Card Title"
    assert result[0].role == "panel"


def test_filler_no_actions_no_label_excluded():
    """Filler with no actions and no child label text is NOT detected."""
    child = _MockAccessible(
        "",
        "filler",
        component=_MockComponent(10, 20, 100, 40),
    )
    desktop_obj = _build_tree([child])

    mock_atspi = MagicMock()
    mock_atspi.CoordType.WINDOW = 0
    mock_atspi.get_desktop.return_value = desktop_obj

    with (
        patch("interact.atspi._Atspi", mock_atspi),
        patch("interact.desktop.DesktopWindow.find", return_value=None),
    ):
        result = AtSpi.detect_elements("Test Window")

    assert result is None


# --- get_window_text ---


def test_get_window_text_no_atspi():
    with patch("interact.atspi._Atspi", None):
        assert AtSpi.window_text("Text Editor") == ""


def test_get_window_text_no_app():
    desktop_obj = _MockAccessible("desktop", "desktop", children=[])
    mock_atspi = MagicMock()
    mock_atspi.get_desktop.return_value = desktop_obj
    with patch("interact.atspi._Atspi", mock_atspi):
        assert AtSpi.window_text("Nonexistent") == ""


# --- _has_actions ---


@pytest.mark.parametrize(
    "action_iface, expected",
    [
        (_MockAction(["click"]), True),
        (_MockAction(["drag", "drop"]), True),
        (_MockAction([]), False),
        (None, False),
    ],
    ids=["single-action", "multiple-actions", "empty-actions", "no-iface"],
)
def test_has_actions(action_iface, expected):
    """_has_actions returns True only when node exposes at least one action."""
    node = _MockAccessible("item", "filler", action=action_iface)
    assert AtSpi._has_actions(node) is expected


def test_has_actions_exception_returns_false():
    """_has_actions returns False when action interface raises."""
    node = MagicMock()
    node.get_action_iface.side_effect = RuntimeError("AT-SPI error")
    assert AtSpi._has_actions(node) is False


class _MockTextIface:
    def __init__(self, content: str):
        self._content = content

    def get_character_count(self):
        return len(self._content)

    def get_text(self, start, end):
        return self._content[start:end]


class _MockStateSet:
    def __init__(self, states=None):
        self._states = states or set()

    def contains(self, state):
        return state in self._states


class _TextAccessible(_MockAccessible):
    def __init__(
        self,
        name,
        role_name,
        text_content=None,
        children=None,
        component=None,
        state_set=None,
    ):
        super().__init__(name, role_name, children=children, component=component)
        self._text_iface = _MockTextIface(text_content) if text_content else None
        self._state_set = state_set

    def get_text_iface(self):
        return self._text_iface

    def get_state_set(self):
        return self._state_set


def test_get_window_text_collects_names_and_text():
    """Collects node names and text interface content from the tree."""
    children = [
        _TextAccessible("File", "menu item"),
        _TextAccessible("Search", "entry", text_content="find replace"),
        _TextAccessible(
            "Status", "label", text_content="Status"
        ),  # same as name → skipped
    ]
    app = _TextAccessible(
        "Editor",
        "application",
        children=[
            _TextAccessible("Text Editor", "frame", children=children),
        ],
    )
    desktop_obj = _MockAccessible("desktop", "desktop", children=[app])

    mock_atspi = MagicMock()
    mock_atspi.get_desktop.return_value = desktop_obj
    with patch("interact.atspi._Atspi", mock_atspi):
        result = AtSpi.window_text("Text Editor")

    assert "File" in result
    assert "Search" in result
    assert "find replace" in result
    # "Status" text_content same as name → not duplicated
    assert result.count("Status") == 1


# --- get_focused_element ---


def test_get_focused_element_no_atspi():
    with patch("interact.atspi._Atspi", None):
        assert AtSpi.focused_element("Text Editor") is None


def test_get_focused_element_finds_focused():
    """Returns 'role: name' for the focused node."""
    mock_atspi = MagicMock()
    mock_atspi.StateType.FOCUSED = "FOCUSED"

    focused_state = _MockStateSet({"FOCUSED"})
    children = [
        _TextAccessible("Save", "push button", state_set=_MockStateSet()),
        _TextAccessible("Search query", "entry", state_set=focused_state),
    ]
    app = _TextAccessible(
        "Editor",
        "application",
        children=[
            _TextAccessible("Text Editor", "frame", children=children),
        ],
    )
    desktop_obj = _MockAccessible("desktop", "desktop", children=[app])

    mock_atspi.get_desktop.return_value = desktop_obj
    with patch("interact.atspi._Atspi", mock_atspi):
        result = AtSpi.focused_element("Text Editor")

    assert result == "entry: Search query"


def test_get_focused_element_none_focused():
    """Returns None when no element has FOCUSED state."""
    mock_atspi = MagicMock()
    mock_atspi.StateType.FOCUSED = "FOCUSED"

    children = [
        _TextAccessible("Save", "push button", state_set=_MockStateSet()),
        _TextAccessible("Cancel", "push button", state_set=_MockStateSet()),
    ]
    app = _TextAccessible(
        "Editor",
        "application",
        children=[
            _TextAccessible("Text Editor", "frame", children=children),
        ],
    )
    desktop_obj = _MockAccessible("desktop", "desktop", children=[app])

    mock_atspi.get_desktop.return_value = desktop_obj
    with patch("interact.atspi._Atspi", mock_atspi):
        result = AtSpi.focused_element("Text Editor")

    assert result is None


_FIND_ELEMENTS = [
    DesktopElement(index=1, x=10, y=20, w=80, h=30, role="push button", name="OK"),
    DesktopElement(index=2, x=100, y=20, w=80, h=30, role="push button", name="Cancel"),
    DesktopElement(index=3, x=10, y=60, w=200, h=30, role="text", name="Search"),
    DesktopElement(
        index=4, x=10, y=100, w=80, h=30, role="push button", name="OK Button"
    ),
    DesktopElement(
        index=5, x=10, y=140, w=80, h=30, role="check box", name="OK to send"
    ),
]

_MOCK_WINDOW = DesktopWindow(name="Test Window", wid=12345, w=800, h=600, x=0, y=0)


@pytest.mark.parametrize(
    "name, role, expected_name",
    [
        ("Cancel", None, "Cancel"),
        ("Search", None, "Search"),
        ("Search", "text", "Search"),
        ("OK", "check box", "OK to send"),
        ("Nonexistent", None, None),
        ("OK", "text", None),
    ],
    ids=[
        "exact-match",
        "unique-substring",
        "substring-with-role",
        "role-disambiguates",
        "no-match",
        "role-filters-all",
    ],
)
def test_find_element(name, role, expected_name):
    with (
        patch("interact.desktop.DesktopWindow.find", return_value=_MOCK_WINDOW),
        patch(
            "interact.desktop.DesktopElement.cached", return_value=_FIND_ELEMENTS
        ),
    ):
        result = AtSpi.find_element("Test Window", name=name, role=role)
    if expected_name is None:
        assert result is None
    else:
        assert result.name == expected_name


@pytest.mark.parametrize(
    "name, role",
    [
        ("OK", None),
        ("OK", "push button"),
    ],
    ids=["ambiguous-no-role", "ambiguous-with-role"],
)
def test_find_element_ambiguous(name, role):
    with (
        patch("interact.desktop.DesktopWindow.find", return_value=_MOCK_WINDOW),
        patch(
            "interact.desktop.DesktopElement.cached", return_value=_FIND_ELEMENTS
        ),
    ):
        with pytest.raises(ValueError, match="Ambiguous"):
            AtSpi.find_element("Test Window", name=name, role=role)


def test_find_element_cache_miss_falls_back():
    with (
        patch("interact.desktop.DesktopWindow.find", return_value=_MOCK_WINDOW),
        patch("interact.desktop.DesktopElement.cached", return_value=None),
        patch(
            "interact.atspi.AtSpi.detect_elements", return_value=_FIND_ELEMENTS
        ) as mock_detect,
    ):
        result = AtSpi.find_element("Test Window", name="Cancel")
    assert result.name == "Cancel"
    mock_detect.assert_called_once_with("Test Window")
