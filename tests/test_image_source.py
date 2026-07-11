"""#44: `path` is an OUTPUT sink — using it to "analyze an existing image" silently captures and
CLOBBERS that file, then describes the new capture. Fix: target="file:<path>" analyzes an existing
image without writing, and a capture that overwrites an existing `path` says so."""

import io

import numpy as np
import pytest
from PIL import Image

import interact.server as srv
from interact.vision import VLMResult


def _png(color=(0, 128, 255), size=(40, 30)) -> bytes:
    arr = np.full((size[1], size[0], 3), color, np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    return buf.getvalue()


def _ascapture(value):
    async def _f(*a, **k):
        return value
    return _f


@pytest.mark.parametrize("target", ["file:{p}", "file://{p}"])
@pytest.mark.asyncio
async def test_screenshot_file_target_analyzes_without_capturing(monkeypatch, tmp_path, target):
    """target="file:<path>" feeds the EXISTING bytes to the VLM and never captures/writes."""
    img = _png()
    p = tmp_path / "artifact.png"
    p.write_bytes(img)

    seen = {}

    async def fake_vlm(data, context, query=None, **kw):
        seen["data"] = data
        return VLMResult(text="a blue rectangle", elapsed=0.1, model="m")

    monkeypatch.setattr(srv.vlm, "_vlm", fake_vlm)
    # If it tried to capture, this would raise — proving no capture path is taken.
    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: pytest.fail("must not capture a file: target"))
    out = await srv.screenshot(query="what shape?", target=target.format(p=p))
    assert "a blue rectangle" in out and seen["data"] == img
    assert p.read_bytes() == img  # untouched — no clobber


@pytest.mark.asyncio
async def test_file_target_missing_file_is_a_clean_error():
    out = await srv.screenshot(query="x", target="file:/no/such/file.png")
    assert out.startswith("ERROR") and "could not read image file" in out


@pytest.mark.asyncio
async def test_screenshot_notes_when_path_overwrites_an_existing_file(monkeypatch, tmp_path):
    """A real capture to an existing `path` must announce the overwrite so the result can't be
    mistaken for an analysis of the prior file."""
    existing = tmp_path / "prev.png"
    existing.write_bytes(_png(color=(255, 0, 0)))

    # Browser-session capture path: stub the page capture + scan so no real browser is needed.
    class _State:
        screenshot_base64 = __import__("base64").b64encode(_png()).decode()

        def text_summary(self):
            return "page"

    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (None, object(), None))
    monkeypatch.setattr(srv.capture, "_capture", _ascapture(_State()))
    monkeypatch.setattr(srv.capture, "_scan_elements", _ascapture([]))
    out = await srv.screenshot(target="browser", path=str(existing))
    assert "overwrote existing file" in out and str(existing) in out
