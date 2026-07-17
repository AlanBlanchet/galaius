import base64
import json
import logging
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import litellm
from pydantic import BaseModel

from interact.config import Config
from interact.models import supports_native_video_inline
from interact.state import PageState, bytes_to_b64

_log = logging.getLogger(__name__)


def _log_usage(model: str, response) -> None:
    try:
        from interact.runtime import config

        log = config.usage_log  # under debug_dir, so it relocates with INTERACT_DEBUG_DIR
        log.parent.mkdir(parents=True, exist_ok=True)
        usage = response.usage
        cost = litellm.completion_cost(completion_response=response)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "input_tokens": usage.prompt_tokens if usage else 0,
            "output_tokens": usage.completion_tokens if usage else 0,
            "cost": cost,
        }
        with log.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


class _Unset:
    """Sentinel for unset parameters."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "_UNSET"


_UNSET = _Unset()


class VLMResult(BaseModel):
    text: str
    elapsed: float
    model: str = ""
    truncated: bool = False


class MediaItem(BaseModel):
    data: str
    media_type: Literal["image", "video", "audio"] = "image"
    mime_type: str = "image/png"

    @classmethod
    def from_bytes(
        cls,
        raw: bytes,
        media_type: Literal["image", "video", "audio"] = "image",
        mime_type: str = "image/png",
    ):
        return cls(data=bytes_to_b64(raw), media_type=media_type, mime_type=mime_type)


def _image_content(item: MediaItem) -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{item.mime_type};base64,{item.data}"},
    }


def _audio_ext(mime_type: str) -> str:
    """File extension for an audio/media MIME — so a transcription temp file is named in a way
    the provider can sniff (whisper accepts mp3/mp4/m4a/wav/webm/ogg/flac directly)."""
    m = mime_type.lower()
    for ext in ("wav", "mp3", "m4a", "webm", "ogg", "flac", "mp4"):
        if ext in m:
            return ext
    if "mpeg" in m:
        return "mp3"
    return "mp3"


def _audio_payload(audio_b64: str, mime_type: str) -> tuple[str, str]:
    """``(base64, format)`` for a chat ``input_audio`` part. The chat audio API only takes wav/mp3,
    so wav/mp3 pass through and anything else (webm/mp4/m4a/ogg from a recording or download) is
    transcoded to mp3 with ffmpeg — ``-vn`` drops any video track, leaving just the audio."""
    m = mime_type.lower()
    if "wav" in m:
        return audio_b64, "wav"
    if "mp3" in m or "mpeg" in m:
        return audio_b64, "mp3"
    with tempfile.TemporaryDirectory() as tmpdir:
        src = f"{tmpdir}/in.{_audio_ext(mime_type)}"
        out = f"{tmpdir}/out.mp3"
        Path(src).write_bytes(base64.b64decode(audio_b64))
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-vn", "-acodec", "libmp3lame", out],
            check=True,
            capture_output=True,
        )
        return bytes_to_b64(Path(out).read_bytes()), "mp3"


def _audio_content(item: MediaItem) -> dict:
    data, fmt = _audio_payload(item.data, item.mime_type)
    return {"type": "input_audio", "input_audio": {"data": data, "format": fmt}}


def evenly_sampled(items: list, k: int) -> list:
    """At most ``k`` items, evenly spaced and always including the first and last — the cost cap
    for video frames. ``k <= 0`` means no cap. Keeps the whole list when it's already small."""
    if k <= 0 or len(items) <= k:
        return items
    if k == 1:
        return [items[0]]
    step = (len(items) - 1) / (k - 1)
    return [items[round(i * step)] for i in range(k)]


def _extract_frames(
    video_base64: str, mime_type: str, fps: int = 1, max_frames: int = 0
) -> list[str]:
    """Sample frames from a clip at ``fps``, then cap to ``max_frames`` evenly-spaced frames so
    the VLM cost is bounded by frame count, not clip length."""
    video_bytes = base64.b64decode(video_base64)
    with tempfile.TemporaryDirectory() as tmpdir:
        ext = "mp4" if "mp4" in mime_type else "webm"
        video_path = f"{tmpdir}/input.{ext}"
        Path(video_path).write_bytes(video_bytes)
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-vf", f"fps={fps}", f"{tmpdir}/frame_%03d.jpg"],
            check=True,
            capture_output=True,
        )
        frames = [
            bytes_to_b64(fp.read_bytes())
            for fp in sorted(Path(tmpdir).glob("frame_*.jpg"))
        ]
    return evenly_sampled(frames, max_frames)


def _video_content(item: MediaItem, fps: int = 1, max_frames: int = 0) -> list[dict]:
    frames = _extract_frames(item.data, item.mime_type, fps=fps, max_frames=max_frames)
    return [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{f}"}}
        for f in frames
    ]


# Raw-bytes ceiling for sending a clip as inline base64 to Gemini: its API caps the WHOLE request
# at 20 MB and base64 inflates ~33%, so keep the raw clip well under that (leaving headroom for the
# prompt). A larger clip falls back to frame sampling rather than a guaranteed 400 (#48).
_NATIVE_VIDEO_MAX_BYTES = 15_000_000


