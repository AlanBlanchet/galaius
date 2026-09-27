"""Deterministic UI pixel measurement — no VLM, no spend, fully reproducible.

Mining real interact usage showed agents never trusted the VLM's prose verdict (it confidently
"PASS"ed a lime-green build that should've been teal); the verdict they trusted came from running
their OWN WCAG contrast calculator + colour sampling on raw pixels — the single most-repeated manual
workaround in the logs. `measure_ui` makes that a primitive: exact colours + WCAG contrast in a
region, and the largest uniform band (the "big empty space" defect the VLM kept missing). It also
backstops `review_ui` — the VLM flags a suspect element, `measure_ui` confirms the number.
"""

from __future__ import annotations

import io
from collections import Counter

import numpy as np
from PIL import Image
from pydantic import BaseModel

Rgb = tuple[int, int, int]


def _to_rgb(png: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))


def _hex(rgb) -> str:
    r, g, b = (int(round(float(c))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _rel_luminance(rgb: Rgb) -> float:
    """WCAG 2.x relative luminance of an sRGB colour."""

    def lin(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast_ratio(rgb1: Rgb, rgb2: Rgb) -> float:
    """WCAG contrast ratio (1.0–21.0) between two colours, order-independent."""
    lo, hi = sorted((_rel_luminance(rgb1), _rel_luminance(rgb2)))
    return (hi + 0.05) / (lo + 0.05)


def _dominant_colors(arr: np.ndarray, k: int = 4, quant: int = 16) -> list[tuple[Rgb, float]]:
    """Top-``k`` colours by frequency. Pixels are grouped into coarse buckets so anti-aliasing
    doesn't fragment a flat fill, but each colour returned is the MEAN of actual pixels in its
    bucket (not the bucket centre) — pure black/white come back exact and the WCAG ratio is
    accurate, not quantization-skewed. Returns ``[(rgb, fraction), …]`` most-common first."""
    flat = arr.reshape(-1, 3).astype(int)
    buckets = flat // quant
    keys = buckets[:, 0] * 100_000 + buckets[:, 1] * 1_000 + buckets[:, 2]
    total = len(keys)
    out: list[tuple[Rgb, float]] = []
    for key, count in Counter(keys.tolist()).most_common(k):
        mean = flat[keys == key].mean(axis=0)
        out.append((tuple(int(round(float(c))) for c in mean), count / total))
    return out


# Blank means NO CONTRAST ANYWHERE — not "one colour dominates". Text is a minority of pixels
# by area on every page ever designed, so a dominant-colour test calls a real sign-in form
# (99.6% white) empty and REFUSES to analyse it. A confident wrong refusal about a real screen
# is worse than the confabulation this exists to prevent.
# The bar is FLAT, deliberately strict. Every reported case — a crashed window, an unmapped
# surface, a GPU buffer the grabber can't read — comes back perfectly uniform, so nothing is
# lost by demanding that; any frame carrying text carries near-black pixels on near-white,
# nowhere near it. Erring this way is the point: wrongly calling a real screen blank REFUSES to
# look at it — the failure this gate exists to prevent, in the other direction. Wrongly calling
# an empty one real just spends a model call.
_FLAT_RANGE = 8
_COMPRESSIBLE_ENOUGH = 0.05
# Below this the encoded size says more about PNG's header than the picture: an 8x8 blank frame
# encodes at 1.08 bytes/pixel, twenty times the threshold. An element query crops single
# widgets, exactly this size range, so the check is simply skipped there — the decode it exists
# to avoid costs microseconds at that size.
_FAST_PATH_MIN_PIXELS = 20_000

_MIN_FOREGROUND_OCCUPANCY = 0.01
# Below 1% of sampled pixels, the second-most-common colour is as likely to be an
# anti-aliasing fringe, a single stray icon pixel, or compression noise as it is real text —
# real glyph coverage clears this even for small captions. A confident WCAG PASS/FAIL computed
# off that sliver is a false violation (or false pass), not a measurement (issue #164).


def _png_dimensions(png: bytes) -> tuple[int, int] | None:
    """Width and height straight out of the IHDR header, without decoding the image."""
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        return None
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def blank_frame_reason(png: bytes) -> str | None:
    """Why this frame has nothing on it, or ``None`` if it does.

    Handed an all-black capture of a CRASHED window, a VLM didn't say "this image is empty" —
    it answered the question anyway, echoing the agent's own action text back as if it had read
    it on screen (#112). A plausible invented answer is worse than an error, because it reads
    as a real observation and the caller acts on it. Emptiness is deterministic, decided here on
    the pixels, never sent to a model.
    """
    # Cheapest question first: a flat image is what PNG compresses BEST, so its encoded size can
    # rule out a busy frame before any decode — hundreds of kB for a real screenshot against a few
    # for an all-black one.
    if (dims := _png_dimensions(png)) is not None:
        w, h = dims
        if w * h >= _FAST_PATH_MIN_PIXELS and len(png) / (w * h) > _COMPRESSIBLE_ENOUGH:
            return None
    try:
        img = Image.open(io.BytesIO(png)).convert("L")
    except Exception:
        return None  # unreadable bytes are a different failure; don't mask it as "blank"
    lum = np.asarray(img)
    if not lum.size:
        return None
    if int(lum.max()) - int(lum.min()) > _FLAT_RANGE:
        return None
    tone = _hex((int(lum.mean()),) * 3)
    return f"blank {img.width}x{img.height} frame — flat {tone}, no contrast anywhere"


def _largest_uniform_band(
    arr: np.ndarray, std_thresh: float = 6.0, color_thresh: float = 10.0
) -> dict | None:
    """The tallest run of consecutive rows that are each near-uniform AND the SAME colour — the
    full-width band of one flat fill that reads as 'too much empty space'. A solid header followed
    by white is two bands, not one (the colour change breaks the run). Returns ``{"y", "height",
    "color"}`` (y relative to ``arr``) or None."""
    img = arr.reshape(arr.shape[0], -1, 3)
    row_std = arr.reshape(arr.shape[0], -1).std(axis=1)
    row_mean = img.mean(axis=1)  # (H, 3) mean colour per row
    best_len = best_start = 0
    start: int | None = None
    for y in range(len(row_std)):
        flat = row_std[y] < std_thresh
        same = start is not None and float(np.abs(row_mean[y] - row_mean[start]).max()) < color_thresh
        if flat and (start is None or same):
            if start is None:
                start = y
        else:  # row is textured, or its colour differs from the run → close the run
            if start is not None and y - start > best_len:
                best_len, best_start = y - start, start
            start = y if flat else None
    if start is not None and len(row_std) - start > best_len:
        best_len, best_start = len(row_std) - start, start
    if best_len == 0:
        return None
    color = img[best_start:best_start + best_len].reshape(-1, 3).mean(axis=0)
    return {"y": best_start, "height": best_len, "color": _hex(color)}


class MeasureResult(BaseModel):
    width: int
    height: int
    region: tuple[int, int, int, int] | None = None
    sampled_color: str | None = None  # colour at a point, or the dominant colour over a region
    palette: list[tuple[str, float]] = []  # (hex, fraction) most-common first
    background: str | None = None
    foreground: str | None = None
    contrast_ratio: float | None = None
    wcag: dict | None = None  # {"aa_normal", "aa_large", "aaa"} pass flags for contrast_ratio
    contrast_uncertain: bool = False  # True: fg occupancy too low to trust as real text glyphs
    largest_uniform_band: dict | None = None  # {"y", "height", "color"} or None


def measure(
    png: bytes,
    region: tuple[int, int, int, int] | None = None,
    point: tuple[int, int] | None = None,
) -> MeasureResult:
    """Deterministic measurement of a capture. ``point`` → the colour at (x,y). ``region`` (x,y,w,h)
    → dominant colours, the two-colour WCAG contrast, and the largest uniform band within it;
    no region → the whole image."""
    arr = _to_rgb(png)
    h, w = arr.shape[:2]
    res = MeasureResult(width=w, height=h, region=region)

    if point is not None:
        x, y = point
        patch = arr[max(0, y - 2):y + 3, max(0, x - 2):x + 3].reshape(-1, 3)
        if patch.size:
            res.sampled_color = _hex(patch.mean(axis=0))
        return res

    sub = arr
    if region is not None:
        x, y, rw, rh = region
        sub = arr[max(0, y):y + rh, max(0, x):x + rw]
        if sub.size == 0:
            return res

    dom = _dominant_colors(sub, k=4)
    res.palette = [(_hex(c), round(f, 4)) for c, f in dom]
    if dom:
        res.sampled_color = _hex(dom[0][0])
    if len(dom) >= 2:
        bg, (fg, fg_fraction) = dom[0][0], dom[1]  # most pixels = background, next = text/foreground
        res.background, res.foreground = _hex(bg), _hex(fg)
        cr = contrast_ratio(bg, fg)
        res.contrast_ratio = round(cr, 2)
        if fg_fraction < _MIN_FOREGROUND_OCCUPANCY:
            # Too few pixels to be legible glyphs — likely anti-aliasing fringe, a stray icon
            # pixel, or compression noise. Reporting a confident PASS/FAIL off that sliver is
            # exactly the false-violation pattern measure_ui exists to avoid (see module intro);
            # the ratio is kept as information, the verdict is withheld.
            res.contrast_uncertain = True
        else:
            res.wcag = {"aa_normal": cr >= 4.5, "aa_large": cr >= 3.0, "aaa": cr >= 7.0}

    band = _largest_uniform_band(sub)
    if band is not None and region is not None:
        band["y"] += region[1]  # report band y in image coordinates
    res.largest_uniform_band = band
    return res


def format_measure(r: MeasureResult) -> str:
    """One compact, human + agent readable block — exact numbers, no prose."""
    where = f"region {r.region}" if r.region else (f"image {r.width}x{r.height}")
    lines = [f"measure_ui — {where}"]
    if r.sampled_color and not r.contrast_ratio:
        lines.append(f"color: {r.sampled_color}")
    if r.palette:
        lines.append("palette: " + ", ".join(f"{c} ({p:.0%})" for c, p in r.palette))
    if r.contrast_ratio is not None:
        if r.contrast_uncertain:
            flags = "UNCERTAIN (foreground colour too sparse to read as legible text)"
        else:
            wc = r.wcag or {}
            flags = " ".join(
                f"{k.replace('_', ' ').upper()}:{'PASS' if v else 'FAIL'}" for k, v in wc.items()
            )
        lines.append(
            f"contrast: {r.contrast_ratio}:1  (fg {r.foreground} on bg {r.background})  {flags}"
        )
    if r.largest_uniform_band:
        b = r.largest_uniform_band
        lines.append(
            f"largest uniform band: {b['height']}px tall at y={b['y']} ({b['color']}) "
            f"— {b['height'] / r.height:.0%} of height"
        )
    return "\n".join(lines)
