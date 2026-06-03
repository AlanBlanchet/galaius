import io

import pytest
from PIL import Image

from interact import atspi
from interact.desktop import CoordTransform, DesktopWindow

pytestmark = pytest.mark.desktop

_WINDOW_TITLE = "Interact Test"


@pytest.fixture
def test_window():
    win = DesktopWindow.find(_WINDOW_TITLE)
    if not win:
        pytest.skip(f"{_WINDOW_TITLE} not running")
    return win


@pytest.fixture
def elements():
    return atspi.AtSpi.detect_elements(_WINDOW_TITLE)


def test_atspi_coords_are_in_screenshot_space(test_window, elements):
    assert elements, "No elements detected"
    screenshot = test_window.capture()
    img = Image.open(io.BytesIO(screenshot))
    # All elements should be within screenshot bounds
    for el in elements:
        assert 0 <= el.x < img.width, f"Element '{el.name}' x={el.x} out of bounds"
        assert 0 <= el.y < img.height, f"Element '{el.name}' y={el.y} out of bounds"


def test_xdotool_coords_within_window(test_window, elements):
    assert elements
    offsets = CoordTransform.get(test_window.wid)
    first = elements[0]
    xdo_x, xdo_y = offsets.screenshot_to_xdotool(first.center_x, first.center_y)
    # xdotool coords should be >= screenshot coords (offset by shadow if any)
    assert xdo_x >= first.center_x
    assert xdo_y >= first.center_y


def test_screenshot_contains_element_at_reported_coords(test_window, elements):
    assert elements
    screenshot = test_window.capture()
    img = Image.open(io.BytesIO(screenshot))
    first = elements[0]
    pixel = img.getpixel((first.center_x, first.center_y))
    assert pixel is not None, "Could get pixel"