def _native_video_part(item: MediaItem) -> dict:
    """litellm's documented native-video content part — an inline base64 `data:` URI under `file`.
    NOT `video_url` (litellm ignores it) and NOT the legacy `image_url` path (#48)."""
    return {"type": "file", "file": {"file_data": f"data:{item.mime_type};base64,{item.data}"}}


def _b64_decoded_size(b64: str) -> int:
    """Decoded byte size of a base64 string without allocating the decoded bytes."""
    return len(b64) * 3 // 4


def _video_ext(mime_type: str) -> str:
    """File extension for a video MIME, matching _extract_frames' container sniffing."""
    return "mp4" if "mp4" in mime_type else "webm"


def _can_upload_to_files_api(model: str) -> bool:
    """Whether an over-cap clip can be uploaded to a Files API and referenced — Gemini AI Studio
    only. Vertex's Files API needs a GCS bucket interact doesn't configure, so a Vertex over-cap
    clip samples instead (#48)."""
    lid = model.lower()
    return "gemini" in lid and "vertex" not in lid


async def _upload_video_ref(item: MediaItem, mime_ext: str) -> dict | None:
    """Upload a clip to the Gemini Files API and return a reference content part (`file_id` = the
    returned file URI), or None if the upload fails — so the caller falls back to frame sampling.
    Lets a clip too large for the ~20 MB inline request cap still be analyzed natively (#48)."""
    try:
        raw = base64.b64decode(item.data)
        uploaded = await litellm.acreate_file(
            file=(f"clip.{mime_ext}", raw, item.mime_type),
            purpose="assistants",  # Gemini's files transform ignores purpose; any valid value works
            custom_llm_provider="gemini",
        )
        uri = getattr(uploaded, "id", None)
        if not uri:
            return None
        return {"type": "file", "file": {"file_id": uri, "format": item.mime_type}}
    except Exception:  # network / key / quota / unexpected shape → sample frames instead
        _log.warning("native video upload failed; falling back to frame sampling", exc_info=True)
        return None


async def _build_media_content(
    media: list[MediaItem], model: str, config: Config, *, force_sampled: bool = False
) -> tuple[list[dict], bool]:
    """Build the message media parts, sending video natively to a Gemini model — inline under the
    size cap, else uploaded to the Files API and referenced — and ffmpeg-sampling frames for
    everything else (non-Gemini providers, an upload failure, or ``force_sampled`` after a native
    send was rejected). Returns ``(parts, sent_native_video)`` (#48)."""
    parts: list[dict] = []
    sent_native_video = False
    for item in media:
        if item.media_type == "image":
            parts.append(_image_content(item))
            continue
        if item.media_type == "audio":
            parts.append(_audio_content(item))
            continue
        # video
        native_part: dict | None = None
        if not force_sampled and supports_native_video_inline(model):
            if _b64_decoded_size(item.data) < _NATIVE_VIDEO_MAX_BYTES:
                native_part = _native_video_part(item)
            elif _can_upload_to_files_api(model):
                native_part = await _upload_video_ref(item, _video_ext(item.mime_type))
        if native_part is not None:
            parts.append(native_part)
            sent_native_video = True
        else:
            parts.extend(
                _video_content(item, fps=config.video_fps, max_frames=config.video_max_frames)
            )
    return parts, sent_native_video


def _supports_response_schema(model: str) -> bool:
    """Whether the provider accepts a native ``response_format`` schema. Some (e.g. zai/GLM) raise
    ``litellm.UnsupportedParamsError`` on it — for those we ask for JSON in the prompt instead, so the
    structured tools run on the model rather than erroring into a frontier fallback (the bug where the
    sovereign tier silently dropped to gemini). Unknown model → False: prompt-JSON works everywhere,
    native is just cleaner where supported."""
    try:
        return bool(litellm.supports_response_schema(model=model))
    except Exception:
        return False


def _schema_instruction(response_format: type[BaseModel] | dict) -> str:
    """A prompt fragment asking for one bare JSON object matching the schema — the fallback for models
    without native ``response_format``. The caller's ``_parse_json_model`` tolerates fences/prose, but
    we still ask for none, and embed the schema so the shape matches what review_ui/verify_ui expect."""
    schema = (
        response_format.model_json_schema()
        if isinstance(response_format, type) and issubclass(response_format, BaseModel)
        else response_format
    )
    return (
        "Respond with ONLY a single JSON object conforming to this JSON Schema — no prose, no markdown "
        "fences, nothing before or after the object:\n" + json.dumps(schema)
    )


