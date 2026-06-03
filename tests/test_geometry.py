"""Parametrized tests for :mod:`interact.geometry`."""

from __future__ import annotations

import numpy as np
import pytest

from interact.geometry import Box, BoxArray


# Single mega-parametrize covers every scalar Box op.
@pytest.mark.parametrize(
    "op, args, expected",
    [
        # area
        ("area", ((0, 0, 10, 10),), 100.0),
        ("area", ((5, 5, 5, 5),), 0.0),  # zero-area
        ("area", ((10, 10, 5, 5),), 0.0),  # inverted clamps to 0
        # center
        ("center", ((0, 0, 10, 20),), (5.0, 10.0)),
        # iou(self, other)
        ("iou", ((0, 0, 10, 10), (0, 0, 10, 10)), 1.0),  # identical
        ("iou", ((0, 0, 10, 10), (20, 20, 30, 30)), 0.0),  # disjoint
        ("iou", ((0, 0, 10, 10), (10, 0, 20, 10)), 0.0),  # edge-touch only
        ("iou", ((0, 0, 10, 10), (5, 5, 15, 15)), 25.0 / 175.0),  # partial
        ("iou", ((5, 5, 5, 5), (0, 0, 10, 10)), 0.0),  # zero-area input
        # contains(pt)
        ("contains", ((0, 0, 10, 10), (5, 5)), True),
        ("contains", ((0, 0, 10, 10), (0, 0)), True),  # corner inclusive
        ("contains", ((0, 0, 10, 10), (10, 10)), True),  # opposite corner
        ("contains", ((0, 0, 10, 10), (11, 5)), False),
        ("contains", ((0, 0, 10, 10), None), False),
        # scale -> Box
        ("scale", ((10, 20, 30, 40), 2.0, 0.5), (20.0, 10.0, 60.0, 20.0)),
        # clamp_value (staticmethod)
        ("clamp_value", (5, 0, 10), 5),
        ("clamp_value", (-1, 0, 10), 0),
        ("clamp_value", (99, 0, 10), 10),
    ],
)
def test_box_ops(op, args, expected):
    if op == "area":
        (xyxy,) = args
        assert Box.from_xyxy(xyxy).area == expected
    elif op == "center":
        (xyxy,) = args
        assert Box.from_xyxy(xyxy).center == expected
    elif op == "iou":
        a, b = args
        assert Box.from_xyxy(a).iou(Box.from_xyxy(b)) == pytest.approx(expected)
    elif op == "contains":
        xyxy, pt = args
        assert Box.from_xyxy(xyxy).contains(pt) is expected
    elif op == "scale":
        xyxy, sx, sy = args
        assert Box.from_xyxy(xyxy).scale(sx, sy).as_xyxy() == expected
    elif op == "clamp_value":
        v, lo, hi = args
        assert Box.clamp_value(v, lo, hi) == expected
    else:  # pragma: no cover — defensive
        raise AssertionError(f"unknown op {op}")


def test_box_clamp_to_bounds():
    out = Box.from_xyxy((-5, -5, 15, 15)).clamp(10, 10)
    assert out.as_xyxy() == (0.0, 0.0, 10.0, 10.0)


class TestBoxArray:
    def test_iou_matrix_matches_scalar(self):
        rng = np.random.default_rng(0)
        n = 50
        xs = np.sort(rng.uniform(0, 100, size=(n, 2)), axis=1)
        ys = np.sort(rng.uniform(0, 100, size=(n, 2)), axis=1)
        a = np.column_stack([xs[:, 0], ys[:, 0], xs[:, 1], ys[:, 1]])
        xs = np.sort(rng.uniform(0, 100, size=(n, 2)), axis=1)
        ys = np.sort(rng.uniform(0, 100, size=(n, 2)), axis=1)
        b = np.column_stack([xs[:, 0], ys[:, 0], xs[:, 1], ys[:, 1]])
        ba_a, ba_b = BoxArray(a), BoxArray(b)
        mat = ba_a.iou_matrix(ba_b)
        for i in range(n):
            for j in range(n):
                exp = Box.from_xyxy(tuple(a[i])).iou(Box.from_xyxy(tuple(b[j])))
                assert mat[i, j] == pytest.approx(exp)

    @pytest.mark.parametrize(
        "rows, expected_xyxy",
        [
            ([(0, 0, 10, 20)], [(0.0, 0.0, 10.0, 20.0)]),
            ([(5, 5, 1, 2), (10, 20, 3, 4)], [(5, 5, 6, 7), (10, 20, 13, 24)]),
        ],
    )
    def test_from_xywh(self, rows, expected_xyxy):
        ba = BoxArray.from_xywh(rows)
        assert ba.data.shape == (len(rows), 4)
        for i, exp in enumerate(expected_xyxy):
            assert ba[i] == exp

    def test_contains_points(self):
        ba = BoxArray([(0, 0, 10, 10), (20, 20, 30, 30)])
        pts = np.array([(5, 5), (25, 25), (15, 15)])
        m = ba.contains_points(pts)
        assert m.tolist() == [
            [True, False, False],
            [False, True, False],
        ]

    def test_scale(self):
        ba = BoxArray([(10, 20, 30, 40)]).scale(2.0, 0.5)
        assert ba[0] == (20.0, 10.0, 60.0, 20.0)

    def test_clamp(self):
        ba = BoxArray([(-5, -5, 15, 15)]).clamp(10, 10)
        assert ba[0] == (0.0, 0.0, 10.0, 10.0)

    def test_getitem_int_and_slice(self):
        ba = BoxArray([(0, 0, 1, 1), (2, 2, 3, 3), (4, 4, 5, 5)])
        assert ba[0] == (0.0, 0.0, 1.0, 1.0)
        sub = ba[1:]
        assert isinstance(sub, BoxArray)
        assert len(sub) == 2
        assert sub[0] == (2.0, 2.0, 3.0, 3.0)

    def test_area_cx_cy(self):
        ba = BoxArray([(0, 0, 10, 20), (5, 5, 15, 25)])
        assert ba.area.tolist() == [200.0, 200.0]
        assert ba.cx.tolist() == [5.0, 10.0]
        assert ba.cy.tolist() == [10.0, 15.0]

    def test_invalid_shape_raises(self):
        with pytest.raises(ValueError):
            BoxArray([(0, 0, 10)])

    def test_inverted_box_raises(self):
        with pytest.raises(ValueError):
            BoxArray([(10, 10, 5, 5)])

    def test_equality_and_unhashable(self):
        ba1 = BoxArray([(0, 0, 1, 1)])
        ba2 = BoxArray([(0, 0, 1, 1)])
        assert ba1 == ba2
        with pytest.raises(TypeError):
            hash(ba1)
