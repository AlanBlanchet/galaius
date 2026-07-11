"""Video and audio are real model CAPABILITIES, not "every vision model".

The bug this guards: the `video` role used to require only `vlm`, so its dropdown mirrored the
image dropdown and offered models (Claude, GPT) that don't take video at all. litellm's
`supports_video_input`/`_audio_input` flags don't populate, so capability comes from a curated
family table (interact.models) grounded in each provider's docs. These tests pin that the video
picker is a genuine subset, the matchers are right, and the new MLVU/MMAU benchmarks exist."""

import pytest

from interact.models import (
    Benchmark,
    Model,
    ModelCapability,
    is_audio_model,
    is_native_video_model,
    is_transcription_only_model,
    supports_native_video_inline,
)
from interact.config import by_key


@pytest.mark.parametrize(
    "model_id, native_inline",
    [
        # Gemini / Vertex family — litellm sends inline_data video natively
        ("gemini/gemini-3.5-flash", True),
        ("gemini-2.5-pro", True),
        ("vertex_ai/gemini-2.5-flash", True),
        # Native-video models on OTHER providers: litellm has no inline-video transform → would
        # silently DROP the part, so we must keep them on frame sampling (#48).
        ("nebius/Qwen/Qwen2.5-VL-72B-Instruct", False),
        ("ollama/qwen3-vl", False),
        # Not video-capable at all
        ("claude-opus-4-7", False),
        ("gpt-5.5", False),
        # Gemini non-understanding variants never qualify
        ("gemini/gemini-3-pro-image-preview", False),
        ("gemini/imagen-3", False),
    ],
)
def test_supports_native_video_inline_is_a_gemini_allowlist(model_id, native_inline):
    assert supports_native_video_inline(model_id) is native_inline


@pytest.mark.parametrize(
    "model_id, video, audio",
    [
        # Native-video families
        ("gemini/gemini-3.5-flash", True, True),
        ("gemini-2.5-pro", True, True),
        ("nebius/Qwen/Qwen2.5-VL-72B-Instruct", True, False),
        ("ollama/qwen3-vl", True, False),
        ("OpenGVLab/InternVL3-8B", True, False),
        ("amazon-nova/nova-lite-v1", True, False),
        ("amazon-nova/nova-pro-v1", True, False),
        # Frames-only / images-only — NOT video, NOT audio
        ("claude-opus-4-7", False, False),
        ("gpt-5.5", False, False),
        ("chatgpt/gpt-5.4", False, False),
        ("xai/grok-4.3", False, False),
        ("amazon-nova/nova-micro-v1", False, False),  # Nova Micro is text-only
        # Audio-only (transcription / audio-chat) — NOT video
        ("whisper-1", False, True),
        ("openai/gpt-4o-transcribe", False, True),
        ("gpt-4o-audio-preview", False, True),
        ("Qwen/Qwen2.5-Omni-7B", False, True),
        # Generation / TTS / embedding variants of a family don't UNDERSTAND input media
        ("gemini/gemini-3-pro-image-preview", False, False),
        ("gemini/gemini-2.5-pro-preview-tts", False, False),
        ("gemini/gemini-embedding-001", False, False),
        ("gemini/imagen-3", False, False),
        ("gemini/veo-2", False, False),
    ],
)
def test_modality_matchers(model_id, video, audio):
    assert is_native_video_model(model_id) is video
    assert is_audio_model(model_id) is audio


@pytest.mark.parametrize(
    "model_id, only",
    [("whisper-1", True), ("openai/gpt-4o-transcribe", True),
     ("gemini/gemini-2.5-flash", False), ("gpt-4o-audio-preview", False)],
)
def test_transcription_only(model_id, only):
    assert is_transcription_only_model(model_id) is only


def test_video_picker_lists_only_native_video_models_not_the_image_list():
    """The regression: video must NOT mirror image. The video dropdown is genuinely video-capable
    models (Gemini/Qwen-VL/Nova), and a frames-only model (Claude/GPT) never appears in it."""
    video_ids = {o.value for o in by_key("video.model").model_options() if o.value}
    image_ids = {o.value for o in by_key("image.model").model_options() if o.value}
    audio_ids = {o.value for o in by_key("audio.model").model_options() if o.value}

    assert video_ids, "video picker is empty — the family table didn't tag any model"
    assert audio_ids, "audio picker is empty"
    # Every video option is a real native-video model; no Claude/GPT leaks in.
    assert all(is_native_video_model(m) for m in video_ids)
    assert not any("claude" in m.lower() or m.lower().startswith("gpt-") for m in video_ids)
    # The whole point: video is a DISTINCT set, not a copy of image.
    assert video_ids != image_ids


def test_video_role_still_resolves_a_model_without_a_native_video_key(monkeypatch):
    """No regression: even with no native-video provider keyed, the video chain falls back (interact
    frame-samples a recording, so any VLM works) — chain_for appends the cheapest available VLM."""
    from interact.config import Config

    # Only an OpenAI key present → no Gemini/Qwen native-video model is available.
    for var in list(__import__("os").environ):
        if var.endswith(("_API_KEY", "_API_BASE")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    Model.load_registry()
    chain = Config().chain_for("video")
    assert chain.preferences, "video chain is empty — a user with no Gemini key can't analyze video"


@pytest.mark.parametrize(
    "bid, category, has_scores",
    [("video_mme", "video", True), ("mvbench", "video", False),
     ("mlvu", "video", False), ("mmau", "audio", True)],
)
def test_benchmarks_registered_with_categories(bid, category, has_scores):
    b = Benchmark.by_id(bid)
    assert b is not None and b.category == category
    if has_scores:
        assert b.published is not None and b.published.entries


def test_audio_is_its_own_benchmark_category():
    cats = {b.category for b in Benchmark.registry()}
    assert {"image", "gui_grounding", "video", "audio"} <= cats
