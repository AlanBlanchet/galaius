"""Parametrized tests for src/interact/parsing.py."""

from __future__ import annotations

import pytest

from interact.parsing import Parse


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("```json\n[1,2]\n```", "[1,2]"),
        ('```\n{"a":1}\n```', '{"a":1}'),
        ("plain", "plain"),
        ("", ""),
    ],
)
def test_strip_markdown_fences(raw, expected):
    assert Parse.strip_markdown_fences(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"x": 1, "y": 2}', {"x": 1, "y": 2}),
        ("```json\n[1, 2, 3]\n```", [1, 2, 3]),
        ("not json", None),
        ("", None),
    ],
)
def test_try_json(raw, expected):
    assert Parse.try_json(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('[{"a": 1}, {"b": 2}]', [{"a": 1}, {"b": 2}]),
        ("```json\n[1,2]\n```", [1, 2]),
        ("noise before [1,2] noise after", [1, 2]),
        ('{"a": 1}', [{"a": 1}]),  # dict promoted to single-element list
        ("nothing", None),
    ],
)
def test_extract_json_array(raw, expected):
    assert Parse.extract_json_array(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"x": 12, "y": 34}', (12.0, 34.0)),
        ("[55, 66]", (55.0, 66.0)),
        ('```json\n{"x":1.5,"y":2.5}\n```', (1.5, 2.5)),
        ("garbage", None),
        ("", None),
    ],
)
def test_extract_point(raw, expected):
    assert Parse.extract_point(raw) == expected

