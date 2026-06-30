"""#48: send NATIVE video to models that accept it (Gemini family, inline_data), not only
ffmpeg-sampled frames — with a safe fall back to frame sampling for every other model and if a
native send is rejected. The capability gate is a positive allowlist because an unsupported
provider SILENTLY DROPS a video part (HTTP 200 + a hallucinated answer), never an error to catch."""

import base64

import pytest

import interact.vision as vision
from interact.config import Config
from interact.vision import MediaItem, VLMResult, _build_media_content

_RAW = b"\x00\x01\x02\x03"


def _video(raw: bytes = _RAW) -> MediaItem:
    return MediaItem(data=base64.b64encode(raw).decode(), media_type="video", mime_type="video/mp4")


def _data_uri(raw: bytes = _RAW) -> str:
    return f"data:video/mp4;base64,{base64.b64encode(raw).decode()}"


def test_native_video_part_for_a_gemini_model_under_the_size_cap():
    content, sent_native = _build_media_content([_video()], "gemini/gemini-3.5-flash", Config())
    assert sent_native
    assert content == [{"type": "file", "file": {"file_data": _data_uri()}}]


def test_non_gemini_video_model_still_gets_sampled_frames(monkeypatch):
    """Qwen-VL is native-video-capable but litellm has no inline-video transform for it → sampling."""
    monkeypatch.setattr(vision, "_extract_frames", lambda *a, **k: ["FRAME_B64"])
    content, sent_native = _build_media_content(
        [_video()], "nebius/Qwen/Qwen2.5-VL-72B-Instruct", Config()
    )
    assert not sent_native
    assert content == [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,FRAME_B64"}}]


def test_gemini_clip_over_the_size_cap_falls_back_to_frames(monkeypatch):
    """A clip near/over Gemini's 20 MB inline request cap must sample, not send base64 inline."""
    monkeypatch.setattr(vision, "_NATIVE_VIDEO_MAX_BYTES", 8)
    monkeypatch.setattr(vision, "_extract_frames", lambda *a, **k: ["F"])
    big = _video(b"x" * 64)  # 64 raw bytes > cap of 8
    content, sent_native = _build_media_content([big], "gemini/gemini-3.5-flash", Config())
    assert not sent_native and content[0]["type"] == "image_url"


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
