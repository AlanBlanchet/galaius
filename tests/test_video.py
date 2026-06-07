"""Video understanding of an interaction, cost-bounded.

Two things matter: a recording is sampled down to a fixed frame budget before the VLM sees it (so
spend is bounded by frame count, not clip length), and run_actions can record a sequence and have
a model describe the flow."""

from unittest.mock import AsyncMock, patch

import pytest

from interact.vision import evenly_sampled


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
async def test_run_actions_record_analyzes_the_recording_with_the_query():
    """record=True wraps the sequence in a recording context and sends the video (carrying the
    query) to the VLM — so the result describes the flow, not just the end state."""
    import interact.server as srv

    mgr = AsyncMock()
    mgr.start_recording = AsyncMock(return_value="about:blank")
    mgr.stop_recording = AsyncMock(return_value=b"WEBMDATA")
    vlm = AsyncMock(return_value=srv.VLMResult(text="user logged in then opened settings", elapsed=0.2))

    with (
        patch.object(srv, "_run_actions_browser", AsyncMock(return_value="[session: default]\nStep 1…")),
        patch.object(srv, "_vlm", vlm),
    ):
        out = await srv._run_actions_browser_recorded(
            mgr, [], "what happened?", None, None, "default", None
        )

    mgr.start_recording.assert_awaited_once()
    mgr.stop_recording.assert_awaited_once()
    # the video bytes + the query went to the VLM as a video
    assert vlm.await_args.args[0] == b"WEBMDATA"
    assert vlm.await_args.args[2] == "what happened?"
    assert vlm.await_args.args[3] == "video"
    assert "[recording]" in out and "logged in then opened settings" in out


@pytest.mark.asyncio
async def test_run_actions_record_handles_no_video():
    import interact.server as srv

    mgr = AsyncMock()
    mgr.start_recording = AsyncMock(return_value="about:blank")
    mgr.stop_recording = AsyncMock(return_value=b"")
    with (
        patch.object(srv, "_run_actions_browser", AsyncMock(return_value="ok")),
        patch.object(srv, "_vlm", AsyncMock()) as vlm,
    ):
        out = await srv._run_actions_browser_recorded(mgr, [], None, None, None, "default", None)
    assert "no video captured" in out
    vlm.assert_not_called()  # nothing to analyse → no spend
