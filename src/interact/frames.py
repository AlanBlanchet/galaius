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

from typing import Self

from pydantic import BaseModel


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