async def _vision_completion(
    messages: list[dict],
    model: str,
    max_tokens: int | None = None,
    response_format: type[BaseModel] | dict | None = None,
) -> VLMResult:
    kwargs: dict = {
        "model": model,
        "messages": messages,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if response_format is not None:
        if _supports_response_schema(model):
            kwargs["response_format"] = response_format
        else:
            # Provider rejects a native schema → ask for JSON in the prompt instead (append to the last
            # user message) so THIS model produces the structured output, not a frontier fallback.
            msgs = [dict(m) for m in messages]
            tail = msgs[-1]
            instr = _schema_instruction(response_format)
            content = tail.get("content")
            tail["content"] = (
                content + [{"type": "text", "text": instr}]
                if isinstance(content, list)
                else f"{content}\n\n{instr}"
            )
            kwargs["messages"] = msgs

    t0 = time.monotonic()
    response = await litellm.acompletion(**kwargs)
    elapsed = time.monotonic() - t0

    _log.debug(
        "VLM completion: model=%s max_tokens=%s finish_reason=%s output_tokens=%d text_len=%d",
        kwargs.get("model"),
        kwargs.get("max_tokens"),
        response.choices[0].finish_reason,
        response.usage.completion_tokens if response.usage else -1,
        len(response.choices[0].message.content or ""),
    )
    _log_usage(model, response)

    text = response.choices[0].message.content or ""  # None on refusal/tool-only/empty
    truncated = response.choices[0].finish_reason == "length"
    if truncated:
        text += "\n\n[Response truncated — increase interact.maxTokens]"
    return VLMResult(text=text, elapsed=elapsed, model=model, truncated=truncated)


def _build_messages(content: list[dict], prompt: str | None) -> list[dict]:
    items: list[dict] = []
    if prompt:
        items.append({"type": "text", "text": prompt})
    items.extend(content)
    return [{"role": "user", "content": items}]


async def analyze_media(
    media: list[MediaItem],
    context: str,
    config: Config,
    prompt: str | None = None,
    max_tokens: int | None | _Unset = _UNSET,
    response_format: type[BaseModel] | dict | None = None,
    *,
    model: str,
) -> VLMResult:
    """Run a VLM over media. ``model`` is REQUIRED and already resolved — callers pass the
    output of ``Config.resolve_model`` (the boundary that turns "auto"/a pin/an override into a
    concrete id). There is deliberately no "model unset → friendly note" branch here: an empty id
    cannot reach this function, so the only remaining failure is a genuinely-missing API key,
    surfaced once below."""
    if not litellm.validate_environment(model)["keys_in_environment"]:
        return VLMResult(
            text=f"[Vision unavailable — {model} API key not configured] {context}",
            elapsed=0,
        )
    tok = max_tokens if max_tokens is not _UNSET else config.max_tokens
    media_parts, sent_native_video = await _build_media_content(media, model, config)
    messages = _build_messages([{"type": "text", "text": context}, *media_parts], prompt)
    try:
        return await _vision_completion(
            messages, model, max_tokens=tok, response_format=response_format
        )
    except (
        litellm.exceptions.BadRequestError,
        litellm.exceptions.UnsupportedParamsError,
        litellm.exceptions.APIError,
    ):
        # A native video send can be rejected (provider 400, unsupported, oversized). Retry once
        # with ffmpeg-sampled frames so video analysis is never worse than before #48. For any
        # other call (no native video sent) the error is genuine — re-raise.
        if not sent_native_video:
            raise
        media_parts, _ = await _build_media_content(media, model, config, force_sampled=True)
        messages = _build_messages([{"type": "text", "text": context}, *media_parts], prompt)
        return await _vision_completion(
            messages, model, max_tokens=tok, response_format=response_format
        )


async def transcribe_audio(
    audio_bytes: bytes,
    *,
    model: str,
    mime_type: str = "audio/mpeg",
) -> VLMResult:
    """Speech-to-text for an audio (or audio-bearing) clip via litellm's transcription endpoint
    (``litellm.atranscription`` — a DIFFERENT API from chat ``acompletion``: it routes to Whisper /
    gpt-4o-transcribe / Gemini / Groq / Deepgram). ``model`` is already resolved. Returns the
    transcript as ``VLMResult.text``; a missing key degrades to a friendly note, never a crash."""
    if not litellm.validate_environment(model)["keys_in_environment"]:
        return VLMResult(
            text=f"[Transcription unavailable — {model} API key not configured]", elapsed=0
        )
    t0 = time.monotonic()
    # delete=False + close BEFORE the reopen: Windows can't reopen a NamedTemporaryFile by path
    # while its handle is open (PermissionError) — litellm needs a real file with a suffix (#73).
    tf = tempfile.NamedTemporaryFile(suffix=f".{_audio_ext(mime_type)}", delete=False)
    try:
        tf.write(audio_bytes)
        tf.close()
        with open(tf.name, "rb") as fh:
            response = await litellm.atranscription(model=model, file=fh)
    finally:
        Path(tf.name).unlink(missing_ok=True)
    elapsed = time.monotonic() - t0
    text = getattr(response, "text", None)
    if text is None and isinstance(response, dict):
        text = response.get("text")
    return VLMResult(text=text or "", elapsed=elapsed, model=model)


async def analyze_screenshot(
    state: PageState, config: Config, prompt: str | None = None
) -> VLMResult:
    media = [MediaItem(data=state.screenshot_base64)]
    return await analyze_media(
        media,
        f"Page: {state.title} ({state.url})",
        config,
        prompt,
        model=config.resolve_model("image"),
    )
