"""The blank-frame check has to be calibrated on REAL captures, not on PIL fixtures.

Its first version asked "does one colour cover 99.5%?" and passed a suite built from
`Image.new` and random noise. Against a real Chromium screenshot it called a plain sign-in form
blank — 99.6% white — and refused to analyse it. A confident wrong REFUSAL about a real screen is
worse than the confabulation it was written to prevent (#112), and it fires on the most
screenshotted page type there is.

Blank means NO CONTRAST ANYWHERE, not "one colour dominates". Text is a minority of pixels by
area on every page ever designed.
"""

import pytest

from interact.vision.measure import blank_frame_reason
from tests.support import browser_manager

# Each is (name, html, expect_blank). The sparse ones are the trap: almost all background.
PAGES = [
    ("sign-in form", """<style>body{font:14px system-ui;margin:0;background:#fff;color:#111}
        .c{max-width:320px;margin:80px auto}input{width:100%;padding:8px;margin:6px 0}
        button{padding:8px 16px}</style><div class=c><h1>Sign in</h1>
        <input placeholder=Email><input placeholder=Password type=password>
        <button>Continue</button></div>""", False),
    ("mobile header only", """<style>body{margin:0;background:#fff;font:16px system-ui}
        header{padding:12px;border-bottom:1px solid #ddd}</style><header>Account</header>""", False),
    ("dark terminal, one line", """<style>body{margin:0;background:#1e1e1e;color:#d4d4d4;
        font:13px monospace}</style><div>$ interact doctor</div>""", False),
    ("truly empty white", "<style>body{margin:0;background:#fff}</style>", True),
    ("truly empty black", "<style>body{margin:0;background:#000}</style>", True),
    ("crashed-window grey", "<style>body{margin:0;background:#1f1f1f}</style>", True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,html,expect_blank", PAGES, ids=[p[0] for p in PAGES])
async def test_blankness_matches_what_a_person_would_say(name, html, expect_blank):
    mgr = browser_manager()
    try:
        await mgr.ensure_ready()
    except Exception as exc:
        pytest.skip(f"no launchable chromium: {exc}")
    try:
        page = await mgr.get_page()
        await page.set_content(html)
        reason = blank_frame_reason(await page.screenshot(type="png"))
        if expect_blank:
            assert reason, f"{name}: a genuinely empty frame must be caught"
        else:
            assert reason is None, f"{name}: refused a real screen as blank — {reason}"
    finally:
        await mgr.close()
