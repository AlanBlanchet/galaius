"""Coordinate transforms between the VLM-resized image space and real screen pixels, plus the
per-window offset cache (window-manager decoration/shadow correction)."""

import io
import logging
import re
import subprocess
from typing import Self

from PIL import Image
from pydantic import BaseModel

_log = logging.getLogger("interact")

_coord_cache: dict = {}


class CoordTransform(BaseModel):
    """Affine coordinate transform: maps VLM/AT-SPI coords to screenshot to xdotool space.

    Composes: VLM resize scaling, crop offset translation, frame offset translation.
    All transformations are (scale, then translate).
    """

    scale_x: float = 1.0
    scale_y: float = 1.0
    crop_x: int = 0
    crop_y: int = 0
    shadow_left: int = 0
    shadow_right: int = 0
    shadow_top: int = 0
    shadow_bottom: int = 0
    decoration_top: int = 0

    @classmethod
    def from_xprop(cls, wid: int) -> Self:
        shadow_left = shadow_right = shadow_top = shadow_bottom = decoration_top = 0
        try:
            xprop = subprocess.check_output(
                ["xprop", "-id", str(wid)], text=True, timeout=5
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return cls()
        m = re.search(
            r"_GTK_FRAME_EXTENTS\(CARDINAL\)\s*=\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)",
            xprop,
        )
        if m:
            shadow_left = int(m.group(1))
            shadow_right = int(m.group(2))
            shadow_top = int(m.group(3))
            shadow_bottom = int(m.group(4))
        m = re.search(
            r"_MUTTER_FRAME_EXTENTS\(CARDINAL\)\s*=\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)",
            xprop,
        )
        if m:
            decoration_top = int(m.group(3))
        return cls(
            shadow_left=shadow_left,
            shadow_right=shadow_right,
            shadow_top=shadow_top,
            shadow_bottom=shadow_bottom,
            decoration_top=decoration_top,
        )

    @classmethod
    def store(cls, wid: int, offsets: Self):
        _coord_cache[wid] = offsets

    @classmethod
    def get(cls, wid: int) -> Self:
        return _coord_cache.get(wid, CoordTransform())

    @classmethod
    def has(cls, wid: int) -> bool:
        return wid in _coord_cache

    @classmethod
    def for_resize(
        cls, orig_w: int, orig_h: int, max_dim: int = 1280, min_dim: int = 768
    ) -> Self:
        mx = max(orig_w, orig_h)
        if mx > max_dim:
            scale = mx / max_dim
        elif mx < min_dim:
            scale = mx / min_dim
        else:
            return cls()
        return cls(scale_x=scale, scale_y=scale)

    def screenshot_to_xdotool(self, x: int, y: int) -> tuple[int, int]:
        return x + self.shadow_left, y + self.shadow_top

    def with_crop(self, crop_x: int, crop_y: int) -> Self:
        return self.model_copy(update={"crop_x": crop_x, "crop_y": crop_y})

    def resize_image(
        self, png_bytes: bytes, orig_w: int, orig_h: int
    ) -> tuple[bytes, int, int]:
        if self.scale_x == 1.0 and self.scale_y == 1.0:
            return png_bytes, orig_w, orig_h
        new_w = round(orig_w / self.scale_x)
        new_h = round(orig_h / self.scale_y)
        img = Image.open(io.BytesIO(png_bytes))
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        return buf.getvalue(), new_w, new_h


