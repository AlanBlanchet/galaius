"""Validated media inputs and normalized vision results shared by API and session transports."""

import base64
import binascii
import json
import math
import os
from pathlib import Path
from typing import Any, Literal, Self

import jsonschema
from pydantic import BaseModel, Field, PrivateAttr, field_validator

from interact.state import bytes_to_b64

_MEDIA_MAX_BYTES = 50 * 1024 * 1024


def _compile_response_schema(
    response_format: type[BaseModel] | dict[str, Any] | None,
) -> dict[str, Any] | None:
    if response_format is None:
        return None
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        schema = response_format.model_json_schema()
    elif isinstance(response_format, dict):
        schema = response_format
    else:
        raise TypeError("response_format must be a Pydantic model class or JSON Schema object")
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


class VLMResult(BaseModel):
    text: str
    elapsed: float
    model: str = ""
    truncated: bool = False
    backend: Literal["session", "api", "none"] = "none"
    provider: str = ""
    billing: Literal["session_usage", "metered_api", "none"] = "none"
    incremental_cost_usd: float | None = None
    api_equivalent_cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    session_id: str | None = None
    request_id: str | None = None
    video_sampled: bool | None = None
    video_sample_timestamps: list[float] = Field(default_factory=list)
    dispatch_eligible: bool | None = None
    dispatch_attempted: bool = False
    dispatch_status: Literal["not_requested", "completed", "unavailable", "failed"] = "not_requested"

    def validated(self, response_format: type[BaseModel] | dict[str, Any] | None) -> Self:
        """Compile and enforce one structured-output contract for every transport."""
        schema = _compile_response_schema(response_format)
        if schema is None:
            return self
        if isinstance(response_format, type) and issubclass(response_format, BaseModel):
            text = response_format.model_validate_json(self.text).model_dump_json()
        else:
            value = json.loads(self.text)
            jsonschema.Draft202012Validator(schema).validate(value)
            text = json.dumps(value, separators=(",", ":"))
        return self.model_copy(update={"text": text})


