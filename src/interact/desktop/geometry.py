"""BBox / point primitives in xyxy convention.

:class:`BBox` is the scalar owner — every scalar operation (area, center,
iou, containment, scaling, clamping) is a method on it. :class:`BoxArray`
is the vectorised SoA wrapper backed by numpy; scalar methods on ``BBox``
delegate to a one-row ``BoxArray`` so there is exactly one implementation
of each algorithm.
"""

from __future__ import annotations

from typing import Self

import numpy as np
from pydantic import BaseModel, ConfigDict, PrivateAttr

Point = tuple[float, float]


class BBox(BaseModel):
    """Immutable xyxy box with scalar geometry methods."""

    model_config = ConfigDict(frozen=True)

    x1: float
    y1: float
    x2: float
    y2: float

    @classmethod
    def from_xyxy(cls, t: tuple[float, float, float, float]) -> BBox:
        x1, y1, x2, y2 = t
        return cls(x1=x1, y1=y1, x2=x2, y2=y2)

    def as_xyxy(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)

    @property
    def center(self) -> Point:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    def iou(self, other: BBox) -> float:
        return float(
            BoxArray([self.as_xyxy()]).iou_matrix(BoxArray([other.as_xyxy()]))[0, 0]
        )

    def contains(self, pt: Point | None) -> bool:
        if pt is None:
            return False
        return bool(
            BoxArray([self.as_xyxy()]).contains_points(np.asarray([pt]))[0, 0]
        )

    def scale(self, sx: float, sy: float) -> BBox:
        return BBox(x1=self.x1 * sx, y1=self.y1 * sy, x2=self.x2 * sx, y2=self.y2 * sy)

    def clamp(self, w: float, h: float) -> BBox:
        return BBox(
            x1=BBox.clamp_value(self.x1, 0.0, w),
            y1=BBox.clamp_value(self.y1, 0.0, h),
            x2=BBox.clamp_value(self.x2, 0.0, w),
            y2=BBox.clamp_value(self.y2, 0.0, h),
        )

    @staticmethod
    def clamp_value(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))


class BoxArray(BaseModel):
    """Vectorised xyxy box collection backed by ``np.ndarray`` of shape ``(N, 4)``."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    _data: np.ndarray = PrivateAttr()

    def __init__(self, data: np.ndarray | list | tuple, **kw):
        arr = np.asarray(data, dtype=np.float64).reshape(-1, 4)
        if arr.ndim != 2 or arr.shape[1] != 4:
            raise ValueError(f"BoxArray expects shape (N, 4); got {arr.shape}")
        if arr.size and (
            np.any(arr[:, 2] < arr[:, 0]) or np.any(arr[:, 3] < arr[:, 1])
        ):
            raise ValueError("BoxArray requires x2>=x1 and y2>=y1 for every row")
        super().__init__(**kw)
        self.__pydantic_private__["_data"] = arr

    @classmethod
    def from_xywh(cls, rows) -> Self:
        arr = np.asarray(rows, dtype=np.float64).reshape(-1, 4)
        xyxy = np.empty_like(arr)
        xyxy[:, 0] = arr[:, 0]
        xyxy[:, 1] = arr[:, 1]
        xyxy[:, 2] = arr[:, 0] + arr[:, 2]
        xyxy[:, 3] = arr[:, 1] + arr[:, 3]
        return cls(xyxy)

    @classmethod
    def from_boxes(cls, boxes) -> Self:
        """Build from an iterable of objects exposing ``as_xyxy()``."""
        if not boxes:
            return cls(np.zeros((0, 4), dtype=np.float64))
        return cls([b.as_xyxy() for b in boxes])

    @property
    def data(self) -> np.ndarray:
        return self._data

    @property
    def x1(self) -> np.ndarray:
        return self._data[:, 0]

    @property
    def y1(self) -> np.ndarray:
        return self._data[:, 1]

    @property
    def x2(self) -> np.ndarray:
        return self._data[:, 2]

    @property
    def y2(self) -> np.ndarray:
        return self._data[:, 3]

    @property
    def area(self) -> np.ndarray:
        return np.clip(self.x2 - self.x1, 0, None) * np.clip(
            self.y2 - self.y1, 0, None
        )

    @property
    def cx(self) -> np.ndarray:
        return (self.x1 + self.x2) / 2.0

    @property
    def cy(self) -> np.ndarray:
        return (self.y1 + self.y2) / 2.0

    def to_xywh(self) -> np.ndarray:
        out = np.empty_like(self._data)
        out[:, 0] = self.x1
        out[:, 1] = self.y1
        out[:, 2] = self.x2 - self.x1
        out[:, 3] = self.y2 - self.y1
        return out

    def iou_matrix(self, other: Self) -> np.ndarray:
        a = self._data
        b = other._data
        ix1 = np.maximum(a[:, None, 0], b[None, :, 0])
        iy1 = np.maximum(a[:, None, 1], b[None, :, 1])
        ix2 = np.minimum(a[:, None, 2], b[None, :, 2])
        iy2 = np.minimum(a[:, None, 3], b[None, :, 3])
        iw = np.clip(ix2 - ix1, 0, None)
        ih = np.clip(iy2 - iy1, 0, None)
        inter = iw * ih
        union = self.area[:, None] + other.area[None, :] - inter
        out = np.zeros_like(inter)
        np.divide(inter, union, out=out, where=union > 0)
        return out

    def contains_points(self, pts: np.ndarray) -> np.ndarray:
        p = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
        x = p[:, 0]
        y = p[:, 1]
        return (
            (self.x1[:, None] <= x[None, :])
            & (x[None, :] <= self.x2[:, None])
            & (self.y1[:, None] <= y[None, :])
            & (y[None, :] <= self.y2[:, None])
        )

    def scale(self, sx: float, sy: float) -> Self:
        out = self._data.copy()
        out[:, [0, 2]] *= sx
        out[:, [1, 3]] *= sy
        return type(self)(out)

    def clamp(self, w: float, h: float) -> Self:
        out = self._data.copy()
        out[:, 0] = np.clip(out[:, 0], 0, w)
        out[:, 2] = np.clip(out[:, 2], 0, w)
        out[:, 1] = np.clip(out[:, 1], 0, h)
        out[:, 3] = np.clip(out[:, 3], 0, h)
        return type(self)(out)

    def __len__(self) -> int:
        return int(self._data.shape[0])

    def __getitem__(self, idx):
        if isinstance(idx, (int, np.integer)):
            row = self._data[idx]
            return (float(row[0]), float(row[1]), float(row[2]), float(row[3]))
        return type(self)(self._data[idx])

    def __eq__(self, other) -> bool:
        if not isinstance(other, BoxArray):
            return NotImplemented
        return np.array_equal(self._data, other._data)

    __hash__ = None  # type: ignore[assignment]
