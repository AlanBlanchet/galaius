"""Speech to text (Whisper through faster-whisper / CTranslate2), run as a long-lived subprocess inside
the managed `~/.galaius/models/env` venv, on the same line protocol as `vision_infer.py` (its module
docstring): each stdin line is `{"request": <path of a JSON request file>}`, the answer is phase
lines then one `{"result": ...}` or `{"error": ...}` line. The model it last loaded stays loaded (for
the machine's `model_keep_warm_seconds`, then the worker exits), so the next utterance is transcribed
at once (Whisper large-v3-turbo on an RTX 2070: ~0.5 s for a 2-8 s command, language detection
included). The server warms it up when the owner allows voice on this machine: a request on one
second of silence, which installs, fetches and loads whatever is missing.

Request: `{"model": <MACHINE_MODELS id>, "audio_path": <local file>, "cache_root": <dir>,
"languages": ["fr", "en"], "hotwords": "Galaius, ...", "hint": "on_mishear"}`. Result: `{"text": str, "language": str, "seconds": float}`.
`hotwords`: the names the owner may say (the server's vocabulary, comma-separated); `hint` (`Hint`, default
`on_mishear`): when Whisper is told them. `on_mishear`: the audio is transcribed with no hint; where 1-3 words come
out a few letters off a name (`misheard`), the transcript with the name in their place is scored against the audio
by the same unhinted model, and kept only when the model finds it nearly as likely (`heard_as`): « Galeus » becomes
« Galaius », « Gaius », « library », « galas » stay. No second decode, so no other word changes. A name written with
other accents, or split, is spelled as given (`spelled`). A hint on every pass (`always`) turned « Goliath »,
« Galilée », « Top » into names and made his other words worse; a second decode told the names spliced in hinted
words that only contained one (« she-advocates-for-change », « galaius-core, » for « Galice »).

No dependency on `galaius` or `galaius_core`: this script runs in that venv only.
"""

import ctypes
import json
import math
import sys
import traceback
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, NamedTuple, get_args

Emit = Callable[[str, str], None]
#: When Whisper is told the names: `always` (a prompt on every pass), `on_mishear` (only where it wrote one a few letters
#: off, and kept only where the audio agrees: `heard_as`), `never`.
Hint = Literal["always", "on_mishear", "never"]


def spoken_language(probabilities: Sequence[tuple[str, float]], languages: Sequence[str]) -> str:
    """The likeliest of `languages` (the ones he speaks) under Whisper's language probabilities; the
    first of them when none was scored. A third language never wins: a French command half-heard as
    Italian is still French."""
    scored = dict(probabilities)
    return max(languages, key=lambda language: (scored.get(language, 0.0), -languages.index(language)))


#: A word (or 2-3 run together) this close to a name may be the name misheard: up to 3 letters in 7 differ (Whisper's
#: « Galeus », « Gallius », « Galios » for « Galaius »). Only a candidate: the audio decides (`heard_as`).
NEAR_EDITS = 3 / 7
#: Names shorter than this are only ever taken as written: too many real words lie a letter away from « Blob ».
NEAR_MIN_LETTERS = 6
#: How much less likely, per token of the name, a model may find the transcript with the name in it than the one it wrote
#: (natural log, both scored by that model against the audio, unhinted) for the name to be written: per model, each
#: scoring on its own scale. Measured on synthetic EN/FR voices (84 clips, audio-critic r2's set, its 35-name list): a
#: misheard « Galaius » scored at worst -4.27 on turbo, -3.68 kept on small (-4.72 lost); real words near a name at best
#: -5.02 (« optimiser ») and -5.04 (« Gaius ») on turbo, -3.93 (« Galas ») on small.
NAME_TOKEN_LOGPROB = {"mobiuslabsgmbh/faster-whisper-large-v3-turbo": -4.5, "small": -3.8}
#: The bar of a model not measured above: the strictest one.
UNMEASURED_LOGPROB = max(NAME_TOKEN_LOGPROB.values())
#: A name Whisper spells in at most this many tokens is a word it knows (« librarian », « teacher », « optimizer »): it
#: writes it when it hears it, and what comes out near it is another real word (« teaches », « optimiser »).
KNOWN_WORD_TOKENS = 2


