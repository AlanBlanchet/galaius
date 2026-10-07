"""Adversarial proof of safeguard #6 (safetensors-only, no pickle): a REAL pickle RCE payload —
one whose `__reduce__` writes a marker file the instant it is unpickled — is refused before any
byte of it reaches a deserializer, disguised as `.safetensors` and under its own honest `.bin`
extension alike. The decisive check: the marker file is never created."""

import os
import pickle
import struct
from pathlib import Path

import pytest
from galaius_core import UnsafeModelWeightsError

from galaius.model_safety import assert_directory_safetensors_only, assert_safetensors_only, is_safetensors, looks_like_pickle


class _PopMarker:
    """A real, functioning pickle RCE gadget: `__reduce__` runs arbitrary code the moment
    `pickle.load`/`pickle.loads` deserializes an instance of this class — exactly the vector
    `torch.load` on a non-`weights_only` checkpoint is vulnerable to."""

    def __init__(self, marker_path: str) -> None:
        self.marker_path = marker_path

    def __reduce__(self):
        return (os.system, (f"touch {self.marker_path}",))


def _rce_payload() -> bytes:
    return pickle.dumps(_PopMarker("/should-never-be-created"))


def test_a_pickle_rce_payload_disguised_as_safetensors_is_refused(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    payload = pickle.dumps(_PopMarker(str(marker)))
    disguised = tmp_path / "model.safetensors"
    disguised.write_bytes(payload)

    with pytest.raises(UnsafeModelWeightsError, match="pickle stream"):
        assert_safetensors_only(disguised)

    assert not marker.exists()  # the RCE gadget never ran: refused before deserialization


def test_a_pickle_checkpoint_under_its_own_honest_extension_is_refused(tmp_path: Path) -> None:
    checkpoint = tmp_path / "pytorch_model.bin"
    checkpoint.write_bytes(_rce_payload())

    with pytest.raises(UnsafeModelWeightsError, match="pickle-based weight format"):
        assert_safetensors_only(checkpoint)


def test_a_genuine_safetensors_file_is_accepted(tmp_path: Path) -> None:
    header = b'{"tensor":{"dtype":"F32","shape":[1],"data_offsets":[0,4]}}'
    body = struct.pack("<Q", len(header)) + header + b"\x00\x00\x80?"
    real = tmp_path / "weights.safetensors"
    real.write_bytes(body)

    assert_safetensors_only(real)  # no raise
    assert is_safetensors(body)
    assert not looks_like_pickle(body)


def test_a_directory_with_one_disguised_pickle_among_real_safetensors_is_refused(tmp_path: Path) -> None:
    header = b'{"t":{"dtype":"F32","shape":[1],"data_offsets":[0,4]}}'
    (tmp_path / "shard_0.safetensors").write_bytes(struct.pack("<Q", len(header)) + header + b"\x00\x00\x80?")
    marker = tmp_path / "pwned"
    (tmp_path / "shard_1.safetensors").write_bytes(pickle.dumps(_PopMarker(str(marker))))

    with pytest.raises(UnsafeModelWeightsError):
        assert_directory_safetensors_only(tmp_path)
    assert not marker.exists()


def test_a_directory_with_no_weight_file_is_refused_not_silently_accepted(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("no weights here")
    with pytest.raises(UnsafeModelWeightsError, match="no weight file"):
        assert_directory_safetensors_only(tmp_path)
