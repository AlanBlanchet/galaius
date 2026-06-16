"""An inline `screenshot` action in run_actions must honour `path` like the standalone tool (#27).

It used to be silently dropped (ScreenshotAction had no `path` field), forcing a separate standalone
screenshot call that re-captured a now-changed page. The pure test pins the field; the integration
test proves the file is actually written through the real browser dispatch.
"""

import pytest

from interact.actions import ScreenshotAction


def test_screenshot_action_accepts_path():
    assert ScreenshotAction(path="/abs/x.png").path == "/abs/x.png"
    assert ScreenshotAction().path is None


@pytest.mark.asyncio
async def test_inline_screenshot_writes_file(tmp_path):
    from interact.browser import BrowserManager
    from interact.config import Config
    from interact.dispatch import _run_actions_browser

    mgr = BrowserManager(Config(headless=True, browser_type="chromium"))
    try:
        await mgr.ensure_ready()
    except Exception as exc:  # no browser provisioned (bare CI)
        pytest.skip(f"no launchable chromium: {exc}")
    try:
        page = await mgr.get_page(0)
        await page.set_content("<title>T</title><body>hi</body>")
        out = tmp_path / "inline.png"
        # no query → no VLM/key needed; the path must still be written
        await _run_actions_browser(
            mgr, [ScreenshotAction(path=str(out))], None, None, None, "default"
        )
        assert out.exists(), "inline screenshot path was not written"
        assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    finally:
        await mgr.close()
