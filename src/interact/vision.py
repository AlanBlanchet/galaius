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
    media_type: Literal["image", "video"] = "image"
    mime_type: str = "image/png"

    @classmethod
    def from_bytes(
        cls,
        raw: bytes,
        media_type: Literal["image", "video"] = "image",
        mime_type: str = "image/png",
    ):
        return cls(data=bytes_to_b64(raw), media_type=media_type, mime_type=mime_type)


def _image_content(item: MediaItem) -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{item.mime_type};base64,{item.data}"},
    }


def _extract_frames(video_base64: str, mime_type: str, fps: int = 1) -> list[str]:
    video_bytes = base64.b64decode(video_base64)
    with tempfile.TemporaryDirectory() as tmpdir:
        ext = "mp4" if "mp4" in mime_type else "webm"
        video_path = f"{tmpdir}/input.{ext}"
        Path(video_path).write_bytes(video_bytes)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                video_path,
                "-vf",
                f"fps={fps}",
                f"{tmpdir}/frame_%03d.jpg",
            ],
            check=True,
            capture_output=True,
        )
        return [
            bytes_to_b64(fp.read_bytes())
            for fp in sorted(Path(tmpdir).glob("frame_*.jpg"))
        ]


def _video_content(item: MediaItem) -> list[dict]:
    frames = _extract_frames(item.data, item.mime_type)
    return [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{f}"}}
        for f in frames
    ]


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
        kwargs["response_format"] = response_format

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
    config_media_type: str | None = None,
    model_override: str | None = None,
) -> VLMResult:
    has_video = any(m.media_type == "video" for m in media)
    routing = config_media_type or ("video" if has_video else "image")
    if model_override:
        model = model_override
    else:
        model = config.model_for(routing)
    if not model:
        return VLMResult(
            text="[Vision not configured — select a model in VS Code settings or set INTERACT_IMAGE_MODEL]",
            elapsed=0,
        )
    if not litellm.validate_environment(model)["keys_in_environment"]:
        return VLMResult(
            text=f"[Vision unavailable — {model} API key not configured] {context}",
            elapsed=0,
        )
    content: list[dict] = [{"type": "text", "text": context}]
    for item in media:
        if item.media_type == "image":
            content.append(_image_content(item))
        else:
            content.extend(_video_content(item))
    tok = max_tokens if max_tokens is not _UNSET else config.max_tokens
    messages = _build_messages(content, prompt)
    return await _vision_completion(
        messages,
        model,
        max_tokens=tok,
        response_format=response_format,
    )


async def analyze_screenshot(
    state: PageState, config: Config, prompt: str | None = None
) -> VLMResult:
    media = [MediaItem(data=state.screenshot_base64)]
    return await analyze_media(
        media,
        f"Page: {state.title} ({state.url})",
        config,
        prompt,
    )
