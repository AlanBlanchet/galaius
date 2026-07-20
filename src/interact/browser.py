import sys
import subprocess
import tempfile
from collections import deque
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from interact.config import LOG_MAXLEN, Config
from interact.state import InteractiveElement


def chromium_launch_kwargs(browser_type: str, headless: bool, slow_mo: int) -> dict:
    """Launch kwargs for the automation browser. For Chromium, hide the default automation signals
    that ordinary bot-checks (Cloudflare et al.) fingerprint — drop the ``--enable-automation`` flag
    and the ``AutomationControlled`` blink feature (which sets ``navigator.webdriver``) — so a
    legitimate QA browse is less likely to be flagged (#69). Not a full stealth mode: an advanced
    challenge can still block; pair with a persistent authenticated profile (browser_profile_dir).
    Non-Chromium engines take only headless/slow_mo (they reject Chromium args)."""
    kw: dict = {"headless": headless, "slow_mo": slow_mo}
    if browser_type == "chromium":
        kw["args"] = ["--disable-blink-features=AutomationControlled"]
        kw["ignore_default_args"] = ["--enable-automation"]
    return kw


class BrowserManager:
    def __init__(self, config: Config, session_id: str = "default"):
        self._config = config
        self._session_id = session_id
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        # Persistent profile dir for this session (#43): when browser_profile_dir is configured the
        # context is launched from <base>/<session_id> so cookies/login survive a restart. Each
        # session gets its own subdir because Playwright locks a user-data-dir to one live context.
        base = config.browser_profile_dir
        self._profile_dir: Path | None = (Path(base) / session_id) if base else None
        self._element_map: dict[int, list[InteractiveElement]] = {}
        # Monotonic ref counter for the DOM scan: a node's ref (eN) is stable across scans within a
        # session — a NEW node gets the next number, a surviving node keeps its own — and the counter
        # is reset only here, on a new session (#35). Never reused → no two nodes collide on a ref.
        self._ref_counter = 0
        self._network_log: deque[dict] = deque(maxlen=LOG_MAXLEN)
        self._console_log: deque[dict] = deque(maxlen=LOG_MAXLEN)
        self._recording_dir: tempfile.TemporaryDirectory | None = None
        # An active device-emulation profile (set by emulate_device); None → the configured
        # default viewport at DPR 1. Folded into every new context via _context_kwargs.
        self._device_override: dict | None = None
        # The tab that tab-less tool calls (screenshot / get_page_state / get_interactive_elements)
        # act on. new_tab / switch_tab move it, so a standalone capture after a switch sees the tab
        # the agent switched to, not tab 0 (#30).
        self._active_tab = 0
        # Native JS dialogs (#77): Playwright would auto-dismiss an unhandled confirm()/prompt()
        # SILENTLY, so a dialog-gated click no-ops with no trace. We handle every dialog
        # ourselves: `_dialog_next` holds a one-shot directive armed by a handle_dialog action
        # (consumed by the next dialog; default = dismiss, the old behavior), and `_dialog_log`
        # records each dialog's type + message + outcome for the step report.
        self._dialog_next: dict | None = None
        self._dialog_log: list[str] = []
        # Last time this session's browser was touched (monotonic). The idle reaper closes a
        # session unused past the configured TTL so idle Chromium instances don't pile up.
        self._last_active = time.monotonic()
        # storage_state (+ url) captured when this session was idle-closed, restored lazily on the
        # next browser use so an idle close doesn't silently log the agent out (#36).
        self._pending_state: dict | None = None
        self._http_credentials: dict | None = None  # Basic-auth creds folded into each context (#70)

    @property
    def _persistent(self) -> bool:
        """True when this session keeps an on-disk profile (#43): the context is launched from a
        user-data-dir, so it persists cookies/login and has no separate Browser handle."""
        return self._profile_dir is not None

    def idle_seconds(self) -> float | None:
        """Seconds since the browser was last used, or None if no browser is open (nothing to reap)."""
        if self._browser is None:
            return None
        return time.monotonic() - self._last_active

    def is_idle(self, ttl: float) -> bool:
        """True if a browser is open and has gone unused for at least ``ttl`` seconds."""
        idle = self.idle_seconds()
        return idle is not None and idle >= ttl

    def _tab_key(self, tab: int | None) -> int:
        """Normalize a tab argument to a concrete index so a tab-less scan (tab=None → the active
        tab) and an explicit active-tab int land in the SAME element-map bucket. Without this a
        tab-less get_interactive_elements/get_page_state/screenshot stores its refs under key None
        while the following run_actions reads them under the active-tab int — None != 0, so every
        ref is lost between calls (#34)."""
        return self._active_tab if tab is None else tab

    def set_element_map(self, tab: int | None, elements: list[InteractiveElement]):
        self._element_map[self._tab_key(tab)] = elements

    def get_element(self, index: int, tab: int | None = None) -> InteractiveElement | None:
        for el in self._element_map.get(self._tab_key(tab), []):
            if el.index == index:
                return el
        return None

    @property
    def tab_count(self) -> int:
        if not self._context:
            return 0
        return len(self._context.pages)

    async def ensure_ready(self):
        if self._context:
            return
        if self._pending_state is not None:
            # Reopening after an idle close: restore cookies/localStorage (+ url) so the agent
            # isn't silently logged out (#36). Consumed once; a failure falls back to a fresh context.
            state, self._pending_state = self._pending_state, None
            try:
                await self.load_state(state)
                return
            except Exception:
                pass
        await self._ensure_browser()
        await self._new_context()

    @property
    def active_tab(self) -> int:
        return self._active_tab

    def set_active_tab(self, index: int) -> None:
        """Remember which tab later tab-less tool calls act on (#30)."""
        self._active_tab = max(0, index)

    async def get_page(self, tab_index: int | None = None) -> Page:
        """The page for ``tab_index``; ``None`` → the session's active tab, so a standalone tool
        call after a tab switch sees the tab the agent switched to, not tab 0 (#30)."""
        self._last_active = time.monotonic()  # every browser action funnels here → marks the session live
        await self.ensure_ready()
        pages = self._context.pages
        if tab_index is None:
            tab_index = min(self._active_tab, len(pages) - 1)  # a closed tab can leave it stale
        if 0 <= tab_index < len(pages):
            return pages[tab_index]
        raise IndexError(f"Tab {tab_index} does not exist — {len(pages)} tab(s) open")

    async def new_tab(self, url: str | None = None) -> int:
        await self.ensure_ready()
        page = await self._context.new_page()
        self._attach_page_listeners(page)
        if url:
            await page.goto(url)
        self._active_tab = len(self._context.pages) - 1  # a freshly opened tab becomes active
        return self._active_tab

    async def switch_tab(self, index: int) -> Page:
        page = await self.get_page(index)  # validates the index exists
        self._active_tab = index
        return page

    async def close_tab(self, tab_index: int):
        await self.ensure_ready()
        pages = self._context.pages
        if tab_index >= len(pages):
            raise IndexError(f"Tab {tab_index} not found")
        await pages[tab_index].close()
        # Keep the active tab valid + pointing at the same logical tab after the close.
        if tab_index == self._active_tab:
            self._active_tab = max(0, tab_index - 1)
        elif tab_index < self._active_tab:
            self._active_tab -= 1

    async def save_state(self) -> dict:
        await self.ensure_ready()
        state = await self._context.storage_state()
        page = self._context.pages[0] if self._context.pages else None
        if page and page.url != "about:blank":
            state["_url"] = page.url
        return state

    async def load_state(self, state: dict):
        await self._ensure_browser()
        url = state.pop("_url", None)
        if self._context:
            await self._context.close()
        self._element_map.clear()
        await self._new_context(state)
        if url:
            page = self._context.pages[0]
            await page.goto(url)

    @property
    def is_recording(self) -> bool:
        return self._recording_dir is not None

    async def start_recording(self) -> str:
        if self._recording_dir:
            raise RuntimeError("Already recording — call stop_recording first")
        await self.ensure_ready()
        page = self._context.pages[0] if self._context.pages else None
        url = page.url if page and page.url != "about:blank" else None
        cookies = await self._context.cookies() if self._context else []
        await self._context.close()
        self._element_map.clear()
        self._recording_dir = tempfile.TemporaryDirectory()
        await self._new_context(record_video_dir=self._recording_dir.name)
        if cookies:
            await self._context.add_cookies(cookies)
        page = self._context.pages[0]
        if url:
            await page.goto(url)
        return url or "about:blank"

    async def stop_recording(self) -> bytes:
        if not self._recording_dir:
            raise RuntimeError("Not recording — call start_recording first")
        page = self._context.pages[0] if self._context.pages else None
        url = page.url if page and page.url != "about:blank" else None
        cookies = await self._context.cookies() if self._context else []
        await self._context.close()
        self._context = None
        video_files = sorted(Path(self._recording_dir.name).glob("*.webm"))
        if not video_files:
            video_files = sorted(Path(self._recording_dir.name).iterdir())
        video_bytes = video_files[-1].read_bytes() if video_files else b""
        self._recording_dir.cleanup()
        self._recording_dir = None
        self._element_map.clear()
        await self._new_context()
        if cookies:
            await self._context.add_cookies(cookies)
        page = self._context.pages[0]
        if url:
            await page.goto(url)
        return video_bytes

    async def close(self):
        if self._persistent and self._context:
            await self._context.close()  # a persistent context owns its browser — closes both
            self._context = None
        elif self._browser:
            await self._browser.close()
            self._browser = None
            self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def emulate_device(
        self,
        *,
        device: str | None = None,
        width: int | None = None,
        height: int | None = None,
        device_scale_factor: float | None = None,
        is_mobile: bool | None = None,
        has_touch: bool | None = None,
        user_agent: str | None = None,
        reset: bool = False,
    ) -> str:
        """Set the session's viewport / device profile (true device metrics: CSS size, DPR, touch),
        then rebuild the context so later navigations & screenshots see it. Preserves cookies and
        re-opens the current URL. Returns a short human description. Raises ValueError on an unknown
        device name."""
        await self.ensure_ready()
        if reset:
            self._device_override = None
            await self._rebuild_context()
            return (
                f"viewport reset to {self._config.viewport_width}x"
                f"{self._config.viewport_height} (DPR 1)"
            )
        profile: dict = {}
        if device:
            spec = (self._playwright.devices or {}).get(device)
            if spec is None:
                raise ValueError(
                    f"Unknown device {device!r}. Use a Playwright device name "
                    "(e.g. 'iPhone 13', 'Pixel 7', 'iPad Mini'), or give explicit width+height."
                )
            vp = spec.get("viewport") or {}
            profile = {
                "width": vp.get("width"),
                "height": vp.get("height"),
                "device_scale_factor": spec.get("device_scale_factor"),
                "is_mobile": spec.get("is_mobile"),
                "has_touch": spec.get("has_touch"),
                "user_agent": spec.get("user_agent"),
            }
        # Explicit fields override / extend a named device.
        for key, val in (
            ("width", width),
            ("height", height),
            ("device_scale_factor", device_scale_factor),
            ("is_mobile", is_mobile),
            ("has_touch", has_touch),
            ("user_agent", user_agent),
        ):
            if val is not None:
                profile[key] = val
        self._device_override = profile
        await self._rebuild_context()
        return self._describe_device(profile)

    @staticmethod
    def _describe_device(p: dict) -> str:
        bits = [f"{p.get('width')}x{p.get('height')} CSS px"]
        dsf = p.get("device_scale_factor")
        if dsf:
            bits.append(f"DPR {dsf}")
        if p.get("is_mobile"):
            bits.append("mobile")
        if p.get("has_touch"):
            bits.append("touch")
        note = (
            " (screenshots are DPR-scaled; element-ref overlays may be offset at DPR≠1)"
            if dsf and float(dsf) != 1.0
            else ""
        )
        return "viewport set to " + ", ".join(bits) + note

    async def _rebuild_context(self):
        """Recreate the context with current kwargs, preserving cookies and the open URL — for a
        setting fixed at context creation (viewport / DPR / mobile / touch) that changed
        mid-session. Mirrors the start/stop-recording swap."""
        page = self._context.pages[0] if self._context and self._context.pages else None
        url = page.url if page and page.url != "about:blank" else None
        cookies = await self._context.cookies() if self._context else []
        if self._context:
            await self._context.close()
        self._element_map.clear()
        await self._new_context()
        if cookies:
            await self._context.add_cookies(cookies)
        if url:
            await self._context.pages[0].goto(url)

    def set_http_credentials(self, username: str, password: str) -> None:
        """Provide HTTP Basic-auth credentials for this session so Playwright authenticates at the
        context level — the native browser 'Sign in' dialog (which can't be typed into reliably,
        #70) never appears. Applied to every context this session creates from now on."""
        self._http_credentials = {"username": username, "password": password}

    def set_http_credentials_spec(self, spec: str | None) -> None:
        """Set (``"user:password"``) or clear (``None``) the session's Basic-auth credentials."""
        if not spec:
            self._http_credentials = None
            return
        username, _, password = spec.partition(":")
        self.set_http_credentials(username, password)

    async def apply_http_credentials(self, spec: str | None) -> None:
        """Set/clear Basic-auth creds and refresh the live context so they take effect on the next
        navigation — httpCredentials is a context-creation option, so an existing context is rebuilt
        (cookies preserved via storage_state for a non-persistent session, #70)."""
        self.set_http_credentials_spec(spec)
        if self._context is None:
            return  # no live context yet → the next _new_context folds the creds in
        state = None
        if not self._persistent:
            try:
                state = await self._context.storage_state()
            except Exception:
                pass
        await self._new_context(state)

    def _context_kwargs(self, record_video_dir: str | None = None) -> dict:
        dev = self._device_override or {}
        chromium = self._config.browser_type == "chromium"
        kw: dict = {
            "viewport": {
                "width": dev.get("width") or self._config.viewport_width,
                "height": dev.get("height") or self._config.viewport_height,
            },
            # Default DPR=1 so screenshot pixels == CSS pixels (and == coords returned by
            # getBoundingClientRect / element.boundingBox). Without this, high-DPI hosts produce
            # 2× screenshots that don't match DOM-reported coordinates and the VLM/annotator render
            # boxes offset bottom-right. emulate_device may raise it for true device metrics — the
            # action documents that ref overlays can then be offset.
            "device_scale_factor": float(dev["device_scale_factor"])
            if dev.get("device_scale_factor")
            else 1.0,
        }
        # is_mobile is Chromium-only (Firefox/WebKit reject it); has_touch / user_agent are general.
        if dev.get("is_mobile") and chromium:
            kw["is_mobile"] = True
        if dev.get("has_touch"):
            kw["has_touch"] = True
        if dev.get("user_agent"):
            kw["user_agent"] = dev["user_agent"]
        if record_video_dir:
            kw["record_video_dir"] = record_video_dir
            kw["record_video_size"] = {
                "width": self._config.viewport_width,
                "height": self._config.viewport_height,
            }
        if self._http_credentials:  # authenticate Basic-auth sites without the native dialog (#70)
            kw["http_credentials"] = self._http_credentials
        return kw

    def _install_browser(self):
        # `python -m playwright`, NOT the bare `playwright` CLI: the CLI isn't on PATH in an
        # installed tool env (uv tool / pipx), which crashed every launch with a cryptic
        # "[Errno 2] No such file or directory: 'playwright'".
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", self._config.browser_type],
            check=True,
            capture_output=True,
        )

    async def _ensure_browser(self):
        # Persistent sessions have no standalone Browser — the context is launched from the profile
        # dir in _new_context; here we only need the Playwright driver up (#43).
        if self._persistent:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            return
        if self._browser:
            return
        self._playwright = await async_playwright().start()
        launcher = getattr(self._playwright, self._config.browser_type)
        launch_kw = chromium_launch_kwargs(
            self._config.browser_type, self._config.headless, self._config.slow_mo
        )
        try:
            # Launch straight away — when the browser is already installed (the common case)
            # this skips the slow per-launch `playwright install` subprocess entirely.
            self._browser = await launcher.launch(**launch_kw)
        except Exception:
            # First run / missing browser → install once, then retry.
            self._install_browser()
            self._browser = await launcher.launch(**launch_kw)

    async def _new_context(self, storage_state: dict | None = None, record_video_dir: str | None = None):
        kwargs = self._context_kwargs(record_video_dir)
        if self._persistent:
            # The on-disk profile is the source of truth for cookies/login, so a persistent context
            # both launches the browser AND is the context (no new_context). storage_state — the
            # idle-close stash (#36) — doesn't apply: persistence is already durable on disk.
            self._profile_dir.mkdir(parents=True, exist_ok=True)
            launcher = getattr(self._playwright, self._config.browser_type)
            try:
                self._context = await launcher.launch_persistent_context(
                    str(self._profile_dir),
                    headless=self._config.headless, slow_mo=self._config.slow_mo, **kwargs,
                )
            except Exception:
                self._install_browser()
                self._context = await launcher.launch_persistent_context(
                    str(self._profile_dir),
                    headless=self._config.headless, slow_mo=self._config.slow_mo, **kwargs,
                )
        else:
            if storage_state is not None:
                kwargs["storage_state"] = storage_state
            self._context = await self._browser.new_context(**kwargs)
        self._active_tab = 0  # a fresh context starts on its first page
        # Fail fast on a missing/non-actionable selector: Playwright's 30s default makes a bad
        # selector hang the agent for half a minute before erroring. config.wait_timeout (10s) is
        # the one knob; a dead selector then surfaces as an actionable nudge (see dispatch) in ~10s.
        from interact.runtime import config

        self._context.set_default_timeout(config.wait_timeout)
        await self._context.grant_permissions(["clipboard-read", "clipboard-write"])
        # A persistent context opens with one page already; an ephemeral new_context has none.
        page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._attach_page_listeners(page)

    def arm_dialog(self, action: str, prompt_text: str | None = None) -> None:
        """Arm how the NEXT dialog is answered (one-shot) — the handle_dialog action (#77)."""
        self._dialog_next = {"action": action, "prompt_text": prompt_text}

    def drain_dialog_log(self) -> list[str]:
        """The dialogs handled since the last drain, for the step report."""
        out, self._dialog_log = self._dialog_log, []
        return out

    async def _on_dialog(self, dialog) -> None:
        directive, self._dialog_next = self._dialog_next, None
        try:
            if directive and directive["action"] == "accept":
                await dialog.accept(directive.get("prompt_text") or "")
                outcome = "accepted (armed)"
            elif directive:
                await dialog.dismiss()
                outcome = "dismissed (armed)"
            else:
                await dialog.dismiss()
                outcome = "dismissed (default — arm with a handle_dialog step to accept)"
        except Exception:  # the page may be gone mid-dialog; never crash the listener
            return
        self._dialog_log.append(f"{dialog.type}({dialog.message!r}) → {outcome}")

    def _attach_page_listeners(self, page: Page):
        page.on("dialog", self._on_dialog)
        page.on(
            "request",
            lambda req: self._network_log.append(
                {
                    "method": req.method,
                    "url": req.url,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                }
            ),
        )
        page.on("response", lambda resp: self._on_response(resp))
        page.on(
            "console",
            lambda msg: self._console_log.append(
                {
                    "level": msg.type,
                    "text": msg.text,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                }
            ),
        )
        page.on(
            "pageerror",
            lambda err: self._console_log.append(
                {
                    "level": "error",
                    "text": str(err),
                    "ts": datetime.now().isoformat(timespec="seconds"),
                }
            ),
        )

    def _on_response(self, response):
        url = response.url
        for entry in reversed(self._network_log):
            if entry["url"] == url and "status" not in entry:
                entry["status"] = response.status
                entry["content_type"] = response.headers.get("content-type", "")
                break

    def drain_network_log(self, clear: bool = False) -> list[dict]:
        entries = list(self._network_log)
        if clear:
            self._network_log.clear()
        return entries

    def drain_console_log(self, clear: bool = False) -> list[dict]:
        entries = list(self._console_log)
        if clear:
            self._console_log.clear()
        return entries


