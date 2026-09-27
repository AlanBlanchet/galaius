"""The `screenshot` tool's `path`/`target` contract: standalone tool and inline run_actions step.

#44: `path` is an OUTPUT sink — using it to "analyze an existing image" silently captures and
CLOBBERS that file, then describes the new capture. Fix: target="file:<path>" analyzes an existing
image without writing, and a capture that overwrites an existing `path` says so.

#27: an inline `screenshot` action in run_actions must honour `path` like the standalone tool. It
used to be silently dropped (ScreenshotAction had no `path` field), forcing a separate standalone
screenshot call that re-captured a now-changed page. The pure test pins the field; the integration
test proves the file is actually written through the real browser dispatch.
"""

import pytest

import interact.server as srv
from interact.actions import ScreenshotAction
from interact.vision import VLMResult
from tests.support import async_capture, browser_manager, solid_png


@pytest.mark.parametrize("target", ["file:{p}", "file://{p}"])
@pytest.mark.asyncio
async def test_screenshot_file_target_analyzes_without_capturing(monkeypatch, tmp_path, target):
    """target="file:<path>" feeds the EXISTING bytes to the VLM and never captures/writes."""
    img = solid_png(40, 30, (0, 128, 255))
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
    existing.write_bytes(solid_png(colour=(255, 0, 0)))

    # Browser-session capture path: stub the page capture + scan so no real browser is needed.
    class _State:
        screenshot_base64 = __import__("base64").b64encode(solid_png()).decode()

        def text_summary(self):
            return "page"

    monkeypatch.setattr(srv.targets, "_resolve_target", lambda *a, **k: (None, object(), None))
    monkeypatch.setattr(srv.capture, "_capture", async_capture(_State()))
    monkeypatch.setattr(srv.capture, "_scan_elements", async_capture([]))
    out = await srv.screenshot(target="browser", path=str(existing))
    assert "overwrote existing file" in out and str(existing) in out


def test_screenshot_action_accepts_path():
    assert ScreenshotAction(path="/abs/x.png").path == "/abs/x.png"
    assert ScreenshotAction().path is None


@pytest.mark.asyncio
async def test_inline_screenshot_writes_file(tmp_path):
    from interact.actions.dispatch import _run_actions_browser

    mgr = browser_manager()
    try:
        await mgr.ensure_ready()
    except Exception as exc:  # no browser provisioned (bare CI)
        pytest.skip(f"no launchable chromium: {exc}")
    try:
        page = await mgr.get_page(0)
        await page.set_content("<title>T</title><body>hi</body>")
        out = tmp_path / "inline.png"
        # no query → no VLM/key needed; the path must still be written
        result = await _run_actions_browser(
            mgr, [ScreenshotAction(path=str(out))], None, None, None, "default"
        )
        assert out.exists(), "inline screenshot path was not written"
        # ...and the report NAMES the absolute file with its size, like every saving tool (#120),
        # instead of echoing the caller's own string back.
        assert f"Saved to {out} ({out.stat().st_size} bytes)" in str(result), str(result)[-300:]
        assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    finally:
        await mgr.close()
