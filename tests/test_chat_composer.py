"""The chat panel's composer, exercised in a real browser against `chat/dark.html`.

Losing typed text is the one thing a chat box must never do — not on a failed send, not on a
transcript patch arriving mid-keystroke.
"""

import os
import subprocess
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parent.parent / "clients" / "vscode"


@pytest.fixture(scope="module")
def browser():
    """ONE browser for the module.

    `asyncio_mode = "auto"` puts every test inside an event loop, and Playwright's sync API
    refuses to open a second context inside one — so a per-test `sync_playwright()` passes when
    run alone and errors in a full run, which is the worst kind of test. It is also one chromium
    instead of six on a machine with no swap.
    """
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


def _unavailable(why: str) -> None:
    """Skip locally, FAIL in CI.

    Skipping locally is fine — not every machine has the webview toolchain. Skipping in CI makes
    the guard decorative exactly where it is the only thing watching: a probe that silently does
    not run is a note, not an invariant, and this file has already shipped that failure once (four
    tests reported "fixture is not present" for weeks after /tmp was cleaned). `test_paths.py`
    already draws this line; these fixtures did not.
    """
    if os.environ.get("CI"):
        pytest.fail(f"{why} — this must not skip in CI, it is the only thing checking this")
    pytest.skip(why)


@pytest.fixture(scope="session")
def panel_pages(tmp_path_factory):
    """Render the side panel's documents from source, once per session.

    The generator lives IN the repo (`clients/vscode/webview/dev/panels.ts`). It used to be a
    hand-written file in /tmp, so when /tmp was cleaned these tests reported "fixture is not
    present" and skipped — silently, which reads as a deliberate skip rather than a guard that has
    gone. A fixture outside the repo is a test that stops guarding without telling anyone.
    """
    ext = Path(__file__).resolve().parent.parent / "clients" / "vscode"
    out = tmp_path_factory.mktemp("panels")
    bundle = out / "panels.js"
    build = subprocess.run(
        ["npx", "esbuild", "webview/dev/panels.ts", "--bundle", f"--outfile={bundle}",
         "--format=cjs", "--platform=node", "--target=es2022"],
        cwd=ext, capture_output=True, text=True,
    )
    if build.returncode != 0:
        _unavailable(f"could not build the panel fixture: {build.stderr[-300:]}")
    run = subprocess.run(["node", str(bundle), str(out)], capture_output=True, text=True)
    if run.returncode != 0:
        _unavailable(f"could not render the panel fixture: {run.stderr[-300:]}")
    return out


@pytest.fixture(scope="module")
def chat_page(browser, panel_pages):
    """The chat document, rendered from source with the host's API stubbed."""
    pg = browser.new_page(viewport={"width": 420, "height": 600})
    pg.goto((panel_pages / "chat" / "dark.html").as_uri())
    pg.wait_for_timeout(300)
    yield pg
    pg.close()


def test_a_failed_send_returns_your_message(chat_page):
    """It used to clear the box on submit, so a send to an agent that had ended left you a toast
    and nothing else. Losing typed text is the one thing a chat box must never do."""
    got = chat_page.evaluate(
        """() => {
          const box = document.getElementById('message');
          box.value = 'a message I do not want to lose';
          document.getElementById('composer').dispatchEvent(
            new Event('submit', {cancelable: true}));
          const emptied = box.value;
          window.dispatchEvent(new MessageEvent('message', {data: {type: 'sent', ok: false}}));
          return {emptied, restored: box.value};
        }"""
    )
    assert got["emptied"] == "", "the box must empty instantly — a laggy chat feels broken"
    assert got["restored"] == "a message I do not want to lose"


def test_a_streaming_update_does_not_wipe_what_you_are_typing(chat_page):
    """The panel patches the transcript while an agent works. Rebuilding the document instead
    would destroy a half-written reply every time the agent said anything."""
    got = chat_page.evaluate(
        """() => {
          const box = document.getElementById('message');
          box.value = 'half-typed thought';
          window.dispatchEvent(new MessageEvent('message',
            {data: {type: 'transcript', html: '<p>new turn</p>'}}));
          return box.value;
        }"""
    )
    assert got == "half-typed thought"
