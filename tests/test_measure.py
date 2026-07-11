"""measure_ui — DETERMINISTIC pixel measurement (no VLM). The verdict agents actually trust in real
usage was a WCAG contrast number computed on raw pixels, not the model's prose. So the numbers must be
EXACT (quantization must not skew a borderline AA verdict) and the tool must work on a captured target
or a saved file."""

import io

import numpy as np
import pytest
from PIL import Image

import interact.server as srv
from interact.vision.measure import contrast_ratio, format_measure, measure


def _png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8)).save(buf, "PNG")
    return buf.getvalue()


def _ascapture(data: bytes):
    async def _f(*a, **k):
        return data
    return _f


@pytest.mark.parametrize(
    "c1, c2, expected",
    [
        ((0, 0, 0), (255, 255, 255), 21.0),       # max contrast
        ((255, 255, 255), (255, 255, 255), 1.0),  # identical
        ((0x76, 0x76, 0x76), (255, 255, 255), 4.54),  # AA-normal boundary (4.5) — accuracy matters
        ((0x77, 0x77, 0x77), (255, 255, 255), 4.48),  # just under → must read as FAIL
    ],
)
def test_contrast_ratio_is_exact(c1, c2, expected):
    assert round(contrast_ratio(c1, c2), 2) == expected


def test_measure_palette_and_contrast_are_exact_not_quantized():
    arr = np.full((100, 200, 3), 255)
    arr[40:60, 10:190] = 0  # black text-ish band on white
    r = measure(_png(arr))
    assert r.palette[0][0] == "#ffffff" and r.palette[1][0] == "#000000"
    assert r.contrast_ratio == 21.0  # NOT 18.9 — dominant colours are exact, not bucket centres
    assert r.wcag == {"aa_normal": True, "aa_large": True, "aaa": True}


def test_measure_borderline_contrast_verdict_is_correct():
    arr = np.full((50, 60, 3), 255)
    arr[:, :20] = [0x76, 0x76, 0x76]  # 4.54:1 — passes AA-normal
    r = measure(_png(arr))
    assert r.contrast_ratio == 4.54 and r.wcag["aa_normal"] is True


def test_measure_point_samples_the_exact_color():
    arr = np.zeros((20, 20, 3))
    arr[:] = [10, 20, 30]
    assert measure(_png(arr), point=(5, 5)).sampled_color == "#0a141e"


def test_uniform_band_breaks_on_color_change_not_just_texture():
    """A solid coloured header + white below = TWO bands; the white one (larger) is reported, not one
    run spanning both (each row is internally flat, but the colour changes)."""
    arr = np.full((100, 100, 3), 255)
    arr[0:30] = [200, 50, 50]  # red header
    band = measure(_png(arr)).largest_uniform_band
    assert band["height"] == 70 and band["color"] == "#ffffff"


def test_uniform_band_y_is_reported_in_image_coords_for_a_region():
    arr = np.full((200, 50, 3), 255)
    arr[120:170] = 0  # black band low in the image
    r = measure(_png(arr), region=(0, 100, 50, 100))  # measure only the lower half
    assert r.largest_uniform_band["y"] >= 100  # region origin added back


def test_format_measure_is_compact_and_carries_the_numbers():
    arr = np.full((40, 40, 3), 255)
    arr[:, :10] = 0
    out = format_measure(measure(_png(arr)))
    assert "contrast:" in out and "21.0:1" in out and "AAA:PASS" in out


# ── the measure_ui tool ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_measure_ui_on_a_captured_target(monkeypatch):
    arr = np.full((50, 80, 3), 255)
    arr[:, :30] = 0
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda target, session: (None, object(), None))
    monkeypatch.setattr(srv.capture, "_capture_target_png", _ascapture(_png(arr)))
    out = await srv.measure_ui(region="0,0,80,50")
    assert "contrast:" in out and "21.0:1" in out


@pytest.mark.asyncio
async def test_measure_ui_on_a_file_target(monkeypatch, tmp_path):
    arr = np.full((30, 30, 3), 255)
    arr[:, :10] = 0
    p = tmp_path / "shot.png"
    p.write_bytes(_png(arr))
    out = await srv.measure_ui(target=f"file:{p}")
    assert "Image file:" in out and "#ffffff" in out


@pytest.mark.asyncio
async def test_measure_ui_rejects_malformed_region():
    out = await srv.measure_ui(target="file:/x", region="1,2,3")
    assert out.startswith("ERROR") and "region needs 4" in out
