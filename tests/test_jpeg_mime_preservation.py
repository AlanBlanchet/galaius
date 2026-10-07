"""A JPEG source file stays `image/jpeg` all the way through — capture, analysis, and debug save
— never silently re-encoded to PNG en route. Covers `screenshot`, `review_ui`, `verify_ui` (with
and without a reference image), and the debug save of a returned capture."""

import io
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image as PILImage

import galaius.server.vlm as server_vlm
from galaius.vision import MediaItem, VLMResult


def _jpeg_bytes() -> bytes:
    image = PILImage.new("RGB", (24, 16))
    for x in range(24):
        for y in range(16):
            image.putpixel((x, y), (x * 10, y * 14, (x + y) * 6))
    out = io.BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


@pytest.fixture
def srv():
    import galaius.server as _srv
    from galaius.server import breaker

    breaker.clear()
    _srv.config.component_criteria = "cap.gui_grounding"
    with patch.object(_srv.Debug, "save"):
        yield _srv
    _srv.config.clear_overrides()  # drop the transient override so it can't leak into later tests
    breaker.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "with_reference"),
    [("screenshot", False), ("review_ui", False), ("verify_ui", False), ("review_ui", True)],
)
async def test_file_image_callers_preserve_jpeg_mime(
    srv, tmp_path: Path, monkeypatch, tool_name: str, with_reference: bool
) -> None:
    jpeg = _jpeg_bytes()
    target = tmp_path / "build image.jpg"
    target.write_bytes(jpeg)
    captured: list[MediaItem] = []

    async def analyze(media, *args, **kwargs):
        captured.extend(media)
        return VLMResult(text="{}", elapsed=0, backend="api")

    monkeypatch.setattr(srv.vlm, "analyze_media", analyze)
    if tool_name == "screenshot":
        fn = getattr(srv.screenshot, "fn", srv.screenshot)
        await fn(target=f"file:{target}", query="describe")
    else:
        monkeypatch.setattr(
            srv.capture,
            "_resolve_capture",
            AsyncMock(
                return_value=(
                    jpeg,
                    "Image file",
                    jpeg if with_reference else None,
                    [],
                    None,
                    None,
                )
            ),
        )
        if tool_name == "review_ui":
            await srv.review_ui(reference=str(target) if with_reference else None)
        else:
            await srv.verify_ui(["matches"], target=f"file:{target}")

    assert captured[0].mime_type == "image/jpeg"
    if with_reference:
        assert [item.mime_type for item in captured] == ["image/jpeg", "image/jpeg"]


@pytest.mark.asyncio
async def test_public_file_screenshot_returns_and_debugs_jpeg_as_jpeg(
    srv, tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "photo.jpg"
    target.write_bytes(_jpeg_bytes())
    saved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        srv.Debug, "save", lambda name, data, *, ext="txt", **kwargs: saved.append((name, ext))
    )
    fn = getattr(srv.screenshot, "fn", srv.screenshot)
    result = await fn(target=f"file:{target}", return_image=True)
    assert result[1]._format == "jpeg"
    assert ("capture", "jpeg") in saved


def _jpeg() -> bytes:
    out = io.BytesIO()
    image = PILImage.new("RGB", (24, 16), "orange")
    for x in range(24):
        for y in range(16):
            image.putpixel((x, y), (x * 10, y * 14, (x + y) * 6))
    image.save(out, format="JPEG")
    return out.getvalue()


@pytest.mark.asyncio
async def test_vlm_preserves_jpeg_mime_for_primary_and_extra_images(monkeypatch) -> None:
    captured: list[MediaItem] = []

    async def analyze(media, *args, **kwargs):
        captured.extend(media)
        return VLMResult(text="ok", elapsed=0, backend="api")

    monkeypatch.setattr(server_vlm, "analyze_media", analyze)
    jpeg = _jpeg()
    result = await server_vlm._vlm(
        jpeg,
        "context",
        "query",
        mime="image/jpeg",
        extra_images=[MediaItem.from_bytes(jpeg, "image", "image/jpeg")],
    )

    assert result.text == "ok"
    assert [item.mime_type for item in captured] == ["image/jpeg", "image/jpeg"]
