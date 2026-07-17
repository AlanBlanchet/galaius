"""vision/core.py transcribe_audio on Windows (#73): a NamedTemporaryFile still OPEN cannot be
reopened by path there (PermissionError, O_TEMPORARY sharing) — the last red windows-latest job
blocking the 0.27.0 release. The writer handle must be CLOSED before the file is reopened for
litellm, and the file must not leak afterward."""

import tempfile

import pytest

from interact.vision import transcribe_audio


@pytest.mark.asyncio
async def test_temp_audio_is_closed_before_reopen_and_removed_after(monkeypatch, tmp_path):
    import interact.vision.core as vis

    writers: list = []
    real_ntf = tempfile.NamedTemporaryFile

    def tracking_ntf(*a, **k):
        tf = real_ntf(*a, **k, dir=tmp_path)
        writers.append(tf)
        return tf

    state: dict = {}

    async def fake_at(*, model, file):
        # Windows semantics: the reopen only works once the writer handle is closed.
        state["writer_closed_at_call"] = writers[0].closed
        state["content"] = file.read()
        return {"text": "hi"}

    monkeypatch.setattr(vis.tempfile, "NamedTemporaryFile", tracking_ntf)
    monkeypatch.setattr(vis.litellm, "validate_environment", lambda m: {"keys_in_environment": True})
    monkeypatch.setattr(vis.litellm, "atranscription", fake_at)

    r = await transcribe_audio(b"AUDIOBYTES", model="whisper-1", mime_type="audio/wav")

    assert r.text == "hi" and state["content"] == b"AUDIOBYTES"
    assert state["writer_closed_at_call"] is True  # reopen-by-path requires this on Windows
    assert list(tmp_path.iterdir()) == []  # the temp file never leaks (delete=False + unlink)
