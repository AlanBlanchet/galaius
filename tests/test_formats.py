"""`CoordFormat` — parsing a VLM's geometry payload (box key, box order, normalization, and the
field-name ALIASES a model actually writes for a box: x/left, w/width, and so on) into pixel
coordinates that never guess past what the payload actually said."""

import json
import logging

import pytest

from interact.desktop import DesktopElement
from interact.formats import BoxOrder, CoordFormat

_QWEN_RESPONSE = json.dumps(
    [
        {"bbox_2d": [618, 23, 660, 58], "label": "icon-button"},
        {"bbox_2d": [688, 23, 730, 58], "label": "icon-button"},
        {"bbox_2d": [758, 23, 800, 58], "label": "icon-button"},
        {"bbox_2d": [32, 183, 736, 246], "label": "button"},
        {"bbox_2d": [32, 268, 736, 331], "label": "button"},
        {"bbox_2d": [32, 353, 736, 416], "label": "button"},
        {"bbox_2d": [32, 438, 736, 501], "label": "button"},
        {"bbox_2d": [32, 523, 736, 586], "label": "button"},
        {"bbox_2d": [32, 648, 364, 711], "label": "button"},
        {"bbox_2d": [404, 648, 736, 711], "label": "button"},
    ]
)

_GEMINI_RESPONSE = json.dumps(
    [
        {"role": "icon-button", "name": "Minimize", "box_2d": [37, 896, 64, 928]},
        {"role": "icon-button", "name": "Maximize", "box_2d": [37, 931, 64, 964]},
        {"role": "icon-button", "name": "Close", "box_2d": [37, 966, 64, 992]},
        {"role": "button", "name": "Open File", "box_2d": [346, 286, 408, 714]},
        {"role": "button", "name": "Save", "box_2d": [434, 286, 497, 714]},
        {"role": "button", "name": "Run Analysis", "box_2d": [522, 286, 585, 714]},
        {"role": "button", "name": "Settings", "box_2d": [611, 286, 673, 714]},
        {"role": "button", "name": "Help", "box_2d": [699, 286, 762, 714]},
    ]
)

_PIXEL_RESPONSE = json.dumps(
    [
        {"role": "button", "name": "Open File", "x": 20, "y": 89, "w": 360, "h": 34},
    ]
)

_ZAI_RESPONSE = json.dumps(
    [
        {"role": "button", "name": "OK", "x": 50, "y": 800, "w": 400, "h": 80},
    ]
)

_OPENAI_BBOX_RESPONSE = json.dumps(
    [
        {"role": "button", "name": "OK", "bbox": [100, 200, 150, 250]},
    ]
)


@pytest.mark.parametrize(
    "fmt, response, img_w, img_h, elem_idx, expected_xywh",
    [
        (
            CoordFormat(box_order=BoxOrder.XYXY, box_key="bbox_2d"),
            _QWEN_RESPONSE,
            768,
            735,
            0,
            (618, 23, 42, 35),
        ),
        (
            CoordFormat(box_order=BoxOrder.XYXY, box_key="bbox_2d"),
            _QWEN_RESPONSE,
            768,
            735,
            3,
            (32, 183, 704, 63),
        ),
        (
            CoordFormat(normalized=True, box_order=BoxOrder.YXYX, box_key="box_2d"),
            _GEMINI_RESPONSE,
            768,
            735,
            3,
            (219, 254, 328, 45),
        ),
        (CoordFormat(), _PIXEL_RESPONSE, 400, 383, 0, (20, 89, 360, 34)),
        (
            CoordFormat(normalized=True, box_order=BoxOrder.XYXY),
            _ZAI_RESPONSE,
            768,
            735,
            0,
            (38, 588, 307, 58),
        ),
        (
            CoordFormat(box_order=BoxOrder.XYXY, box_key="bbox"),
            _OPENAI_BBOX_RESPONSE,
            800,
            600,
            0,
            (100, 200, 50, 50),
        ),
    ],
    ids=[
        "qwen-first",
        "qwen-button-no-norm",
        "gemini-yxyx-norm",
        "pixel-passthrough",
        "zai-xywh-norm",
        "openai-bbox-xyxy",
    ],
)
def test_format_parse_regression(fmt, response, img_w, img_h, elem_idx, expected_xywh):
    """Regression: format parsing produces correct pixel coords from real VLM output."""
    elements = fmt.parse(response, img_w, img_h)
    assert elements is not None
    assert len(elements) > elem_idx
    el = elements[elem_idx]
    ex, ey, ew, eh = expected_xywh
    assert abs(el.x - ex) <= 1, f"x: {el.x} != {ex}"
    assert abs(el.y - ey) <= 1, f"y: {el.y} != {ey}"
    assert abs(el.w - ew) <= 1, f"w: {el.w} != {ew}"
    assert abs(el.h - eh) <= 1, f"h: {el.h} != {eh}"


def test_get_format_warns_on_miss(caplog):
    """CoordFormat.for_model logs debug when no prefix matches."""
    CoordFormat.load_from_config(
        {"gemini/": {"normalized": True, "box_order": "yxyx", "box_key": "box_2d"}}
    )
    with caplog.at_level(logging.DEBUG, logger="interact.formats"):
        result = CoordFormat.for_model("unknown/model-xyz")
    assert "No coord format registered for model" in caplog.text
    assert "'unknown/model-xyz'" in caplog.text
    assert result == CoordFormat()  # default fallback
    CoordFormat.load_from_config({})


