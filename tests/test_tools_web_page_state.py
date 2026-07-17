"""get_page_state must target a specific tab like get_interactive_elements does (#74): with two
tabs interleaved, a tab-less read returns whichever tab a PRIOR call left active — a real session
got the wrong tab's content and had to insert switch_tab before every read. `tab=` removes the
stateful dependency; the capture/scan plumbing already accepts it."""

import pytest

import interact.server as srv
from interact.browser import BrowserManager
from interact.config import Config


@pytest.mark.asyncio
async def test_get_page_state_reads_the_requested_tab_not_the_active_one(monkeypatch):
    mgr = BrowserManager(Config(headless=True, browser_type="chromium"))
    try:
        try:
            await mgr.ensure_ready()
        except Exception as exc:  # no browser provisioned (bare CI)
            pytest.skip(f"no launchable chromium: {exc}")
        page = await mgr.get_page()
        await page.goto("data:text/html,<title>AAA</title><button>alpha</button>")
        await mgr.new_tab("data:text/html,<title>BBB</title><button>beta</button>")
        assert mgr.active_tab == 1  # a prior call left tab 1 active — the #74 trap

        monkeypatch.setattr(srv.core._sessions, "get", lambda s: mgr)
        out = await srv.get_page_state(tab=0)
        assert "AAA" in out and "BBB" not in out  # the requested tab, not the active one

        out_active = await srv.get_page_state()
        assert "BBB" in out_active  # tab-less keeps the active-tab default (#30)
    finally:
        await mgr.close()
