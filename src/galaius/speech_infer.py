"""Speech to text (Whisper through faster-whisper / CTranslate2), run as a long-lived subprocess inside
the managed `~/.galaius/models/env` venv, on the same line protocol as `vision_infer.py` (its module
docstring): each stdin line is `{"request": <path of a JSON request file>}`, the answer is phase
lines then one `{"result": ...}` or `{"error": ...}` line. The model it last loaded stays loaded (for
the machine's `model_keep_warm_seconds`, then the worker exits), so the next utterance is transcribed
at once (Whisper large-v3-turbo on an RTX 2070: ~0.5 s for a 2-8 s command, language detection
included). The server warms it up when the owner allows voice on this machine: a request on one
second of silence, which installs, fetches and loads whatever is missing.

Request: `{"model": <MACHINE_MODELS id>, "audio_path": <local file>, "cache_root": <dir>,
"languages": ["fr", "en"], "hotwords": "Galaius, ..."}`. Result: `{"text": str, "language": str, "seconds": float}`.
`hotwords`: the names the owner may say (the server's vocabulary, comma-separated). The audio is first
transcribed with no hint; only where a word comes out a few letters off one of those names (`misheard`)
is it transcribed again told the names (faster-whisper `hotwords`: told the one name alone, Whisper base and small
wrote « Galius », « Galios » where the whole list got « Galaius »), and only that place is taken from the second
transcript, when it does write the name there (`corrected`); a name written with other accents, or split, is
spelled as given (`spelled`). A hint on every request turned
« Goliath », « Galilée », « galets » into the name and made his other words worse; a real word Whisper
writes without hint stays as he said it. Empty: no hint ever.

No dependency on `galaius` or `galaius_core`: this script runs in that venv only.
"""

import ctypes
import difflib
import json
import re
import sys
import traceback
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path

Emit = Callable[[str, str], None]


def spoken_language(probabilities: Sequence[tuple[str, float]], languages: Sequence[str]) -> str:
    """The likeliest of `languages` (the ones he speaks) under Whisper's language probabilities; the
    first of them when none was scored. A third language never wins: a French command half-heard as
    Italian is still French."""
    scored = dict(probabilities)
    return max(languages, key=lambda language: (scored.get(language, 0.0), -languages.index(language)))


#: A word (or 2-3 run together) this close to a name is the name misheard: up to 3 letters in 7 differ. Measured on
#: synthetic EN/FR clips: Whisper's spellings of « Galaius » (Galeus, Gallius, Galios, Galeos) are within it, the real
#: words near it (Goliath, Galilée, Galileo, galleon) are not, or keep their spelling once told the name.
NEAR_EDITS = 3 / 7
#: Names shorter than this are only ever taken as written: too many real words lie a letter away from « Blob ».
NEAR_MIN_LETTERS = 6


def _folded(text: str) -> str:
    """Lowercase letters and digits only, accents dropped: « Galéaüs, » -> « galeaus »."""
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode())


def _edits(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        previous, row[0] = row[:], i
        for j in range(1, len(b) + 1):
            row[j] = min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (a[i - 1] != b[j - 1]))
    return row[-1]


def misheard(words: Sequence[str], names: Sequence[str]) -> list[tuple[int, int, str]]:
    """`(start, end, name)` for each place in `words` that reads as `name` misheard: 1 to 3 words whose letters are
    within `NEAR_EDITS` of it, where it is not already written. Overlapping readings keep the closest."""
    found = []
    for name in names:
        target = _folded(name)
        if len(target) < NEAR_MIN_LETTERS:
            continue
        near = sorted((_edits(_folded("".join(words[start:start + n])), target), n, start)
                      for start in range(len(words)) for n in (1, 2, 3) if start + n <= len(words))
        taken: list[tuple[int, int]] = []
        for distance, n, start in near:
            if distance > int(NEAR_EDITS * len(target)):
                break
            if all(start + n <= s or start >= e for s, e in taken):
                taken.append((start, start + n))
                if distance:
                    found.append((start, start + n, name))
    return sorted(found)


def corrected(plain: str, hinted: str, places: Sequence[tuple[int, int, str]]) -> str:
    """`plain` with each of `places` replaced by the fewest `hinted` words that write its name there; every other
    word stays as first heard."""
    plain_words, hinted_words = plain.split(), hinted.split()
    matcher = difflib.SequenceMatcher(a=[_folded(w) for w in plain_words], b=[_folded(w) for w in hinted_words], autojunk=False)
    words: list[str] = []
    for operation, a0, a1, b0, b1 in matcher.get_opcodes():
        place = next(((max(a0, start), min(a1, end), _folded(name)) for start, end, name in places
                      if operation != "equal" and start < a1 and end > a0), None)
        written = place and min(((j - i, i, j) for i in range(b0, b1) for j in range(i + 1, b1 + 1)
                                 if place[2] in _folded("".join(hinted_words[i:j]))), default=None)
        if place and written:
            words += plain_words[a0:place[0]] + hinted_words[written[1]:written[2]] + plain_words[place[1]:a1]
        else:
            words += plain_words[a0:a1]
    return " ".join(words)


def spelled(text: str, names: Sequence[str]) -> str:
    """`text` with each one-word name of `names` written as given where it reads the same but for accents or hyphens
    (« Galaïus, » -> « Galaius, »), or, for a name of letters only, comes out split (« Galai us » -> « Galaius »); case
    alone is kept (« bureau » stays a word, not his PC « Bureau »)."""
    written = {_folded(name): name for name in names if " " not in name}
    joined = {folded: name for folded, name in written.items() if re.fullmatch(r"[^\W\d_]+", name)}
    tokens = [re.fullmatch(r"(\W*)(.*?)(\W*)", token).groups() for token in text.split()]
    words, at = [], 0
    while at < len(tokens):
        for n in (3, 2, 1):
            group = tokens[at:at + n]
            if len(group) < n or any(tail for _head, _core, tail in group[:-1]) or any(head for head, _core, _tail in group[1:]):
                continue
            core = "".join(core for _head, core, _tail in group)
            name = (joined if n > 1 else written).get(_folded(core))
            if name and (n > 1 or core.casefold() != name.casefold()):
                words.append(group[0][0] + name + group[-1][2])
                at += n
                break
        else:
            words.append("".join(tokens[at]))
            at += 1
    return " ".join(words)


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

    def transcript(hotwords: str | None) -> str:
        segments, _info = model.transcribe(audio, language=language, beam_size=1, condition_on_previous_text=False, hotwords=hotwords)
        return " ".join(segment.text.strip() for segment in segments).strip()

    names = [name.strip() for name in str(request.get("hotwords") or "").split(",") if name.strip()]
    text = transcript(None)
    if places := misheard(text.split(), names):
        text = corrected(text, transcript(", ".join(names)), places)
    return {"text": spelled(text, names), "language": language, "seconds": round(len(audio) / 16_000, 3)}


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
