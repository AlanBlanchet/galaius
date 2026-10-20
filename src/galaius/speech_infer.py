"""Speech to text (Whisper through faster-whisper / CTranslate2), run as a long-lived subprocess inside
the managed `~/.galaius/models/env` venv, on the same line protocol as `vision_infer.py` (its module
docstring): each stdin line names one JSON request file, the answer is phase lines then one
`{"result": ...}` or `{"error": ...}` line. The model it last loaded stays loaded, so the next
utterance is transcribed at once (Whisper large-v3-turbo on an RTX 2070: ~0.5 s for a 2-8 s command,
language detection included).

Request: `{"model": <MACHINE_MODELS id>, "audio_path": <local file>, "cache_root": <dir>,
"languages": ["fr", "en"]}`. Result: `{"text": str, "language": str, "seconds": float}`.

No dependency on `galaius` or `galaius_core`: this script runs in that venv only.
"""

import ctypes
import json
import sys
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path

Emit = Callable[[str, str], None]


def spoken_language(probabilities: Sequence[tuple[str, float]], languages: Sequence[str]) -> str:
    """The likeliest of `languages` (the ones he speaks) under Whisper's language probabilities; the
    first of them when none was scored. A third language never wins: a French command half-heard as
    Italian is still French."""
    scored = dict(probabilities)
    return max(languages, key=lambda language: (scored.get(language, 0.0), -languages.index(language)))


def _load_cuda_libraries() -> None:
    """CTranslate2 opens cuBLAS 12 and cuDNN 9 by name; in this venv they are the `nvidia-*` wheels
    torch installed, which no loader path names. Loaded here once, globally, before it imports."""
    import nvidia  # noqa: PLC0415 - the wheels' namespace package, present only with a CUDA torch

    for folder in ("cublas", "cudnn"):
        for library in sorted((Path(next(iter(nvidia.__path__))) / folder / "lib").glob("lib*.so*")):
            ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)


def infer(request: dict, loaded: dict, emit: Emit) -> dict:
    from faster_whisper import WhisperModel, decode_audio  # noqa: PLC0415 - after the CUDA libraries above

    model_id = str(request["model"])
    if loaded.get("id") != model_id:
        emit("loading_model", f"loading {model_id}")
        import ctranslate2  # noqa: PLC0415

        gpu = ctranslate2.get_cuda_device_count() > 0
        loaded.clear()
        loaded.update(id=model_id, model=WhisperModel(model_id, device="cuda" if gpu else "cpu", compute_type="int8_float16" if gpu else "int8",
                                                      download_root=str(Path(request["cache_root"]) / "speech")))
    model = loaded["model"]
    emit("running_model", "transcribing")
    audio = decode_audio(str(request["audio_path"]), sampling_rate=16_000)
    languages = tuple(request.get("languages") or ("fr", "en"))
    _language, _probability, probabilities = model.detect_language(audio)
    language = spoken_language(probabilities, languages)
    segments, _info = model.transcribe(audio, language=language, beam_size=1, condition_on_previous_text=False)
    return {"text": " ".join(segment.text.strip() for segment in segments).strip(), "language": language, "seconds": round(len(audio) / 16_000, 3)}


def _say(message: dict) -> None:
    print(json.dumps(message, separators=(",", ":")), flush=True)


def serve() -> None:
    """Answers requests from stdin until it closes."""
    try:
        _load_cuda_libraries()
    except (ImportError, OSError):
        pass  # no CUDA wheels: CTranslate2 runs on the CPU
    loaded: dict = {}
    emit = lambda phase, detail: _say({"phase": phase, "detail": detail})
    for line in sys.stdin:
        try:
            request = json.loads(Path(json.loads(line)["request"]).read_text(encoding="utf-8"))
            _say({"result": infer(request, loaded, emit)})
        except Exception:
            _say({"error": traceback.format_exc()[-2000:]})


if __name__ == "__main__":
    serve()
