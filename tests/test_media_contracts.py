"""Focused contracts for stable media responses and canonical accounting ownership."""

import base64
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from interact.agents.providers import ClaudeCodeProvider, CodexProvider
from interact.server import tools_desktop, vlm
from interact.server.core import mcp
from interact.vision.types import MediaAnalysis, RecordingCapture, RecordingResult, VLMResult
from interact.vision.usage_records import UsageEntry


@pytest.mark.asyncio
async def test_media_response_has_one_stable_shape(monkeypatch) -> None:
    async def analyze(*args, **kwargs) -> VLMResult:
        return VLMResult(
            text="visible", elapsed=0.1, model="", backend="session", provider="claude"
        )

    monkeypatch.setattr(vlm, "_vlm", analyze)
    response = await vlm._media_response(b"image", "context", "describe")
    assert response.text.startswith("visible")
    assert response.result is not None and response.result.provider == "claude"


def test_default_session_diagnostic_names_actual_provider() -> None:
    rendered = vlm._fmt_timing(
        VLMResult(text="ok", elapsed=0, backend="session", provider="claude", model="")
    )
    assert "claude subscription session (provider default)" in rendered.lower()


def test_usage_entry_is_owned_by_the_writer_and_serializes_one_schema() -> None:
    entry = UsageEntry(
        timestamp=datetime.now(UTC), model="claude default", backend="session",
        provider="claude", billing="session_usage", outcome="failed", cost=None,
    )
    payload = entry.model_dump(mode="json")
    assert payload["provider"] == "claude" and payload["cost"] is None
    assert "request_id" in payload and "session_id" in payload


def test_claude_media_argv_flags_exist_in_installed_help() -> None:
    provider = ClaudeCodeProvider()
    if not provider.available():
        pytest.skip("Claude CLI is not installed")
    help_text = provider.media_help_text()
    argv = provider.media_command(
        cwd=Path.cwd(), model="sonnet", media_paths=[Path("staged image.png")],
        schema_path=Path("schema.json"), schema_json='{"type":"object"}',
        mcp_config=Path("mcp.json"), settings_path=Path("settings.json"),
    )
    assert all(flag in help_text for flag in argv if flag.startswith("--"))


def test_codex_has_no_media_execution_surface() -> None:
    assert "media_command" not in CodexProvider.__dict__
    assert "media_isolation_args" not in CodexProvider.__dict__
    assert "media_model_field" not in CodexProvider.__dict__
    assert "no_extra_usage_guidance" not in CodexProvider.__dict__
    provider = CodexProvider()
    assert not hasattr(provider, "media_command")
    assert not hasattr(provider, "run_media_process")


@pytest.mark.asyncio
async def test_record_mcp_separates_capture_evidence_from_optional_analysis() -> None:
    record_tool = next(tool for tool in await mcp.list_tools() if tool.name == "record")
    schema = record_tool.outputSchema
    assert schema is not None
    assert set(schema.get("required", ())) >= {"capture", "analysis"}
    capture = schema["$defs"][schema["properties"]["capture"]["$ref"].rsplit("/", 1)[-1]]
    analysis = schema["$defs"][schema["properties"]["analysis"]["$ref"].rsplit("/", 1)[-1]]
    assert {"artifact", "measured_fps", "duration"} <= set(
        capture.get("properties", {})
    )
    assert {"status", "eligible", "attempted", "input_kind", "sample_timestamps"} <= set(
        analysis.get("properties", {})
    )


@pytest.mark.asyncio
async def test_record_returns_timestamped_frames_without_requesting_analysis(monkeypatch) -> None:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    async def sample(*args, **kwargs):
        kwargs["_timestamp_basis"].append("source_pts")
        return [(png, 0.0), (png + b"different", 0.25)]

    async def forbidden(*args, **kwargs):
        raise AssertionError("a capture-only request reached optional analysis")

    monkeypatch.setattr(tools_desktop, "sample_video_frames", sample)
    monkeypatch.setattr(vlm, "_vlm", forbidden)
    result = await tools_desktop._record_response(
        b"video", fps=9, mime="video/mp4", path=None, context="capture", query=None,
    )

    assert result.capture.requested_fps == 9
    assert result.capture.measured_fps is None
    assert result.capture.timestamp_basis == "source_pts"
    assert result.capture.max_frame_gap == 0.25
    assert result.capture.frame_timestamps == [0.0, 0.25]
    assert result.analysis.status == "not_requested"
    assert result.analysis.sample_timestamps == []
    assert not result.analysis.attempted


