"""Coordinate frames — the OS-agnostic way to express and convert coordinate spaces.

A desktop agent juggles several spaces: the virtual **screen** (spanning all
monitors), a **monitor**, a **window** (offset on the screen, minus its decoration
shadow), and the **image** it reasons over (a window/monitor capture, possibly
resized for the VLM). A :class:`Frame` names one such space and positions it within
its parent by an ``offset`` (origin in parent coords) and a ``scale`` (parent units
per frame unit). Convert a point between *any* two frames via their shared root:
``frame.convert(x, y, to=other)``.

This is the "basic functionality" layer — pure arithmetic, no OS calls. A
``DesktopBackend`` supplies the concrete offsets/scales (window position from the
window manager, resize factor from the VLM transform); everything OS-specific stays
in the backend, coordinate math stays here.
"""

import io
from typing import Self

from PIL import Image
from pydantic import BaseModel, Field, field_validator


class Frame(BaseModel):
    """A named coordinate space positioned (offset + scale) within a parent frame."""

    model_config = {"arbitrary_types_allowed": True}

    name: str
    offset_x: float = 0.0  # this frame's origin, in PARENT coordinates
    offset_y: float = 0.0
    scale_x: float = 1.0  # parent units per one unit of this frame
    scale_y: float = 1.0
    parent: "Frame | None" = None

    def child(
        self,
        name: str,
        *,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
    ) -> Self:
        """Derive a sub-frame positioned within this one (e.g. screen → window → image)."""
        return type(self)(
            name=name,
            offset_x=offset_x,
            offset_y=offset_y,
            scale_x=scale_x,
            scale_y=scale_y,
            parent=self,
        )

    def to_parent(self, x: float, y: float) -> tuple[float, float]:
        return self.offset_x + x * self.scale_x, self.offset_y + y * self.scale_y

    def from_parent(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.offset_x) / self.scale_x, (y - self.offset_y) / self.scale_y

    def to_root(self, x: float, y: float) -> tuple[float, float]:
        """Map a point in this frame up to the root (screen) coordinate space."""
        frame: Frame | None = self
        while frame is not None:
            x, y = frame.to_parent(x, y)
            frame = frame.parent
        return x, y

    def from_root(self, x: float, y: float) -> tuple[float, float]:
        """Map a root (screen) point down into this frame's coordinate space."""
        chain: list[Frame] = []
        frame: Frame | None = self
        while frame is not None:
            chain.append(frame)
            frame = frame.parent
        for frame in reversed(chain):
            x, y = frame.from_parent(x, y)
        return x, y

    def convert(self, x: float, y: float, to: "Frame") -> tuple[float, float]:
        """Convert a point in this frame to ``to``'s coordinate space (shared root)."""
        return to.from_root(*self.to_root(x, y))


class RegionOutsideCapture(ValueError):
    """A requested region does not lie inside the image it should crop."""


class Framing(BaseModel):
    """Which part of a capture to hand back, and how big: crop to ``region`` (``x, y, w, h`` in the
    capture's own pixels), then shrink to ``max_width`` keeping the aspect ratio. A 5760-wide
    three-monitor grab becomes one readable monitor without a trip through ImageMagick.

    ``apply`` returns the new PNG and a one-line note mapping image points back to capture points
    (the coordinates click / hover take), empty when nothing changed."""

    region: tuple[int, int, int, int] | None = None
    max_width: int | None = Field(None, gt=0)

    @field_validator("region")
    @classmethod
    def _positive_size(cls, region):
        if region is not None and (region[2] <= 0 or region[3] <= 0):
            raise ValueError(f"region {list(region)} needs a positive width and height — [x, y, w, h]")
        return region

    def apply(self, png: bytes) -> tuple[bytes, str]:
        if self.region is None and self.max_width is None:
            return png, ""
        img = Image.open(io.BytesIO(png))
        width, height = img.size
        x, y, w, h = self.region or (0, 0, width, height)
        if x < 0 or y < 0 or x + w > width or y + h > height:
            raise RegionOutsideCapture(
                f"region {w}x{h}+{x}+{y} is not inside the {width}x{height} capture — give "
                "x, y, w, h in the captured image's pixels"
            )
        cropped = (x, y, w, h) != (0, 0, width, height)
        if cropped:
            img = img.crop((x, y, x + w, y + h))
        scale = w / self.max_width if self.max_width and self.max_width < w else 1.0
        if scale == 1.0 and not cropped:
            return png, ""
        if scale != 1.0:
            img = img.resize((self.max_width, max(1, round(h / scale))), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        note = (
            f"(image {img.width}x{img.height} = capture region {w}x{h}+{x}+{y}"
            + (f", downscaled scale {scale:g}" if scale != 1.0 else "")
            + f"; an image point (u, v) is capture point ({x} + u*{scale:g}, {y} + v*{scale:g}))"
        )
        return buf.getvalue(), note
