"""`MediaItem` payload validation: malformed base64, a declared mime that lies about the bytes,
an over-limit decode, and the MPEG frame-sync/header checks that keep a bogus audio blob from
reaching a model as if it were real audio.
"""

import base64
import math

import pytest

from interact.vision import MediaItem
from tests.support import solid_png


def test_media_item_rejects_malformed_base64_at_construction() -> None:
    with pytest.raises(ValueError, match="base64"):
        MediaItem(data="%%%", media_type="image", mime_type="image/png")


def test_media_item_rejects_declared_mime_that_does_not_match_bytes() -> None:
    with pytest.raises(ValueError, match="does not match"):
        MediaItem.from_bytes(b"not a png", "image", "image/png").decoded(max_bytes=100)


def test_media_item_rejects_decoded_payload_over_byte_limit() -> None:
    oversized = MediaItem(data=base64.b64encode(b"12345").decode(), media_type="image", mime_type="image/png")
    with pytest.raises(ValueError, match="exceeds"):
        oversized.decoded(max_bytes=4)


@pytest.mark.parametrize("second", [0xFA, 0xFB, 0xF2, 0xF3, 0xE2, 0xE3])
def test_media_item_accepts_valid_mpeg_frame_sync_variants(second: int) -> None:
    item = MediaItem.from_bytes(bytes([0xFF, second, 0x90, 0x64]), "audio", "audio/mpeg")
    assert item.decoded().startswith(b"\xff")


@pytest.mark.parametrize("prefix", [b"\xff\x00", b"\xff\x7a", b"not-mp3"])
def test_media_item_rejects_malformed_mpeg_sync(prefix: bytes) -> None:
    with pytest.raises(ValueError, match="does not match"):
        MediaItem.from_bytes(prefix + b"payload", "audio", "audio/mpeg").decoded()


@pytest.mark.parametrize(
    "header",
    [b"\xff\xea\x90\x64", b"\xff\xf8\x90\x64", b"\xff\xfa\x00\x64", b"\xff\xfa\xf0\x64", b"\xff\xfa\x9c\x64"],
)
def test_media_item_rejects_invalid_mpeg_header_fields(header: bytes) -> None:
    with pytest.raises(ValueError, match="does not match"):
        MediaItem.from_bytes(header, "audio", "audio/mpeg").decoded()


def test_media_item_decoded_size_preflight_accounts_for_base64_padding() -> None:
    exact = MediaItem.from_bytes(b"fLaC", "audio", "audio/flac")
    assert exact.decoded(max_bytes=4) == b"fLaC"
    with pytest.raises(ValueError, match="exceeds"):
        exact.decoded(max_bytes=3)


@pytest.mark.parametrize("timestamp", [math.nan, math.inf])
def test_media_item_rejects_non_finite_timestamps(timestamp: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        MediaItem(
            data=base64.b64encode(solid_png(12, 8, (0, 0, 128))).decode(),
            media_type="image",
            mime_type="image/png",
            timestamp_seconds=timestamp,
        )