def test_coord_format_for_model_classmethod():
    """CoordFormat.for_model returns the prefix-matched entry."""
    CoordFormat.load_from_config(
        {
            "gpt-4o": {"box_order": "xywh", "divisor": 1000},
            "gemini/": {"normalized": True, "box_order": "yxyx", "box_key": "box_2d"},
        }
    )
    try:
        match = CoordFormat.for_model("gpt-4o-mini")
        assert match is not None
        assert match.prefix == "gpt-4o"

        gem = CoordFormat.for_model("gemini/gemini-2.0-flash")
        assert gem is not None
        assert gem.box_key == "box_2d"

        assert CoordFormat.for_model("unknown/model") == CoordFormat()
    finally:
        CoordFormat._reset()


def test_coord_format_load_from_config_clears_registry():
    """load_from_config replaces, not appends."""
    CoordFormat.load_from_config({"a/": {"divisor": 100}})
    assert len(CoordFormat.registry()) == 1
    CoordFormat.load_from_config({"b/": {"divisor": 200}, "c/": {"divisor": 300}})
    prefixes = {f.prefix for f in CoordFormat.registry()}
    assert prefixes == {"b/", "c/"}
    CoordFormat._reset()


_TYPO_RESPONSE = json.dumps(
    [
        # The misspelling a real model produced (#122): one letter off a known box key.
        {"role": "button", "name": "OK", "box_2dd": [37, 896, 64, 928]},
        # A four-number list under an unrelated key is NOT a box.
        {"role": "swatch", "name": "Red", "color": [255, 0, 0, 255]},
    ]
)


def test_a_misspelled_box_key_is_still_a_box(caplog):
    """#122: a model wrote `box_2dd`; dropping the entry silently turned one typo into
    "0 elements". A four-number list under a key one edit away from a box key is read as the
    box — and said so in the log — while a four-list under an unrelated key stays out."""
    fmt = CoordFormat(box_order=BoxOrder.YXYX, normalized=True, box_key="box_2d")
    with caplog.at_level(logging.WARNING, logger="interact.formats"):
        elements = fmt.parse(_TYPO_RESPONSE, 1000, 1000)
    assert elements is not None and len(elements) == 1, elements
    assert elements[0].name == "OK"
    assert "box_2dd" in caplog.text


# =============================================================================================
# xywh field-name aliases (#122 / #133 / #135)
# =============================================================================================


@pytest.mark.parametrize('entry', [
    {'left': 100, 'top': 200, 'width': 300, 'height': 100},
    {'x': 100, 'y': 200, 'widht': 300, 'height': 100},
    {'x': 100, 'y': 200, 'ww': 300, 'h': 100},
])
def test_xywh_aliases_preserve_pixels_and_normalized_coordinates(entry):
    parsed = DesktopElement.parse_vlm(json.dumps([entry]))
    assert parsed is not None
    assert (parsed[0].x, parsed[0].y, parsed[0].w, parsed[0].h) == (100, 200, 300, 100)
    scaled = CoordFormat(normalized=True).parse(json.dumps([entry]), 800, 600)
    assert scaled is not None
    assert (scaled[0].x, scaled[0].y, scaled[0].w, scaled[0].h) == (80, 120, 240, 60)


def test_xywh_unrelated_or_invalid_geometry_is_not_guessed():
    for entry in ({'color': [1, 2, 3, 4]}, {'x': 1, 'y': 2, 'width': 'bad', 'height': 4}):
        assert DesktopElement.parse_vlm(json.dumps([entry])) is None
        assert CoordFormat().parse(json.dumps([entry]), 800, 600) is None


@pytest.mark.parametrize('entry', [
    {'x': 1, 'y': 2, 'w': 'inf', 'h': 3},
    {'x': 1, 'y': 2, 'widht': 30, 'ww': 40, 'h': 3},
])
def test_xywh_nonfinite_and_ambiguous_typos_are_refused(entry):
    assert DesktopElement.parse_vlm(json.dumps([entry])) is None
    assert CoordFormat().parse(json.dumps([entry]), 800, 600) is None


def test_normalized_object_fallback_scales_aliases():
    source = json.dumps({'left': 100, 'top': 200, 'width': 300, 'height': 100})
    parsed = CoordFormat(normalized=True).parse(source, 800, 600)
    assert parsed is not None
    assert (parsed[0].x, parsed[0].y, parsed[0].w, parsed[0].h) == (80, 120, 240, 60)


@pytest.mark.parametrize('aliases', [
    {'x': 10, 'left': 11},
    {'w': 30, 'width': 40},
    {'x': 10.1, 'left': 10.9},
    {'w': 30, 'widht': 40},
])
def test_conflicting_coordinate_aliases_are_not_actionable(aliases):
    entry = {'x': 10, 'y': 20, 'w': 30, 'h': 40, **aliases}
    source = json.dumps([entry])
    assert DesktopElement.parse_vlm(source) is None
    assert CoordFormat(normalized=True).parse(source, 800, 600) is None


def test_equivalent_coordinate_aliases_agree():
    entry = {'x': 10, 'left': '10', 'y': 20, 'top': 20, 'w': 30, 'width': 30, 'h': 40, 'height': 40}
    element = DesktopElement.from_vlm_dict(entry, 1)
    assert (element.x, element.y, element.w, element.h) == (10, 20, 30, 40)


def test_unrelated_near_spelling_is_not_a_coordinate():
    source = json.dumps([{'x': 10, 'y': 20, 'w': 30, 'weight': 40}])
    assert DesktopElement.parse_vlm(source) is None


def test_parse_vlm_elements_no_transform():
    """CoordFormat.parse returns raw VLM-space coords; caller applies CoordTransform."""
    from interact.formats import CoordFormat

    response = '[{"role":"button","name":"OK","x":200,"y":100,"w":80,"h":30}]'
    elements = CoordFormat().parse(response, 800, 600)
    assert elements is not None
    assert elements[0].x == 200
    assert elements[0].y == 100
