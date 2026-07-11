"""The `transcribe` tool: speech-to-text + audio understanding over a local file.

Transcription goes through litellm's transcription endpoint (a different API from chat
completion — see vision.transcribe_audio); a `query` is answered acoustically when the model can
hear the clip (Gemini, gpt-4o-audio) and over the transcript otherwise (Whisper). All model calls
are mocked — unit tests never spend (conftest also blocks the transcription endpoints)."""

import base64

import pytest

import interact.server as srv
from interact.vision import VLMResult, _audio_content, MediaItem, transcribe_audio


class _FakeConfig:
    """Stand-in for the live config: deterministic per-role resolution, no file I/O."""

    def __init__(self, audio: str, image: str = "gemini/img-model"):
        self._audio, self._image = audio, image

    def refresh(self):
        return self

    def resolve_model(self, role, override="", breaker=None):
        return override or {"audio": self._audio, "image": self._image}.get(role, self._image)


def _audio_file(tmp_path, name="clip.mp3", data=b"ID3audio"):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


# ── transcribe_audio (the litellm transcription endpoint) ───────────────────────────────────
@pytest.mark.asyncio
async def test_transcribe_audio_calls_the_transcription_endpoint(monkeypatch):
    import interact.vision.core as vis

    captured: dict = {}

    class _Resp:
        text = "the quick brown fox"

    async def fake_at(model, file, **kw):
        captured["model"] = model
        captured["read"] = file.read()  # the endpoint receives the audio bytes
        return _Resp()

    monkeypatch.setattr(vis.litellm, "validate_environment", lambda m: {"keys_in_environment": True})
    monkeypatch.setattr(vis.litellm, "atranscription", fake_at)
    r = await transcribe_audio(b"AUDIOBYTES", model="whisper-1", mime_type="audio/wav")
    assert r.text == "the quick brown fox" and r.model == "whisper-1"
    assert captured["model"] == "whisper-1" and captured["read"] == b"AUDIOBYTES"


@pytest.mark.asyncio
async def test_transcribe_audio_missing_key_is_friendly_not_a_crash(monkeypatch):
    import interact.vision.core as vis

    monkeypatch.setattr(vis.litellm, "validate_environment", lambda m: {"keys_in_environment": False})
    r = await transcribe_audio(b"x", model="whisper-1")
    assert "unavailable" in r.text.lower() and r.elapsed == 0


def test_audio_content_builds_an_input_audio_part_passthrough_wav():
    part = _audio_content(MediaItem.from_bytes(b"RIFFWAVEDATA", "audio", "audio/wav"))
    assert part["type"] == "input_audio" and part["input_audio"]["format"] == "wav"
    assert base64.b64decode(part["input_audio"]["data"]) == b"RIFFWAVEDATA"


# ── the transcribe tool ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_transcribe_no_query_returns_the_transcript(monkeypatch, tmp_path):
    monkeypatch.setattr(srv.tools_vision, "config", _FakeConfig(audio="whisper-1"))
    captured: dict = {}

    async def fake_transcribe(data, *, model, mime_type="audio/mpeg"):
        captured.update(model=model, mime=mime_type)
        return VLMResult(text="hello world", elapsed=0.4, model=model)

    monkeypatch.setattr(srv.tools_vision, "transcribe_audio", fake_transcribe)
    out = await srv.transcribe(_audio_file(tmp_path))
    assert "hello world" in out
    assert captured["model"] == "whisper-1" and captured["mime"] == "audio/mpeg"


@pytest.mark.asyncio
async def test_transcribe_query_with_audio_chat_model_hears_the_clip(monkeypatch, tmp_path):
    """A Gemini-class model takes the audio directly (acoustic understanding) — media_type='audio',
    no transcription round-trip."""
    monkeypatch.setattr(srv.tools_vision, "config", _FakeConfig(audio="gemini/gemini-2.5-flash"))
    captured: dict = {}

    async def fake_vlm(data, context, query=None, media_type="image", mime="image/png", **kw):
        captured.update(media_type=media_type, query=query, mime=mime)
        return VLMResult(text="two speakers, calm tone", elapsed=1.0, model="gemini")

    monkeypatch.setattr(srv.vlm, "_vlm", fake_vlm)
    out = await srv.transcribe(_audio_file(tmp_path, "v.webm", b"WEBM"), query="how many speakers?")
    assert "two speakers" in out
    assert captured["media_type"] == "audio" and captured["query"] == "how many speakers?"
    assert captured["mime"] == "audio/webm"


@pytest.mark.asyncio
async def test_transcribe_query_with_transcription_only_model_answers_over_transcript(monkeypatch, tmp_path):
    """Whisper can't take audio in chat, so the query is answered over its transcript by the image
    model — and the transcript itself is still surfaced."""
    monkeypatch.setattr(srv.tools_vision, "config", _FakeConfig(audio="whisper-1", image="gemini/img"))
    captured: dict = {}

    async def fake_transcribe(data, *, model, mime_type="audio/mpeg"):
        return VLMResult(text="quarterly revenue grew 12 percent", elapsed=0.3, model=model)

    async def fake_analyze(media, context, config, prompt=None, **kw):
        captured.update(media=media, context=context, prompt=prompt)
        return VLMResult(text="Revenue +12%.", elapsed=0.2, model="gemini/img")

    monkeypatch.setattr(srv.tools_vision, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(srv.tools_vision, "analyze_media", fake_analyze)
    out = await srv.transcribe(_audio_file(tmp_path), query="summarize the numbers")
    assert "Revenue +12%." in out and "quarterly revenue grew 12 percent" in out  # answer + transcript
    assert captured["media"] == [] and captured["prompt"] == "summarize the numbers"  # text-only over transcript
    assert "quarterly revenue" in captured["context"]


@pytest.mark.asyncio
async def test_transcribe_missing_file_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(srv.tools_vision, "config", _FakeConfig(audio="whisper-1"))
    out = await srv.transcribe("/no/such/audio.mp3")
    assert out.startswith("ERROR") and "could not read" in out