class SessionRegistry:
    # Cap on idle-close state stashes (#36): one small storage_state dict per never-returning
    # session id; bounded so a pathological client churning ids can't grow it without limit.
    _MAX_STASH = 64

    def __init__(self, config: Config):
        self._config = config
        self._sessions: dict[str, BrowserManager] = {}
        # session_id → storage_state stashed at idle-close, handed to the next manager (#36).
        self._stash: dict[str, dict] = {}

    def get(self, session_id: str) -> BrowserManager:
        if session_id not in self._sessions:
            mgr = BrowserManager(self._config, session_id)  # session_id → its own persistent profile (#43)
            mgr._pending_state = self._stash.pop(session_id, None)  # restore login if idle-closed
            self._sessions[session_id] = mgr
        return self._sessions[session_id]

    async def close(self, session_id: str):
        mgr = self._sessions.pop(session_id, None)
        if mgr:
            await mgr.close()

    def active(self) -> list[str]:
        return list(self._sessions.keys())

    def idle_seconds(self, session_id: str) -> float | None:
        mgr = self._sessions.get(session_id)
        return mgr.idle_seconds() if mgr else None

    async def close_idle(self, ttl: float) -> list[str]:
        """Close + drop sessions whose browser has been idle for at least ``ttl`` seconds (``ttl``
        <= 0 disables, returning []). Each closed session re-opens lazily on the next ``get`` — and
        its cookies/login are stashed first, so the reopen restores them instead of logging out (#36)."""
        if ttl <= 0:
            return []
        stale = [sid for sid, mgr in self._sessions.items() if mgr.is_idle(ttl)]
        for sid in stale:
            try:
                state = await self._sessions[sid].save_state()
                if state:
                    if len(self._stash) >= self._MAX_STASH:
                        self._stash.pop(next(iter(self._stash)))  # drop oldest, stay bounded
                    self._stash[sid] = state
            except Exception:
                pass  # a session with nothing worth saving just reopens fresh
            await self.close(sid)
        return stale

    async def close_all(self):
        for mgr in self._sessions.values():
            await mgr.close()
        self._sessions.clear()
