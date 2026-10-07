"""The `transcribe` tool: speech-to-text + audio understanding over a local file.

Transcription goes through litellm's transcription endpoint (a different API from chat
completion — see vision.transcribe_audio); a `query` is answered acoustically when the model can
hear the clip (Gemini, gpt-4o-audio) and over the transcript otherwise (Whisper). All model calls
are mocked — unit tests never spend (conftest also blocks the transcription endpoints)."""

import base64
from pathlib import Path

import pytest

import galaius.server as srv
import galaius.vision.core as vis
from galaius.config import Config
from galaius.vision import MediaItem, VLMResult
from galaius.vision.core import _audio_content, transcribe_audio


class _FakeConfig:
    """Stand-in for the live config: deterministic per-role resolution, no file I/O."""

    def __init__(self, audio: str, image: str = "gemini/img-model"):
        self._audio, self._image = audio, image
        self.media_backend = "session"
        self.media_billing = "api_allowed"
        self.media_provider_order = ("claude",)
        self.media_session_no_extra_usage_confirmed_for = ("claude",)
        self.media_max_items = 16
        self.media_max_total_bytes = 50 * 1024 * 1024
        self.media_max_context_chars = 32 * 1024
        self.criteria_weights = ""

    def refresh(self):
        return self

    def criteria_for(self, role):
        return ""  # blank → the criterion-selection block is skipped, same as an unset field

    def resolve_model(self, role, override="", breaker=None):
        return override or {"audio": self._audio, "image": self._image}.get(role, self._image)

    def media_api_enabled(self):
        return False

    def media_sessions_enabled(self):
        return True

    def ranked_models(self, role, breaker=None):
        return []


def _audio_file(tmp_path, name="clip.mp3", data=b"ID3audio"):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


@pytest.mark.asyncio
async def test_session_only_audio_policy_precedes_file_read_and_model_resolution(
    monkeypatch, tmp_path
) -> None:
    fake_config = _FakeConfig(audio="whisper-1")
    fake_config.media_billing = "session_only"

    def forbidden(*args, **kwargs):
        raise AssertionError("session-only audio performed I/O or resolved an API model")

    fake_config.resolve_model = forbidden
    monkeypatch.setattr(srv.tools_vision, "config", fake_config)
    audio_path = tmp_path / "must-not-be-read.mp3"
    read_bytes = Path.read_bytes

    def guarded_read(path):
        if path == audio_path:
            forbidden()
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)

    result = await srv.transcribe(str(audio_path))

    assert "subscription sessions cannot transcribe audio" in result


def test_transcribe_public_copy_names_only_proven_session_media_provider() -> None:
    copy = " ".join((srv.transcribe.__doc__ or "").split())
    assert "Claude subscription sessions" in copy
    assert "Codex" not in copy


# ── transcribe_audio (the litellm transcription endpoint) ───────────────────────────────────
@pytest.mark.asyncio
async def test_transcribe_audio_calls_the_transcription_endpoint(monkeypatch, media_output_root):
    captured: dict = {}

    class _Resp:
        text = "the quick brown fox"

    async def fake_at(model, file, **kw):
        captured["model"] = model
        captured["read"] = file.read()  # the endpoint receives the audio bytes
        return _Resp()

    monkeypatch.setattr(vis.litellm, "validate_environment", lambda m: {"keys_in_environment": True})
    monkeypatch.setattr(vis.litellm, "atranscription", fake_at)
    r = await transcribe_audio(
        b"RIFF\x08\x00\x00\x00WAVE", model="whisper-1", mime_type="audio/wav",
        config=Config(debug_dir=media_output_root, media_backend="api", media_billing="api_allowed"),
    )
    assert r.text == "the quick brown fox" and r.model == "whisper-1"
    assert captured["model"] == "whisper-1" and captured["read"] == b"RIFF\x08\x00\x00\x00WAVE"


@pytest.mark.asyncio
async def test_transcribe_audio_missing_key_is_friendly_not_a_crash(monkeypatch):
    monkeypatch.setattr(vis.litellm, "validate_environment", lambda m: {"keys_in_environment": False})
    r = await transcribe_audio(b"x", model="whisper-1")
    assert "unavailable" in r.text.lower() and r.elapsed == 0


