"""#43: a profile-backed browser session persists cookies/login on disk so an authenticated flow
survives a restart — running through the reliable DOM-ref path instead of the flaky desktop-window
VLM path. Opt in with INTERACT_BROWSER_PROFILE_DIR (config.browser_profile_dir); each session gets
its own <base>/<session> subdir (Playwright locks a user-data-dir to one live context)."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from interact.browser import BrowserManager, SessionRegistry
from interact.config import Config


# --- profile-dir resolution: opt-in + per-session isolation ---


def test_no_profile_dir_means_ephemeral():
    mgr = BrowserManager(Config(browser_profile_dir=None))
    assert mgr._profile_dir is None
    assert mgr._persistent is False


def test_each_session_gets_its_own_profile_subdir(tmp_path):
    cfg = Config(browser_profile_dir=tmp_path)
    a = BrowserManager(cfg, "a")
    b = BrowserManager(cfg, "b")
    assert a._profile_dir == tmp_path / "a"
    assert b._profile_dir == tmp_path / "b"
    assert a._persistent and b._persistent


def test_registry_keys_persistent_profile_by_session_id(tmp_path):
    reg = SessionRegistry(Config(browser_profile_dir=tmp_path))
    assert reg.get("alpha")._profile_dir == tmp_path / "alpha"
    assert reg.get("beta")._profile_dir == tmp_path / "beta"


# --- _new_context branches on persistence (mocked, no real browser) ---


def _fake_context(pages: int):
    ctx = MagicMock()
    ctx.pages = [MagicMock() for _ in range(pages)]
    ctx.set_default_timeout = MagicMock()
    ctx.grant_permissions = AsyncMock()
    ctx.new_page = AsyncMock(return_value=MagicMock())
    return ctx


@pytest.mark.asyncio
async def test_persistent_new_context_launches_from_the_session_profile_dir(tmp_path):
    mgr = BrowserManager(Config(browser_profile_dir=tmp_path), "work")
    ctx = _fake_context(pages=1)  # a persistent context opens with one page already
    launcher = MagicMock()
    launcher.launch_persistent_context = AsyncMock(return_value=ctx)
    mgr._playwright = MagicMock(chromium=launcher)

    await mgr._new_context()

    launcher.launch_persistent_context.assert_awaited_once()
    args, kwargs = launcher.launch_persistent_context.call_args
    assert args[0] == str(tmp_path / "work")  # the per-session profile dir
    assert kwargs["headless"] == mgr._config.headless
    assert mgr._context is ctx
    assert mgr._browser is None  # persistent sessions never open a standalone Browser
    ctx.new_page.assert_not_awaited()  # reuse the page the persistent context came with


@pytest.mark.asyncio
async def test_ephemeral_new_context_uses_new_context_and_opens_a_page():
    mgr = BrowserManager(Config(browser_profile_dir=None))
    ctx = _fake_context(pages=0)  # an ephemeral context starts empty → must open the first page
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=ctx)
    mgr._browser = browser

    await mgr._new_context()

    browser.new_context.assert_awaited_once()
    ctx.new_page.assert_awaited_once()
    assert mgr._context is ctx


# --- the real thing: a cookie survives a full close+reopen (a server restart) ---


@pytest.mark.asyncio
async def test_persistent_profile_keeps_a_cookie_across_a_restart(tmp_path):
    cfg = Config(headless=True, browser_type="chromium", browser_profile_dir=tmp_path)
    m1 = BrowserManager(cfg, "auth")
    try:
        await m1.ensure_ready()
    except Exception as exc:  # no launchable chromium (bare CI) → not this test's concern
        pytest.skip(f"no launchable chromium: {exc}")
    try:
        await m1._context.add_cookies([{
            "name": "tok", "value": "secret-42", "domain": "example.com", "path": "/",
            "expires": time.time() + 3600,  # persistent — a session cookie wouldn't survive a restart
        }])
    finally:
        await m1.close()

    # A brand-new manager on the SAME profile dir is what an MCP-server restart looks like.
    m2 = BrowserManager(cfg, "auth")
    try:
        await m2.ensure_ready()
        cookies = await m2._context.cookies("https://example.com/")
        assert any(c["name"] == "tok" and c["value"] == "secret-42" for c in cookies)
    finally:
        await m2.close()
