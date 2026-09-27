"""Chrome DevTools Protocol bridge for a nested Electron/VS Code target (#126, #143, #174).

interact drives most desktop apps through synthetic X11 input (XTEST via xdotool) because that
is the only channel a native GTK/Qt/Flutter window understands. An Electron/Chromium window is
different: it is a real DOM underneath, and when the launch command already names a debug port
(``--remote-debugging-port``, in the owner's own launch recipes) that DOM is reachable directly —
DOM-accurate clicks (real focus transfer), real character input (no XKB keymap translation to
garble), and ``evaluate_js`` itself, none of which XTEST can give a webview.

Design (two lines):
1. Discovery — the port is read from the LAUNCHED argv (never invented): a literal
   ``--remote-debugging-port=N`` wins outright; ``N=0`` (OS-assigned) resolves by polling
   ``<profile>/DevToolsActivePort``, the file Chromium itself writes once its DevTools server is
   listening. No port named at all -> no CDP path, full stop.
2. Ownership/closing + multiple targets — one bridge is opened lazily per ``run_actions`` batch
   (see ``_CdpSlot`` in ``interact.actions.dispatch``), reused by every action in that same call,
   and closed in that call's ``finally`` — never cached across calls, so a relaunched app never
   inherits a stale connection. Several exposed targets (main window, a webview, the extension
   host, a hidden devtools page) are disambiguated by :func:`pick_target`: an explicit hint wins,
   else a ``vscode-webview:`` page wins (the surface this family is actually about), else the
   most-recently-created page.
"""

import re
import time
from pathlib import Path

import httpx

_PORT_RE = re.compile(r"^--remote-debugging-port(?:=(\d+))?$")


def declared_port(argv: list[str]) -> int | None:
    """The literal ``--remote-debugging-port`` value from a launched argv, or None when the
    command named no debug port at all — the caller then has no CDP path, only synthetic input.
    ``0`` is returned as-is; it means "OS-assigned", resolved separately via the profile's
    ``DevToolsActivePort`` file."""
    for i, tok in enumerate(argv):
        m = _PORT_RE.match(tok)
        if not m:
            continue
        if m.group(1) is not None:
            return int(m.group(1))
        if i + 1 < len(argv) and argv[i + 1].isdigit():  # `--remote-debugging-port 9222` form
            return int(argv[i + 1])
        return None
    return None


def declared_profile(argv: list[str]) -> Path | None:
    """The ``--user-data-dir`` an argv named, if any — where a ``0`` port's real value gets
    written."""
    for tok in argv:
        if tok.startswith("--user-data-dir="):
            return Path(tok.split("=", 1)[1])
    return None


def wait_for_active_port(profile: Path, timeout: float = 5.0, poll: float = 0.1) -> int | None:
    """Read the OS-assigned port Chromium writes to ``<profile>/DevToolsActivePort`` (first line)
    after a ``--remote-debugging-port=0`` launch. The file appears shortly after the process
    starts listening, not at exec time, so this polls rather than reading once."""
    target = profile / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while True:
        try:
            return int(target.read_text().splitlines()[0].strip())
        except (OSError, ValueError, IndexError):
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll)


def resolve_port(argv: list[str], timeout: float = 5.0) -> int | None:
    """The CDP port for a launched command, or None when the command named none — the ONE
    decision point that gates the whole bridge. Blocking (bounded by ``timeout``); callers on the
    event loop run it via ``asyncio.to_thread``."""
    port = declared_port(argv)
    if port is None or port != 0:
        return port
    profile = declared_profile(argv)
    if profile is None:
        return None
    return wait_for_active_port(profile, timeout=timeout)


async def list_targets(
    port: int, timeout: float = 3.0, transport: httpx.BaseTransport | None = None
) -> list[dict]:
    """The live CDP targets at ``port`` (Chromium's ``/json/list``) — one dict per page/webview/
    worker/etc. Raises on a dead or non-CDP port; a caller that swallowed this into an empty list
    would report "no targets" for what is actually "nothing is listening there". ``transport`` is
    test-only: swaps the real socket for an ``httpx.MockTransport`` fake CDP endpoint."""
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        resp = await client.get(f"http://127.0.0.1:{port}/json/list")
        resp.raise_for_status()
        return resp.json()


def pick_target(targets: list[dict], hint: str | None = None) -> dict | None:
    """The target to drive when a launch exposes several. Preference order: (1) title/url
    containing ``hint`` when the caller named one; (2) a ``vscode-webview:`` page — the surface
    #126/#143/#174 are actually about; (3) the LAST ``page``-type target — Chromium lists targets
    in creation order, so the most-recently-opened page is the most likely to be foreground; (4)
    None when there is no page-type target at all (only a background/service-worker/devtools
    page)."""
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        return None
    if hint:
        needle = hint.lower()
        named = [t for t in pages if needle in (t.get("title", "") + t.get("url", "")).lower()]
        if named:
            return named[-1]
    webviews = [t for t in pages if t.get("url", "").startswith("vscode-webview:")]
    if webviews:
        return webviews[-1]
    return pages[-1]


class CDPBridge:
    """One Playwright connection to an already-running CDP endpoint, holding the single Page the
    caller resolved via :func:`pick_target`. Never launches a browser — always attaches to a
    process interact already started via ``launch_app``."""

    def __init__(self, port: int, target: dict):
        self.port = port
        self.target = target
        self._playwright = None
        self._browser = None
        self.page = None

    async def connect(self) -> None:
        from playwright.async_api import async_playwright  # noqa: PLC0415 — only needed here

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.port}"
        )
        self.page = self._find_page()

    def _find_page(self):
        """The Playwright Page matching the chosen target, by URL — connect_over_cdp surfaces
        every target Chromium reports, and URL is the one field :func:`pick_target` already used
        to choose, so matching on it keeps the two steps consistent instead of re-deciding twice."""
        wanted = self.target.get("url")
        pages = [p for ctx in self._browser.contexts for p in ctx.pages]
        for p in pages:
            if p.url == wanted:
                return p
        return pages[-1] if pages else None

    async def close(self) -> None:
        # Never close the BROWSER (it's the app's own process, not ours to kill) — only the
        # connection interact opened onto it.
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