def fold(text: str) -> str:
    """Letters and digits of any script, lowercase, accents dropped: « Galéaüs, » -> « galeaus », « Ωmega-2 » -> « ωmega2 »."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if unicodedata.category(c)[0] in "LN")


def _edits(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        previous, row[0] = row[:], i
        for j in range(1, len(b) + 1):
            row[j] = min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (a[i - 1] != b[j - 1]))
    return row[-1]


class Place(NamedTuple):
    """Words `start` to `end` (exclusive) of a transcript that may be `name` misheard."""

    start: int
    end: int
    name: str


def misheard(words: Sequence[str], names: Sequence[str], known: Callable[[str], bool] = lambda _name: False) -> list[Place]:
    """Each place in `words` that may be one of `names` misheard: 1 to 3 words whose letters are within `NEAR_EDITS`
    of it, none of them already part of a name written exactly; a `known` name (a word the model spells itself) only
    ever as written. Overlapping readings of one name keep the closest."""
    spans = [(start, start + n) for start in range(len(words)) for n in (1, 2, 3) if start + n <= len(words)]
    folded = {span: fold("".join(words[span[0]:span[1]])) for span in spans}
    written = {fold(name) for name in names}
    taken = [span for span in spans if folded[span] in written]
    found = []
    for name in names:
        target = fold(name)
        if len(target) < NEAR_MIN_LETTERS or known(name):
            continue
        near = sorted((_edits(folded[span], target), span[1] - span[0], span) for span in spans)
        claimed = list(taken)
        for distance, _n, (start, end) in near:
            if distance > int(NEAR_EDITS * len(target)):
                break
            if all(end <= s or start >= e for s, e in claimed):
                claimed.append((start, end))
                found.append(Place(start, end, name))
    return sorted(found)


def _parts(token: str) -> tuple[str, str, str]:
    """Punctuation before, the word, punctuation after: « (Galeus,) » -> « ( », « Galeus », « ,) »."""
    kept = [at for at, char in enumerate(token) if unicodedata.category(char)[0] in "LNM"]
    return (token[:kept[0]], token[kept[0]:kept[-1] + 1], token[kept[-1] + 1:]) if kept else (token, "", "")


def _named(words: Sequence[str], place: Place) -> list[str]:
    """The words of `place` written as its name, the punctuation around them kept: « Galeus, » -> « Galaius, »."""
    return f"{_parts(words[place.start])[0]}{place.name}{_parts(words[place.end - 1])[2]}".split()


def substituted(words: Sequence[str], place: Place) -> str:
    """`words` with `place` written as its name: « Ask Galeus, now » -> « Ask Galaius, now »."""
    return " ".join([*words[:place.start], *_named(words, place), *words[place.end:]])


def heard_as(words: Sequence[str], places: Sequence[Place], scores: Sequence[float], bar: float) -> str:
    """`words` with each place whose score (log-likelihood gained by the name, per name token) reaches `bar`
    (`NAME_TOKEN_LOGPROB`) written as its name; of overlapping places the best scored wins."""
    kept: list[Place] = []
    for score, place in sorted(zip(scores, places), key=lambda pair: -pair[0]):
        if score >= bar and all(place.end <= other.start or place.start >= other.end for other in kept):
            kept.append(place)
    out = list(words)
    for place in sorted(kept, reverse=True):
        out[place.start:place.end] = _named(out, place)
    return " ".join(out)


def spelled(text: str, names: Sequence[str]) -> str:
    """`text` with each one-word name of `names` written as given where it reads the same but for accents or hyphens
    (« Galaïus, » -> « Galaius, »), or, for a name of letters only, comes out split (« Galai us » -> « Galaius »); case
    alone is kept (« bureau » stays a word, not his PC « Bureau »)."""
    written = {fold(name): name for name in names if " " not in name}
    joined = {folded: name for folded, name in written.items() if name.isalpha()}
    tokens = [_parts(token) for token in text.split()]
    words, at = [], 0
    while at < len(tokens):
        for n in (3, 2, 1):
            group = tokens[at:at + n]
            if len(group) < n or any(tail for _head, _core, tail in group[:-1]) or any(head for head, _core, _tail in group[1:]):
                continue
            core = "".join(core for _head, core, _tail in group)
            name = (joined if n > 1 else written).get(fold(core))
            if core and name and (n > 1 or core.casefold() != name.casefold()):
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

    names = [name.strip() for name in str(request.get("hotwords") or "").split(",") if name.strip()]
    hint: Hint = request.get("hint", "on_mishear")
    if hint not in get_args(Hint):
        raise ValueError(f"hint {hint!r} is none of {get_args(Hint)}")
    segments, _info = model.transcribe(audio, language=language, beam_size=1, condition_on_previous_text=False,
                                       hotwords=", ".join(names) if hint == "always" and names else None)
    # Whisper hears 30 s at a time: each window's words are scored against that window's audio only.
    windows: dict[int, list[str]] = {}
    for segment in segments:
        windows.setdefault(segment.seek, []).extend(segment.text.split())
    if hint == "on_mishear" and names:
        from faster_whisper.tokenizer import Tokenizer  # noqa: PLC0415 - in the speech venv only

        tokenizer = Tokenizer(model.hf_tokenizer, True, task="transcribe", language=language)
        bar = NAME_TOKEN_LOGPROB.get(model_id, UNMEASURED_LOGPROB)
        features = None
        for seek, words in windows.items():
            if places := misheard(words, names, known=lambda name: len(tokenizer.encode(" " + name)) <= KNOWN_WORD_TOKENS):
                features = model.feature_extractor(audio) if features is None else features
                window = features[:, seek:seek + model.feature_extractor.nb_max_frames]
                windows[seek] = heard_as(words, places, _name_scores(model, tokenizer, window, words, places), bar).split()
    text = " ".join(word for words in windows.values() for word in words)
    return {"text": spelled(text, names) if hint != "never" else text, "language": language, "seconds": round(len(audio) / 16_000, 3)}


def _name_scores(model, tokenizer, window, words: Sequence[str], places: Sequence[Place]) -> list[float]:
    """Per place: how much likelier (log, per token of the name) the unhinted model finds the transcript with the name
    than the one it wrote, both scored against `window` (the log-Mel frames, at most 30 s, Whisper wrote `words` from;
    CTranslate2's `align`: teacher-forced token probabilities)."""
    from faster_whisper.audio import pad_or_trim  # noqa: PLC0415 - in the speech venv only

    encoded = model.encode(pad_or_trim(window))

    def likelihood(text: str) -> float:
        ids = tokenizer.encode(" " + text)
        aligned = model.model.align(encoded, tokenizer.sot_sequence, [ids], window.shape[-1], median_filter_width=7)[0]
        return sum(math.log(max(p, 1e-12)) for p in aligned.text_token_probs)

    plain = likelihood(" ".join(words))
    return [(likelihood(substituted(words, place)) - plain) / len(tokenizer.encode(" " + place.name)) for place in places]


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