@pytest.mark.asyncio
async def test_audio_content_builds_an_input_audio_part_passthrough_wav():
    part = await _audio_content(
        MediaItem.from_bytes(b"RIFF\x08\x00\x00\x00WAVE", "audio", "audio/wav"), Config()
    )
    assert part["type"] == "input_audio" and part["input_audio"]["format"] == "wav"
    assert base64.b64decode(part["input_audio"]["data"]) == b"RIFF\x08\x00\x00\x00WAVE"


# ── the transcribe tool ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_transcribe_no_query_returns_the_transcript(monkeypatch, tmp_path):
    monkeypatch.setattr(srv.tools_vision, "config", _FakeConfig(audio="whisper-1"))
    captured: dict = {}

    async def fake_transcribe(data, *, model, mime_type="audio/mpeg", config=None):
        captured.update(
            model=model,
            mime=mime_type,
            backend=config.media_backend,
            billing=config.media_billing,
        )
        return VLMResult(text="hello world", elapsed=0.4, model=model)

    monkeypatch.setattr(srv.tools_vision, "transcribe_audio", fake_transcribe)
    out = await srv.transcribe(_audio_file(tmp_path))
    assert "hello world" in out
    assert captured["model"] == "whisper-1" and captured["mime"] == "audio/mpeg"
    assert captured["backend"] == "session" and captured["billing"] == "api_allowed"


@pytest.mark.asyncio
async def test_transcribe_query_with_audio_chat_model_hears_the_clip(monkeypatch, tmp_path):
    """A Gemini-class model takes the audio directly (acoustic understanding) — media_type='audio',
    no transcription round-trip."""
    fake_config = _FakeConfig(audio="gemini/gemini-2.5-flash")
    monkeypatch.setattr(srv.tools_vision, "config", fake_config)
    monkeypatch.setattr(srv.vlm, "config", fake_config)
    captured: dict = {}

    async def api(media, context, config, prompt, max_tokens, response_format, model, _dispatch_state):
        captured.update(
            media_type=media[0].media_type,
            query=prompt,
            mime=media[0].mime_type,
            backend=config.media_backend,
            billing=config.media_billing,
        )
        return VLMResult(text="two speakers, calm tone", elapsed=1.0, model="gemini")

    async def forbidden_session(*args, **kwargs):
        raise AssertionError("audio-chat query reached a visual session")

    monkeypatch.setattr(vis, "_api_media_completion", api)
    monkeypatch.setattr(vis, "subscription_media_completion", forbidden_session)
    out = await srv.transcribe(
        _audio_file(tmp_path, "v.webm", b"\x1aE\xdf\xa3audio"),
        query="how many speakers?",
    )
    assert "two speakers" in out
    assert captured["media_type"] == "audio" and captured["query"] == "how many speakers?"
    assert captured["mime"] == "audio/webm"
    assert captured["backend"] == "session" and captured["billing"] == "api_allowed"


@pytest.mark.asyncio
async def test_transcribe_query_with_transcription_only_model_answers_over_transcript(monkeypatch, tmp_path):
    """Whisper can't take audio in chat, so the query is answered over its transcript by the image
    model — and the transcript itself is still surfaced."""
    monkeypatch.setattr(srv.tools_vision, "config", _FakeConfig(audio="whisper-1", image="gemini/img"))
    captured: dict = {}

    async def fake_transcribe(data, *, model, mime_type="audio/mpeg", config=None):
        return VLMResult(text="quarterly revenue grew 12 percent", elapsed=0.3, model=model)

    async def session(
        media, context, config, prompt, response_format, explicit_model, _dispatch_state,
    ):
        captured.update(
            media=media,
            context=context,
            prompt=prompt,
            backend=config.media_backend,
            billing=config.media_billing,
        )
        return VLMResult(text="Revenue +12%.", elapsed=0.2, model="gemini/img")

    monkeypatch.setattr(srv.tools_vision, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(vis, "subscription_media_completion", session)
    out = await srv.transcribe(_audio_file(tmp_path), query="summarize the numbers")
    assert "Revenue +12%." in out and "quarterly revenue grew 12 percent" in out  # answer + transcript
    assert captured["media"] == [] and captured["prompt"] == "summarize the numbers"  # text-only over transcript
    assert "quarterly revenue" in captured["context"]
    assert captured["backend"] == "session" and captured["billing"] == "api_allowed"


@pytest.mark.asyncio
async def test_transcribe_missing_file_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(srv.tools_vision, "config", _FakeConfig(audio="whisper-1"))
    out = await srv.transcribe("/no/such/audio.mp3")
    assert out.startswith("ERROR") and "could not read" in out