class MediaItem(BaseModel):
    data: str
    media_type: Literal["image", "video", "audio"] = "image"
    mime_type: str = "image/png"
    timestamp_seconds: float | None = None

    @field_validator("data")
    @classmethod
    def _valid_base64(cls, value: str) -> str:
        if len(value) > ((_MEDIA_MAX_BYTES + 2) // 3) * 4:
            raise ValueError(f"base64 media exceeds {_MEDIA_MAX_BYTES} bytes")
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("media data is not valid base64") from exc
        return value


    @field_validator("timestamp_seconds")
    @classmethod
    def _valid_timestamp(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("media timestamp must be finite")
        if value is not None and value < 0:
            raise ValueError("media timestamp cannot be negative")
        return value

    @classmethod
    def validate_collection(
        cls,
        items: list[Self],
        *,
        max_items: int,
        max_total_bytes: int,
    ) -> None:
        """Validate every item's signature and cap aggregate request size before transport."""
        if len(items) > max_items:
            raise ValueError(f"media item count {len(items)} exceeds {max_items}")
        total = 0
        for item in items:
            total += len(item.decoded())
            if total > max_total_bytes:
                raise ValueError(
                    f"media aggregate decoded size exceeds {max_total_bytes} bytes"
                )

    @classmethod
    def from_bytes(
        cls,
        raw: bytes,
        media_type: Literal["image", "video", "audio"] = "image",
        mime_type: str | None = None,
    ) -> Self:
        mime = mime_type or cls.detect_mime(raw, media_type)
        return cls(data=bytes_to_b64(raw), media_type=media_type, mime_type=mime)

    @staticmethod
    def detect_mime(
        raw: bytes, media_type: Literal["image", "video", "audio"] = "image"
    ) -> str:
        """Infer a supported MIME from magic bytes; never infer from a filename."""
        candidates = {
            "image": ("image/png", "image/jpeg", "image/gif", "image/webp"),
            "video": ("video/mp4", "video/webm"),
            "audio": ("audio/mpeg", "audio/wav", "audio/webm", "audio/flac", "audio/mp4"),
        }[media_type]
        probe = MediaItem(data=bytes_to_b64(raw), media_type=media_type)
        for mime in candidates:
            probe.mime_type = mime
            if probe._matches_mime(raw):
                return mime
        raise ValueError(f"unsupported or malformed {media_type} media signature")

    def decoded(self, *, max_bytes: int = _MEDIA_MAX_BYTES) -> bytes:
        """Validated raw bytes, bounded before allocation and checked against declared MIME."""
        padding = len(self.data) - len(self.data.rstrip("="))
        decoded_size = len(self.data) // 4 * 3 - padding
        if decoded_size > max_bytes:
            raise ValueError(f"decoded media exceeds {max_bytes} bytes")
        try:
            raw = base64.b64decode(self.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("media data is not valid base64") from exc
        if len(raw) > max_bytes:
            raise ValueError(f"decoded media exceeds {max_bytes} bytes")
        if not self._matches_mime(raw):
            raise ValueError(
                f"{self.media_type} data does not match declared MIME type {self.mime_type}"
            )
        return raw

    def _matches_mime(self, raw: bytes) -> bool:
        mime = self.mime_type.lower().split(";", 1)[0].strip()
        if self.media_type == "image":
            if mime == "image/png":
                return raw.startswith(b"\x89PNG\r\n\x1a\n")
            if mime in ("image/jpeg", "image/jpg"):
                return raw.startswith(b"\xff\xd8\xff")
            if mime == "image/gif":
                return raw.startswith((b"GIF87a", b"GIF89a"))
            if mime == "image/webp":
                return raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
            return False
        if self.media_type == "video":
            if mime == "video/mp4":
                return len(raw) >= 12 and raw[4:8] == b"ftyp"
            if mime in ("video/webm", "video/x-matroska"):
                return raw.startswith(b"\x1aE\xdf\xa3")
            return False
        if mime in ("audio/mpeg", "audio/mp3"):
            if raw.startswith(b"ID3"):
                return True
            if len(raw) < 3 or raw[0] != 0xFF or raw[1] & 0xE0 != 0xE0:
                return False
            version = (raw[1] >> 3) & 0b11
            layer = (raw[1] >> 1) & 0b11
            bitrate = raw[2] >> 4
            sample_rate = (raw[2] >> 2) & 0b11
            return version != 0b01 and layer != 0 and bitrate not in (0, 0xF) and sample_rate != 0b11
        if mime in ("audio/wav", "audio/x-wav"):
            return raw.startswith(b"RIFF") and raw[8:12] == b"WAVE"
        if mime in ("audio/webm", "audio/ogg"):
            return raw.startswith((b"\x1aE\xdf\xa3", b"OggS"))
        if mime == "audio/flac":
            return raw.startswith(b"fLaC")
        if mime in ("audio/mp4", "audio/m4a", "audio/x-m4a"):
            return len(raw) >= 12 and raw[4:8] == b"ftyp"
        return False

    def extension(self) -> str:
        mime = self.mime_type.lower().split(";", 1)[0]
        if self.media_type == "image":
            return "jpg" if mime in ("image/jpeg", "image/jpg") else mime.removeprefix("image/")
        if self.media_type == "video":
            return "mp4" if mime == "video/mp4" else "webm"
        if "wav" in mime:
            return "wav"
        if "webm" in mime:
            return "webm"
        if "ogg" in mime:
            return "ogg"
        if "flac" in mime:
            return "flac"
        if "mp4" in mime or "m4a" in mime:
            return "m4a"
        return "mp3"

    def stage(self, path: Path, *, max_bytes: int = _MEDIA_MAX_BYTES) -> Path:
        """Write validated media atomically as a new mode-0600 file."""
        raw = self.decoded(max_bytes=max_bytes)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path


class RecordingCapture(BaseModel):
    status: Literal["started", "captured", "unavailable"]
    artifact: str | None = None
    requested_fps: int
    measured_fps: float | None = None
    duration: float | None = None
    max_frame_gap: float | None = None
    timestamp_basis: Literal["source_pts", "derived_cadence"]
    observation: Literal["observed", "not_observed", "indeterminate"] = Field(
        description="Change among returned samples only; bounded by max_frame_gap and frames_truncated.",
    )
    frames_truncated: bool = False
    frame_timestamps: list[float] = Field(default_factory=list)


class MediaAnalysis(BaseModel):
    status: Literal["not_requested", "completed", "unavailable", "failed"]
    eligible: bool | None
    attempted: bool
    input_kind: Literal["none", "native_video", "sampled_frames"]
    sample_timestamps: list[float] = Field(default_factory=list)
    text: str | None = None


class RecordingResult(BaseModel):
    capture: RecordingCapture
    analysis: MediaAnalysis
    _frame_bytes: list[bytes] = PrivateAttr(default_factory=list)


def evenly_sampled(items: list, k: int) -> list:
    """At most ``k`` evenly-spaced items, always retaining the first and last."""
    if k <= 0 or len(items) <= k:
        return items
    if k == 1:
        return [items[0]]
    step = (len(items) - 1) / (k - 1)
    return [items[round(i * step)] for i in range(k)]


def video_sample_indices(duration: float, fps: int, frame_cap: int) -> list[int]:
    """Representative frame indices without allocating one entry per source frame."""
    scaled_frame_count = duration * fps
    if (
        not math.isfinite(duration)
        or duration <= 0
        or not math.isfinite(scaled_frame_count)
        or scaled_frame_count > 2**53
    ):
        raise ValueError("video duration is outside the safe sampling range")
    frame_count = max(1, math.ceil(scaled_frame_count))
    sample_count = min(frame_count, max(1, frame_cap))
    if sample_count == 1:
        return [0]
    span = frame_count - 1
    denominator = sample_count - 1
    return [
        (index * span + denominator // 2) // denominator
        for index in range(sample_count)
    ]
