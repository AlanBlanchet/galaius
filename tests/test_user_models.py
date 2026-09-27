"""`interact.user_models`: the fetch half of running a workspace's own registered model. No real
network or Docker daemon here (that is this feature's separate live-GPU proof) — these pin the
DISPATCH logic and the safety gate integration: a Hugging Face fetch is restricted to the pinned
safetensors + safe metadata, a server-mediated fetch refuses a disguised pickle payload exactly
like a Hub fetch does, and a docker pull failure is never swallowed."""

import http.server
import struct
import subprocess
import threading
from pathlib import Path

import pytest
from interact_core import ConnectionResourceRef, UnsafeModelWeightsError, UserModelOrigin

from interact import user_models

_CONNECTION = ConnectionResourceRef(id="00000000-0000-0000-0000-000000000001", revision="00000000-0000-0000-0000-000000000002", capability="read")


def _safetensors_bytes() -> bytes:
    header = b'{"tensor":{"dtype":"F32","shape":[1],"data_offsets":[0,4]}}'
    return struct.pack("<Q", len(header)) + header + b"\x00\x00\x80?"


def test_supported_tasks_are_named_not_guessed() -> None:
    assert user_models.SUPPORTED_TASKS == {"object-detection", "image-segmentation"}


def test_huggingface_fetch_restricts_allow_patterns_to_pinned_weights_and_safe_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    (snapshot_dir / "model.safetensors").write_bytes(_safetensors_bytes())
    (snapshot_dir / "config.json").write_text("{}")
    captured = {}

    def fake_snapshot_download(*, repo_id, revision, cache_dir, allow_patterns):
        captured["repo_id"], captured["revision"], captured["allow_patterns"] = repo_id, revision, allow_patterns
        return str(snapshot_dir)

    monkeypatch.setattr("huggingface_hub.snapshot_download", fake_snapshot_download)
    origin = UserModelOrigin(kind="huggingface_repo", repo_id="hustvl/yolos-tiny", revision="main", weight_files=("model.safetensors",))

    result = user_models.fetch_weights_dir(origin, "model-id", tmp_path / "cache", "https://server", "token")

    assert result == snapshot_dir
    assert captured["repo_id"] == "hustvl/yolos-tiny"
    assert "model.safetensors" in captured["allow_patterns"]
    assert "*.json" in captured["allow_patterns"]
    assert not any(pattern.endswith((".bin", ".pt", ".pth", ".ckpt", ".pkl")) for pattern in captured["allow_patterns"])


def test_huggingface_fetch_refuses_a_disguised_pickle_the_hub_actually_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Registration-time already trusted the Hub's file listing once; this is the machine's own,
    independent check of the bytes that actually landed — it must not simply trust the claim again."""
    import pickle

    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    (snapshot_dir / "model.safetensors").write_bytes(pickle.dumps({"a": 1}))
    monkeypatch.setattr("huggingface_hub.snapshot_download", lambda **kw: str(snapshot_dir))
    origin = UserModelOrigin(kind="huggingface_repo", repo_id="org/name", weight_files=("model.safetensors",))

    with pytest.raises(UnsafeModelWeightsError):
        user_models.fetch_weights_dir(origin, "model-id", tmp_path / "cache", "https://server", "token")


class _WeightsHandler(http.server.BaseHTTPRequestHandler):
    payload = b""

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        assert self.headers.get("authorization") == "Bearer secret-token"
        self.send_response(200)
        self.send_header("content-length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def weights_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _WeightsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_server_mediated_fetch_downloads_and_verifies_real_safetensors_bytes(tmp_path: Path, weights_server) -> None:
    _WeightsHandler.payload = _safetensors_bytes()
    origin = UserModelOrigin(kind="uploaded_weights", connection=_CONNECTION, path="weights/deadbeef/model.safetensors", digest="0" * 64)
    server_url = f"http://{weights_server.server_address[0]}:{weights_server.server_address[1]}"

    directory = user_models.fetch_weights_dir(origin, "model-id", tmp_path / "cache", server_url, "secret-token")

    assert (directory / "model.safetensors").read_bytes() == _safetensors_bytes()


def test_server_mediated_fetch_refuses_a_pickle_stream_the_server_answered_with(tmp_path: Path, weights_server) -> None:
    import pickle

    _WeightsHandler.payload = pickle.dumps({"a": 1})
    origin = UserModelOrigin(kind="uploaded_weights", connection=_CONNECTION, path="weights/deadbeef/model.safetensors", digest="0" * 64)
    server_url = f"http://{weights_server.server_address[0]}:{weights_server.server_address[1]}"

    with pytest.raises(UnsafeModelWeightsError):
        user_models.fetch_weights_dir(origin, "model-id", tmp_path / "cache", server_url, "secret-token")


def test_docker_pull_returns_the_reference_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {}

    def fake_run(args, **kwargs):
        calls["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    origin = UserModelOrigin(kind="docker_image", image="registry.example/model:latest")

    assert user_models.pull_docker_image(origin) == "registry.example/model:latest"
    assert calls["args"] == ["docker", "pull", "registry.example/model:latest"]


def test_docker_pull_raises_with_the_daemons_own_error_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="", stderr="no such image"))
    origin = UserModelOrigin(kind="docker_image", image="registry.example/missing:latest")

    with pytest.raises(RuntimeError, match="no such image"):
        user_models.pull_docker_image(origin)
