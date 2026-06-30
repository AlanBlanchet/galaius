"""#48: send NATIVE video to models that accept it instead of only ffmpeg-sampled frames.
Small clips go inline (Gemini `inline_data`); a clip too large for the inline request cap is
uploaded to the Gemini Files API and sent by reference; everything else — non-Gemini providers
(litellm has no inline-video transform and would silently drop the part), Vertex (needs a GCS
bucket interact doesn't have), an upload failure, or a completion rejection — falls back to frame
sampling, so video analysis is never worse than before."""

import base64
from types import SimpleNamespace

import pytest

import interact.vision as vision
from interact.config import Config
from interact.vision import MediaItem, VLMResult, _build_media_content

_RAW = b"\x00\x01\x02\x03"


def _video(raw: bytes = _RAW) -> MediaItem:
    return MediaItem(data=base64.b64encode(raw).decode(), media_type="video", mime_type="video/mp4")


def _data_uri(raw: bytes = _RAW) -> str:
    return f"data:video/mp4;base64,{base64.b64encode(raw).decode()}"


@pytest.mark.asyncio
async def test_native_inline_video_part_for_a_gemini_model_under_the_size_cap():
    content, sent_native = await _build_media_content([_video()], "gemini/gemini-3.5-flash", Config())
    assert sent_native
    assert content == [{"type": "file", "file": {"file_data": _data_uri()}}]


@pytest.mark.asyncio
async def test_non_gemini_video_model_still_gets_sampled_frames(monkeypatch):
    """Qwen-VL is native-video-capable but litellm has no inline-video transform for it → sampling."""
    monkeypatch.setattr(vision, "_extract_frames", lambda *a, **k: ["FRAME_B64"])
    content, sent_native = await _build_media_content(
        [_video()], "nebius/Qwen/Qwen2.5-VL-72B-Instruct", Config()
    )
    assert not sent_native
    assert content == [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,FRAME_B64"}}]


@pytest.mark.asyncio
async def test_oversized_gemini_clip_is_uploaded_and_referenced_by_uri(monkeypatch):
    """A clip over the inline cap is uploaded to the Gemini Files API and sent by reference instead
    of sampled — so a long recording is still analyzed natively (the #48 URL/Files-API extension)."""
    monkeypatch.setattr(vision, "_NATIVE_VIDEO_MAX_BYTES", 8)
    uploaded: dict = {}

    async def fake_acreate_file(*, file, purpose, custom_llm_provider, **k):
        uploaded.update(name=file[0], provider=custom_llm_provider, n=len(file[1]))
        return SimpleNamespace(id="https://generativelanguage.googleapis.com/v1beta/files/abc")

    monkeypatch.setattr(vision.litellm, "acreate_file", fake_acreate_file)
    big = _video(b"x" * 64)  # > cap of 8
    content, sent_native = await _build_media_content([big], "gemini/gemini-3.5-flash", Config())
    assert sent_native
    assert content == [{
        "type": "file",
        "file": {"file_id": "https://generativelanguage.googleapis.com/v1beta/files/abc",
                 "format": "video/mp4"},
    }]
    assert uploaded["provider"] == "gemini" and uploaded["n"] == 64  # raw bytes uploaded, not base64


@pytest.mark.asyncio
async def test_oversized_gemini_clip_falls_back_to_frames_when_upload_fails(monkeypatch):
    monkeypatch.setattr(vision, "_NATIVE_VIDEO_MAX_BYTES", 8)
    monkeypatch.setattr(vision, "_extract_frames", lambda *a, **k: ["F"])

    async def boom(**k):
        raise RuntimeError("upload failed")

    monkeypatch.setattr(vision.litellm, "acreate_file", boom)
    content, sent_native = await _build_media_content([_video(b"x" * 64)], "gemini/gemini-2.5-pro", Config())
    assert not sent_native and content[0]["type"] == "image_url"


@pytest.mark.asyncio
async def test_oversized_vertex_clip_samples_no_files_api(monkeypatch):
    """Vertex's Files API needs a GCS bucket interact doesn't configure, so an over-cap Vertex clip
    samples rather than attempting an upload."""
    monkeypatch.setattr(vision, "_NATIVE_VIDEO_MAX_BYTES", 8)
    monkeypatch.setattr(vision, "_extract_frames", lambda *a, **k: ["F"])
    called = False

    async def fake_acreate_file(**k):
        nonlocal called
        called = True
        return SimpleNamespace(id="x")

    monkeypatch.setattr(vision.litellm, "acreate_file", fake_acreate_file)
    content, sent_native = await _build_media_content([_video(b"x" * 64)], "vertex_ai/gemini-2.5-flash", Config())
    assert not sent_native and content[0]["type"] == "image_url" and called is False


@pytest.mark.asyncio
async def test_analyze_falls_back_to_frames_when_native_video_is_rejected(monkeypatch):
    """Backstop: if a native send raises (provider 400 / unsupported / oversized), retry once with
    sampled frames so video analysis is never worse than before #48."""
    import litellm

    monkeypatch.setattr(vision, "_extract_frames", lambda *a, **k: ["F"])
    monkeypatch.setattr(
        vision.litellm, "validate_environment", lambda model: {"keys_in_environment": True}
    )
    calls: list[str] = []

    async def fake_completion(messages, model, **k):
        parts = [p for m in messages for p in (m["content"] if isinstance(m["content"], list) else [])]
        kinds = [p.get("type") for p in parts]
        calls.append("file" if "file" in kinds else "frames")
        if "file" in kinds:
            raise litellm.exceptions.BadRequestError("unsupported", "gemini", "gemini/x")
        return VLMResult(text="described from frames", elapsed=0.1)

    monkeypatch.setattr(vision, "_vision_completion", fake_completion)
    out = await vision.analyze_media([_video()], "context", Config(), model="gemini/gemini-3.5-flash")
    assert out.text == "described from frames"
    assert calls == ["file", "frames"]  # native first, sampled fallback second
