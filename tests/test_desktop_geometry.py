"""IoU and box/fusion geometry: overlap fraction, Box-method subclass preservation, and
VLM/AT-SPI element fusion (geometry from AT-SPI, role/name kept from the VLM detection)."""

import pytest

from interact.desktop import CoordTransform, DesktopElement


def make_element(
    x: int,
    y: int,
    w: int,
    h: int,
    role: str = "button",
    name: str = "",
    index: int = 1,
) -> DesktopElement:
    """A ``DesktopElement`` built the shorthand way; local because only the IoU/fusion tests
    below need it."""
    return DesktopElement(index=index, x=x, y=y, w=w, h=h, role=role, name=name)


@pytest.mark.parametrize(
    "a, b, expected",
    [
        # Perfect overlap
        (make_element(0, 0, 100, 100), make_element(0, 0, 100, 100), 1.0),
        # No overlap
        (make_element(0, 0, 50, 50), make_element(100, 100, 50, 50), 0.0),
        # Partial overlap (50x50 intersection, union = 2*100*100 - 2500 = 17500)
        (make_element(0, 0, 100, 100), make_element(50, 50, 100, 100), 2500 / 17500),
        # Zero-area element
        (make_element(10, 10, 0, 0), make_element(10, 10, 100, 100), 0.0),
        # Contained element (50x50 inside 100x100)
        (make_element(25, 25, 50, 50), make_element(0, 0, 100, 100), 2500 / 10000),
    ],
    ids=["perfect", "disjoint", "partial", "zero-area", "contained"],
)
def test_iou(a, b, expected):
    assert abs(a.iou(b) - expected) < 1e-6


@pytest.mark.parametrize(
    "method",
    [
        lambda el: el.clamp(1000, 1000),
        lambda el: el.scale(2.0, 2.0),
        lambda el: el.translate(10, 20),
        lambda el: el.transform(
            CoordTransform(scale_x=1.5, scale_y=1.5, crop_x=5, crop_y=10)
        ),
    ],
    ids=["clamp", "scale", "translate", "transform"],
)
def test_box_methods_preserve_subclass(method):
    el = DesktopElement(index=5, x=100, y=200, w=80, h=40, role="button", name="Save")
    result = method(el)
    assert isinstance(result, DesktopElement)
    assert result.index == 5
    assert result.role == "button"
    assert result.name == "Save"


def test_fuse_elements_snaps_bbox():
    vlm = [make_element(10, 20, 80, 30, "button", "Save")]
    atspi = [make_element(12, 18, 82, 32, "push-button", "Save Button")]
    fused = DesktopElement.fuse(vlm, atspi)
    assert len(fused) == 1
    # VLM role/name kept, AT-SPI geometry used
    assert fused[0].role == "button"
    assert fused[0].name == "Save"
    assert fused[0].x == 12
    assert fused[0].y == 18
    assert fused[0].w == 82
    assert fused[0].h == 32


def test_fuse_elements_low_iou_keeps_both():
    """Non-overlapping elements: VLM kept as-is + AT-SPI appended as unmatched."""
    vlm = [make_element(0, 0, 50, 50, "button", "A")]
    atspi = [make_element(500, 500, 50, 50, "push-button", "B")]
    fused = DesktopElement.fuse(vlm, atspi)
    assert len(fused) == 2
    assert fused[0].x == 0
    assert fused[0].name == "A"
    assert fused[1].x == 500
    assert fused[1].name == "B"
    assert fused[1].index == 2


def test_fuse_elements_inherits_atspi_name_when_vlm_empty():
    vlm = [make_element(10, 10, 100, 100, "button", "")]
    atspi = [make_element(10, 10, 100, 100, "push-button", "Toolbar Save")]
    fused = DesktopElement.fuse(vlm, atspi)
    assert fused[0].name == "Toolbar Save"


def test_fuse_elements_reindexes():
    vlm = [
        make_element(0, 0, 50, 50, "button", "A"),
        make_element(100, 100, 50, 50, "link", "B"),
    ]
    atspi = [make_element(0, 0, 50, 50, "push-button", "X")]
    fused = DesktopElement.fuse(vlm, atspi)
    assert [f.index for f in fused] == [1, 2]