@pytest.mark.asyncio
async def test_record_mcp_returns_frames_as_direct_image_content(monkeypatch) -> None:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    async def browser(*args, **kwargs):
        return await tools_desktop._record_response(
            b"video", fps=4, mime="video/mp4", path=None, context="capture", query=None,
        )

    async def sample(*args, **kwargs):
        return [(png, 0.0), (png, 0.25)]

    monkeypatch.setattr(tools_desktop.targets, "_resolve_target", lambda *args: (None, object(), None))
    monkeypatch.setattr(tools_desktop, "_record_browser", browser)
    monkeypatch.setattr(tools_desktop, "sample_video_frames", sample)

    result = await mcp.call_tool("record", {"start": False})

    assert result.structuredContent["capture"]["status"] == "captured"
    assert result.structuredContent["capture"]["frame_timestamps"] == [0.0, 0.25]
    assert "data" not in str(result.structuredContent)
    images = [content for content in result.content if content.type == "image"]
    assert len(images) == len(result.structuredContent["capture"]["frame_timestamps"])
    assert [image.data for image in images] == [base64.b64encode(png).decode()] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("cap", "truncated"), [(2, True), (20, False)])
async def test_real_ffmpeg_record_mcp_separates_sample_cadence_from_encoded_fps(
    monkeypatch, tmp_path: Path, cap: int, truncated: bool,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is unavailable")
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i",
         "testsrc=size=32x32:rate=10:duration=1", "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )
    win = MagicMock()
    win.stop_video.return_value = clip.read_bytes()
    win.name, win.w, win.h = "isolated", 32, 32
    monkeypatch.setattr(tools_desktop.config, "video_max_frames", cap)
    monkeypatch.setattr(tools_desktop.config, "video_fps", 5)
    monkeypatch.setattr(
        tools_desktop.targets, "_resolve_target", lambda *args: (win, None, None),
    )

    result = await mcp.call_tool("record", {"start": False, "target": "isolated"})

    capture = result.structuredContent["capture"]
    images = [content for content in result.content if content.type == "image"]
    assert capture["timestamp_basis"] == "derived_cadence"
    assert capture["requested_fps"] == 5
    assert capture["measured_fps"] == 10
    assert capture["duration"] == pytest.approx(1)
    assert capture["frames_truncated"] is truncated
    assert len(images) == len(capture["frame_timestamps"])
    assert len(images) <= cap


def test_record_drops_unpaired_frame_evidence() -> None:
    result = RecordingResult(
        capture=RecordingCapture(
            status="captured",
            requested_fps=4,
            timestamp_basis="source_pts",
            observation="observed",
            frame_timestamps=[0.0],
        ),
        analysis=MediaAnalysis(
            status="not_requested",
            eligible=None,
            attempted=False,
            input_kind="none",
        ),
    )
    result._frame_bytes = [b"first", b"unpaired"]

    response = tools_desktop._record_tool_result(result)

    assert response.structuredContent["capture"]["frame_timestamps"] == []
    assert response.structuredContent["capture"]["observation"] == "indeterminate"
    assert not [item for item in response.content if item.type == "image"]


@pytest.mark.asyncio
async def test_native_analysis_eligibility_does_not_depend_on_frame_extraction(monkeypatch) -> None:
    async def unavailable(*args, **kwargs):
        raise RuntimeError("capture evidence extraction unavailable")

    async def native(*args, **kwargs):
        return VLMResult(
            text="native result", elapsed=0, backend="api", video_sampled=False,
            dispatch_eligible=True, dispatch_attempted=True, dispatch_status="completed",
        )

    monkeypatch.setattr(tools_desktop, "sample_video_frames", unavailable)
    monkeypatch.setattr(vlm, "_vlm", native)
    result = await tools_desktop._record_response(
        b"video", fps=4, mime="video/mp4", path=None, context="capture", query="describe",
    )

    assert result.capture.observation == "indeterminate"
    assert result.analysis.status == "completed"
    assert result.analysis.eligible is True and result.analysis.attempted is True
    assert result.analysis.input_kind == "native_video"
    assert result.analysis.sample_timestamps == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempted", "status", "eligible"),
    [(False, "unavailable", False), (True, "failed", True)],
)
async def test_vlm_dispatch_failure_state_is_set_at_the_transport_boundary(
    monkeypatch, attempted: bool, status: str, eligible: bool,
) -> None:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    async def fail(*args, **kwargs):
        if attempted:
            kwargs["_dispatch_state"].append(True)
        raise RuntimeError("controlled route failure")

    monkeypatch.setattr(vlm, "analyze_media", fail)
    monkeypatch.setattr(vlm, "blank_frame_reason", lambda data: None)
    result = await vlm._vlm(png, "context", "describe", "image", "image/png")

    assert result.dispatch_status == status
    assert result.dispatch_eligible is eligible
    assert result.dispatch_attempted is attempted
