"""What a model can take in: the capability flags and the family checks for video, audio and transcription-only models."""

from enum import StrEnum
from typing import TYPE_CHECKING



if TYPE_CHECKING:
    pass


class ModelCapability(StrEnum):
    LLM = "llm"
    VLM = "vlm"
    GUI_GROUNDING = "gui_grounding"
    # Native computer-use: the model emits click coordinates directly (Anthropic/OpenAI
    # computer-use tool). Sourced from litellm's `supports_computer_use` — not hardcoded.
    COMPUTER_USE = "computer_use"
    VIDEO = "video"  # native video input — see is_native_video_model
    AUDIO = "audio"  # audio understanding / transcription — see is_audio_model


# Native video INPUT and audio understanding are per-FAMILY facts litellm's
# `supports_video_input`/`supports_audio_input` flags do NOT reliably populate (return nothing
# for every current model), so — like CoordFormat's grounding table — these are curated
# substring tables grounded in each provider's own docs (web-verified 2026-06-24):
#   • Native video input: Gemini (all 2.x/3.x, incl. YouTube URLs), Qwen-VL/Qwen3-VL, InternVL,
#     LLaVA-Video, Amazon Nova Lite/Pro/Premier. OpenAI + Anthropic are FRAMES-ONLY (no native
#     video media type) — galaius still drives them by ffmpeg-sampling a recording to images,
#     but they aren't "video models".
#   • Audio understanding/transcription: Gemini, GPT-4o-audio/-transcribe, Whisper, Qwen-Omni.
#     (Anthropic has no audio input.)
# Substring match against the litellm id (provider prefix included), case-insensitive.
_VIDEO_FAMILIES: tuple[str, ...] = (
    "gemini",
    "qwen2-vl", "qwen2.5-vl", "qwen3-vl", "qwen-vl", "qwenvl",
    "internvl",
    "llava-video", "llava-next-video",
    "nova-lite", "nova-pro", "nova-premier",  # Nova Micro is text-only — match the video tiers
)


_AUDIO_FAMILIES: tuple[str, ...] = (
    "gemini",
    "gpt-4o-audio", "gpt-4o-mini-audio", "gpt-audio",
    "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper",
    "qwen-omni", "qwen2.5-omni", "qwen3-omni", "qwen2-audio",
)


# Generation/TTS/embedding variants of a family (e.g. gemini-*-image-preview, *-tts, imagen,
# veo) OUTPUT media or vectors — they don't UNDERSTAND video/audio input, so excluded from that
# capability even though the name carries the family substring.
_NOT_UNDERSTANDING: tuple[str, ...] = (
    "-image", "image-preview", "image-generation", "-tts", "-embedding",
    "embedding", "imagen", "veo",
)


def _is_understanding(model_id: str) -> bool:
    lid = model_id.lower()
    return not any(x in lid for x in _NOT_UNDERSTANDING)


def is_native_video_model(model_id: str) -> bool:
    """Whether a model accepts NATIVE video input (a clip), per the curated family table."""
    lid = model_id.lower()
    return _is_understanding(lid) and any(fam in lid for fam in _VIDEO_FAMILIES)


def is_audio_model(model_id: str) -> bool:
    """Whether a model can understand or transcribe AUDIO, per the curated family table."""
    lid = model_id.lower()
    return _is_understanding(lid) and any(fam in lid for fam in _AUDIO_FAMILIES)


# Families litellm sends NATIVE inline video to (a Gemini `inline_data` part) — matched as a
# substring so a bare `gemini-2.5-pro`, `gemini/…` and `vertex_ai/gemini-…` all qualify. Other
# providers have no inline-video transform in litellm and SILENTLY DROP a video content part
# (HTTP 200 + a hallucinated answer, never an error) — so this MUST be a positive allowlist, not
# exception catching. Qwen/DashScope is URL-only through litellm, stays on frame sampling (#48).
_NATIVE_VIDEO_INLINE_FAMILIES: tuple[str, ...] = ("gemini", "vertex_ai")


def supports_native_video_inline(model_id: str) -> bool:
    """Whether litellm will send this model native inline video rather than us ffmpeg-sampling
    frames — a Gemini/Vertex understanding model only, today (#48)."""
    lid = model_id.lower()
    return is_native_video_model(lid) and any(fam in lid for fam in _NATIVE_VIDEO_INLINE_FAMILIES)


# Pure speech-to-text models — transcribe but can't take audio in a chat completion. The
# transcribe tool answers a `query` about one of these over its TRANSCRIPT (via the image
# model), not by routing audio into an acoustic chat call it can't serve.
_TRANSCRIBE_ONLY: tuple[str, ...] = ("whisper", "-transcribe")


def is_transcription_only_model(model_id: str) -> bool:
    lid = model_id.lower()
    return any(t in lid for t in _TRANSCRIBE_ONLY)
