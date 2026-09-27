"""Small synthetic media a test can hand to code that expects a real capture."""

from __future__ import annotations

import io

from PIL import Image


def solid_png(
    width: int | tuple[int, int] = 8,
    height: int | None = None,
    colour: tuple[int, int, int] | int = (255, 255, 255),
    *,
    mode: str = "RGB",
    speckle: int = 0,
    block: tuple[int, int, int, int] | None = None,
    block_colour: tuple[int, int, int] | int = 0,
    bottom: tuple[int, int, int] | None = None,
    bottom_frac: float = 0.12,
) -> bytes:
    """A synthetic PNG built from ONE signature instead of the seven ad-hoc builders this
    replaces: a solid fill (any PIL ``mode``), optionally speckled (the "has content" case),
    optionally painted with an explicit ``block`` rectangle (the keystroke-band diff), or with a
    ``bottom`` strip sized by ``bottom_frac`` (the GL-unrendered / capture-repaint heuristics).

    ``width`` takes either a bare int with ``height`` given separately (every existing caller's
    shape), or a ``(w, h)`` tuple — for a caller already holding its size as one tuple, so it
    doesn't have to unpack it back into two positional args just to call this. In the tuple form
    a value passed positionally in ``height``'s slot is really ``colour`` (`solid_png((w, h), 255,
    ...)`, exactly as `solid_png(w, h, 255, ...)` reads today)."""
    if isinstance(width, tuple):
        width, height, colour = width[0], width[1], height if height is not None else colour
    elif height is None:
        height = width
    img = Image.new(mode, (width, height), colour)
    if block is not None:
        x0, y0, x1, y1 = block
        for x in range(x0, x1):
            for y in range(y0, y1):
                img.putpixel((x, y), block_colour)
    if bottom is not None:
        for x in range(width):
            for y in range(int(height * (1 - bottom_frac)), height):
                img.putpixel((x, y), bottom)
    fill_value = 255 if mode == "L" else (255, 255, 255)
    for i in range(speckle):
        img.putpixel((i % width, (i * 7) % height), fill_value)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def varied_png(size: tuple[int, int] = (320, 200)) -> bytes:
    """A frame with content everywhere — the negative case for every blankness check."""
    img = Image.new("RGB", size)
    for x in range(size[0]):
        for y in range(size[1]):
            img.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
