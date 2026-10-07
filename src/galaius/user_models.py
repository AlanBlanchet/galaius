"""Fetch a workspace's own registered model (`galaius_core.UserModelOrigin`) to this machine's
local model cache — the fetch half of "run inferences on any kind of model, even your own": the
runner reads `origin` and gets weight bytes onto disk, verified as safetensors-only, BEFORE
anything ever deserializes them. Loading is the vision runtime's job (`vision_infer.py`, inside the
isolated `vision_env` venv); this module never imports torch/transformers, matching the process
split `vision_env.py` documents — a machine with no vision node configured never pays for either.

Four origin kinds this codebase defines, three fetch mechanics (the fourth, `docker_image`, never
produces a local weights directory — the image itself is the artifact):

- `huggingface_repo`: `huggingface_hub.snapshot_download`, restricted by `allow_patterns` to the
  pinned `.safetensors` file(s) plus safe (JSON/text) architecture metadata — never any other file
  in the repo, closing the "repo also ships a pickle fallback" path structurally rather than by
  trusting the Hub's file list a second time (the server's registration-time check already
  trusted it once; this is the machine's OWN independent fetch).
- `uploaded_weights` / `object_storage`: the bytes live on the SERVER (its own upload store, or a
  connection whose credentials only the server holds) — fetched over a plain HTTPS GET the runner
  already has a bearer token for (the same `iwm_...` token `galaius machine connect` uses for the
  machine-channel websocket), never a new credential minted for this.
- `docker_image`: `pull_docker_image` only pulls and confirms the image exists; the machine has no
  proven sandboxed runtime to actually RUN one yet (see `machines.py`'s `_run_user_model`).

Every weight-bearing branch re-checks the bytes it fetched (`galaius.model_safety`) before
returning a path — defense in depth: the SOURCE's claim was already checked once at registration
time; this is independent verification of what actually landed on THIS machine's disk.
"""

from __future__ import annotations

import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from galaius_core import UserModelOrigin

from . import USER_AGENT, model_safety

#: `ModelTask`s this machine's generalized loader (`vision_infer.py`'s transformers/timm path) can
#: run today. Growing this list (a text-generation loader, an ONNX runtime) is that runtime's own
#: later change — this machine never guesses at an unlisted task, it refuses by name.
SUPPORTED_TASKS: frozenset[str] = frozenset({"object-detection", "image-segmentation"})

#: Safe (never weight-bearing, never pickle-executable) file patterns fetched ALONGSIDE the pinned
#: safetensors weights from a Hugging Face repo — architecture config, tokenizer/processor
#: metadata a bare state dict cannot be loaded without. Never a wildcard that could also match a
#: `.bin`/`.pt`/`.ckpt` fallback (`huggingface_hub`'s `allow_patterns` is a WHITELIST: a file
#: matching none of these, including every pickle-format weight file, is never even requested).
_SAFE_METADATA_PATTERNS = ("*.json", "*.txt", "*.model", "vocab*", "merges.txt")

_UNSAFE_NAME = re.compile(r"[^\w.() -]")


def fetch_weights_dir(origin: UserModelOrigin, model_id: str, cache_root: Path, server_url: str, token: str) -> Path:
    """A local directory holding `origin`'s weights (and, for a Hub repo, its architecture
    metadata), byte-verified safetensors-only before this returns. Raises `model_safety`'s own
    error for a byte-level failure (a masquerading pickle, a truncated download) — never hands an
    unverified path to a loader."""
    if origin.kind == "huggingface_repo":
        return _fetch_huggingface(origin, cache_root)
    if origin.kind in ("uploaded_weights", "object_storage"):
        return _fetch_from_server(origin, model_id, cache_root, server_url, token)
    raise ValueError(f"no local-weights fetch for origin kind {origin.kind!r}")


def _fetch_huggingface(origin: UserModelOrigin, cache_root: Path) -> Path:
    from huggingface_hub import snapshot_download

    patterns = [*(origin.weight_files or ()), *_SAFE_METADATA_PATTERNS]
    directory = Path(snapshot_download(
        repo_id=origin.repo_id, revision=origin.revision or "main",
        cache_dir=str(cache_root / "hf"), allow_patterns=patterns,
    ))
    model_safety.assert_directory_safetensors_only(directory)
    return directory


def _fetch_from_server(origin: UserModelOrigin, model_id: str, cache_root: Path, server_url: str, token: str) -> Path:
    """`uploaded_weights`/`object_storage`: the server reads the bytes (its own upload store, or
    the connection it alone holds credentials for) and answers a plain authenticated GET; this machine never
    receives or needs the connection's own secret."""
    directory = cache_root / "user" / model_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    name = _UNSAFE_NAME.sub("_", (origin.path or "weights.safetensors").rsplit("/", 1)[-1]) or "weights.safetensors"
    weights_path = directory / name
    temporary = weights_path.with_name(f".{name}.{os.getpid()}.part")
    url = f"{server_url}/v1/machine-channel/models/{model_id}/weights"
    request = urllib.request.Request(url, headers={"authorization": f"Bearer {token}", "User-Agent": USER_AGENT})
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            with urllib.request.urlopen(request, timeout=300) as response, os.fdopen(descriptor, "wb") as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"server refused this model's weights: HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"could not reach the server for this model's weights: {error.reason}") from error
        os.replace(temporary, weights_path)
    finally:
        temporary.unlink(missing_ok=True)
    model_safety.assert_safetensors_only(weights_path)
    return directory


def pull_docker_image(origin: UserModelOrigin, timeout: float = 900) -> str:
    """`docker pull`s `origin.image` and returns the pulled reference — confirms the registered
    image still exists and is reachable, real, useful work even though this machine cannot yet run
    it (`machines.py`'s `_run_user_model` refuses execution right after this, by name)."""
    reference = origin.image or ""
    if not reference:
        raise ValueError("docker origin has no image reference")
    completed = subprocess.run(["docker", "pull", reference], capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "docker pull failed").strip()[-2000:])
    return reference
