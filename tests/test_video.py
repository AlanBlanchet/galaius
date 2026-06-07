"""Video understanding of an interaction, cost-bounded.

Two things matter: a recording is sampled down to a fixed frame budget before the VLM sees it (so
spend is bounded by frame count, not clip length), and run_actions can record a sequence and have
a model describe the flow."""

import base64
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from interact.vision import _extract_frames, evenly_sampled


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_extract_frames_real_ffmpeg_respects_budget():
    """End-to-end with real ffmpeg: a clip is decoded to frames and the budget actually caps the
    count (the cost guarantee), while no cap returns the full sampling."""
    with tempfile.TemporaryDirectory() as d:
        clip = Path(d) / "clip.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "testsrc=duration=3:size=160x120:rate=10", "-pix_fmt", "yuv420p", str(clip)],
            check=True, capture_output=True,
        )
        data = base64.b64encode(clip.read_bytes()).decode()
        capped = _extract_frames(data, "video/mp4", fps=5, max_frames=6)
        uncapped = _extract_frames(data, "video/mp4", fps=5, max_frames=0)
    assert len(capped) == 6  # ~15 sampled frames → capped to the budget
    assert all(isinstance(f, str) and f for f in capped)  # base64 JPEGs
    assert len(uncapped) > 6  # without a cap, the full fps sampling comes through


@pytest.mark.parametrize(
    "n, k, expected_len, first_last_kept",
    [
        (100, 12, 12, True),   # long clip → capped to the budget
        (5, 12, 5, True),      # short clip → kept whole
        (12, 12, 12, True),
        (50, 1, 1, False),     # k=1 → just the first frame
        (50, 0, 50, True),     # k<=0 → no cap
    ],
)
def test_evenly_sampled_bounds_frames(n, k, expected_len, first_last_kept):
    items = list(range(n))
    out = evenly_sampled(items, k)
    assert len(out) == expected_len
    assert out == sorted(out)  # order preserved, evenly spaced
    if first_last_kept and expected_len > 1:
        assert out[0] == 0 and out[-1] == n - 1  # endpoints anchored


def test_evenly_sampled_is_evenly_spaced():
    out = evenly_sampled(list(range(100)), 5)
    assert out == [0, 25, 50, 74, 99]  # spread across the whole clip


@pytest.mark.asyncio
async def test_record_analyzes_per_step_frames_capped_with_query():
    """record captures one frame per step; a long interaction is sampled to the frame budget and
    the whole sequence (carrying the query) goes to the video model — so it reads the flow."""
    import interact.server as srv

    frames = [f"frame{i}".encode() for i in range(100)]  # 100 steps
    captured: dict = {}

    async def fake_analyze(media, context, config, prompt=None, **kw):
        captured["n"] = len(media)
        captured["prompt"] = prompt
        return srv.VLMResult(text="logged in, then opened settings", elapsed=0.1)

    with patch.object(srv, "analyze_media", fake_analyze):
        out = await srv._analyze_interaction_frames(frames, "what happened?")

    assert captured["n"] == 12  # capped to config.video_max_frames (default)
    assert captured["prompt"] == "what happened?"
    assert "[recording: 12 frames]" in out and "opened settings" in out


@pytest.mark.asyncio
async def test_record_keeps_every_frame_for_short_interactions():
    """The common case — a short sequence keeps a frame for every step, nothing dropped."""
    import interact.server as srv

    captured: dict = {}

    async def fake_analyze(media, *a, prompt=None, **kw):
        captured["n"] = len(media)
        return srv.VLMResult(text="ok", elapsed=0.1)

    with patch.object(srv, "analyze_media", fake_analyze):
        await srv._analyze_interaction_frames([b"a", b"b", b"c"], None)
    assert captured["n"] == 3  # every step kept
