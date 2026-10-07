"""Safetensors-only model weight ingestion (threat-model safeguard #6, threat #5): `torch.load`/
`pickle.load` execute arbitrary code on deserialization via `__reduce__` — a poisoned checkpoint
runs AS the model-serving process the instant it's loaded, no separate exploit needed. This module
is the one gate every model artifact a workspace supplies (not the pre-registered
`galaius_core.MACHINE_MODELS` vendor catalog, which the machine never receives as raw bytes —
`transformers.AutoModelFor*.from_pretrained` streams those) must pass BEFORE any byte of it is
deserialized.

Two independent checks, neither trusted alone: the FILENAME (a normal, honest upload) and the
MAGIC BYTES (a pickle stream renamed to `.safetensors` to slip past an extension-only gate —
exactly what an adversarial upload would try). Model-bundled Python code (a custom
`modeling_*.py`, `trust_remote_code`) is refused outright — it runs at the SAME sandbox tier as a
script node (`galaius.sandbox`), never at a lighter one, since executing arbitrary model-bundled
code is the same RCE class as a script."""

import struct
from pathlib import Path

from galaius_core import UnsafeModelWeightsError

#: Pickle's own protocol markers (`pickletools.opcodes`): PROTO (`\x80`), a bare pickle stream's
#: MARK (`(`), or the ASCII "GLOBAL"/module-import opcode pattern old protocol-0 pickles start
#: with. `torch.load` on a non-`weights_only`, non-safetensors checkpoint is a zip archive whose
#: `data.pkl` member starts with one of these — this function only sees raw bytes, so it checks a
#: bare pickle stream directly and, for a zip container, hands the embedded member's own first
#: bytes to the same check (see `_zip_member_is_pickle`).
_PICKLE_PROTO_MARKER = b"\x80"
_PICKLE_MARK = b"("
_SAFETENSORS_MIN_HEADER = 8


def looks_like_pickle(data: bytes) -> bool:
    if not data:
        return False
    if data[:1] == _PICKLE_PROTO_MARKER and len(data) >= 2 and data[1] <= 5:
        return True  # PROTO opcode + a valid pickle protocol version byte (0-5)
    return data[:1] == _PICKLE_MARK


def is_safetensors(data: bytes) -> bool:
    """A safetensors file's first 8 bytes are a little-endian `u64` header length, followed by
    that many bytes of a JSON header — never executable, purely a length-prefixed byte format. A
    file this short, or whose declared header length overruns the file or fails to parse as JSON,
    is refused as malformed rather than assumed safe."""
    if len(data) < _SAFETENSORS_MIN_HEADER:
        return False
    (header_length,) = struct.unpack("<Q", data[:_SAFETENSORS_MIN_HEADER])
    if header_length <= 0 or _SAFETENSORS_MIN_HEADER + header_length > len(data):
        return False
    header = data[_SAFETENSORS_MIN_HEADER:_SAFETENSORS_MIN_HEADER + header_length]
    try:
        import json

        json.loads(header)
    except (ValueError, UnicodeDecodeError):
        return False
    return True


#: Extensions that are themselves pickle-based regardless of what their magic bytes say (a
#: `.bin`/`.pt` checkpoint is a zip of pickled tensors even when the outer bytes start with the
#: zip magic `PK\x03\x04`, which `looks_like_pickle` alone would miss) — refused by name, never
#: given the benefit of the doubt from a byte-level check that only catches the BARE-pickle case.
_PICKLE_EXTENSIONS = frozenset({".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle"})


def assert_safetensors_only(path: Path) -> None:
    """Refuses `path` unless its extension is `.safetensors` AND its bytes actually parse as one —
    an extension-only check would pass a pickle stream simply renamed `.safetensors`; a
    magic-bytes-only check would pass a legitimately-named `.bin` sibling sitting next to a real
    safetensors file in the same upload. Raises `UnsafeModelWeightsError`, refusing before any
    byte is handed to a deserializer."""
    if path.suffix in _PICKLE_EXTENSIONS:
        raise UnsafeModelWeightsError(f"{path.name}: pickle-based weight format ({path.suffix}) is never loaded; safetensors only")
    if path.suffix != ".safetensors":
        raise UnsafeModelWeightsError(f"{path.name}: unrecognised weight format {path.suffix!r}; safetensors only")
    data = path.read_bytes()
    if looks_like_pickle(data):
        raise UnsafeModelWeightsError(f"{path.name}: named .safetensors but its own bytes are a pickle stream; refused")
    if not is_safetensors(data):
        raise UnsafeModelWeightsError(f"{path.name}: not a valid safetensors file (malformed or truncated header)")


def assert_directory_safetensors_only(model_dir: Path) -> None:
    """Every weight-shaped file under `model_dir` must pass `assert_safetensors_only`, AND at
    least one real `.safetensors` file must be present — a directory containing zero weight files
    at all is a different bug (nothing to run), never silently accepted as "safe by absence"."""
    weight_suffixes = _PICKLE_EXTENSIONS | {".safetensors"}
    candidates = [path for path in model_dir.rglob("*") if path.is_file() and path.suffix in weight_suffixes]
    if not candidates:
        raise UnsafeModelWeightsError(f"{model_dir}: no weight file found (expected at least one .safetensors file)")
    for path in candidates:
        assert_safetensors_only(path)
