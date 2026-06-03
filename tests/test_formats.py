import json
import logging

import pytest

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
