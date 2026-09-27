"""`vision_infer._bounded_thumbnail`: the machine-local thumbnail every vision workflow step
embeds in its result — for the overlay it produces AND (the bug this covers) the photo it read —
so the workflow editor's node card shows a picture, never a raw file path. Bounded so a run event
never bloats: at most 256 px on its long side, at most 64 KB once WebP-encoded (interact_core's
VALUE_PREVIEW_MAX_PIXELS / VALUE_PREVIEW_MAX_BYTES, duplicated here since this script stays
dependency-free — see its module docstring).
"""

import base64
import io

from PIL import Image

from interact.vision_infer import _PREVIEW_MAX_BYTES, _PREVIEW_MAX_PIXELS, _bounded_thumbnail


def _decode(encoded: str) -> Image.Image:
    raw = base64.b64decode(encoded)
    assert len(raw) <= _PREVIEW_MAX_BYTES
    image = Image.open(io.BytesIO(raw))
    assert image.format == "WEBP"
    return image


def test_small_image_thumbnails_under_the_pixel_and_byte_bound() -> None:
    source = Image.new("RGB", (64, 48), color=(20, 120, 200))
    decoded = _decode(_bounded_thumbnail(source))
    assert max(decoded.size) <= _PREVIEW_MAX_PIXELS
    assert decoded.size[0] / decoded.size[1] == 64 / 48


def test_large_high_entropy_image_still_lands_under_the_byte_bound() -> None:
    """A busy, incompressible frame (real detection photos are never a flat colour) forces the
    quality back-off loop — the exact input the 64 KB cap exists to hold."""
    import random

    random.seed(0)
    pixels = bytes(random.randrange(256) for _ in range(1024 * 1024 * 3))
    source = Image.frombytes("RGB", (1024, 1024), pixels)
    encoded = _bounded_thumbnail(source)
    decoded = _decode(encoded)
    assert max(decoded.size) <= _PREVIEW_MAX_PIXELS


def test_thumbnail_preserves_aspect_ratio_on_the_long_side() -> None:
    source = Image.new("RGB", (2000, 500), color=(10, 10, 10))
    decoded = _decode(_bounded_thumbnail(source))
    assert decoded.size[0] == _PREVIEW_MAX_PIXELS
    assert decoded.size[1] == _PREVIEW_MAX_PIXELS // 4
