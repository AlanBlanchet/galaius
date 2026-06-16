import sys
import subprocess
import tempfile
from collections import deque
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from interact.config import LOG_MAXLEN, Config
from interact.state import InteractiveElement


class BrowserManager:
    def __init__(self, config: Config):
        self._config = config
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._element_map: dict[int, list[InteractiveElement]] = {}
        self._network_log: deque[dict] = deque(maxlen=LOG_MAXLEN)
        self._console_log: deque[dict] = deque(maxlen=LOG_MAXLEN)
        self._recording_dir: tempfile.TemporaryDirectory | None = None
        # An active device-emulation profile (set by emulate_device); None → the configured
        # default viewport at DPR 1. Folded into every new context via _context_kwargs.
        self._device_override: dict | None = None

    def set_element_map(self, tab: int, elements: list[InteractiveElement]):
        self._element_map[tab] = elements

    def get_element(self, index: int, tab: int = 0) -> InteractiveElement | None:
        for el in self._element_map.get(tab, []):
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
        await self._ensure_browser()
        await self._new_context()

    async def get_page(self, tab_index: int = 0) -> Page:
        await self.ensure_ready()
        pages = self._context.pages
        if tab_index < len(pages):
            return pages[tab_index]
        raise IndexError(f"Tab {tab_index} does not exist — {len(pages)} tab(s) open")

    async def new_tab(self, url: str | None = None) -> int:
        await self.ensure_ready()
        page = await self._context.new_page()
        self._attach_page_listeners(page)
        if url:
            await page.goto(url)
        return len(self._context.pages) - 1

    async def close_tab(self, tab_index: int):
        await self.ensure_ready()
        pages = self._context.pages
        if tab_index >= len(pages):
            raise IndexError(f"Tab {tab_index} not found")
        await pages[tab_index].close()

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
        if self._browser:
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
        if self._browser:
            return
        self._playwright = await async_playwright().start()
        launcher = getattr(self._playwright, self._config.browser_type)
        try:
            # Launch straight away — when the browser is already installed (the common case)
            # this skips the slow per-launch `playwright install` subprocess entirely.
            self._browser = await launcher.launch(
                headless=self._config.headless, slow_mo=self._config.slow_mo
            )
        except Exception:
            # First run / missing browser → install once, then retry.
            self._install_browser()
            self._browser = await launcher.launch(
                headless=self._config.headless, slow_mo=self._config.slow_mo
            )

    async def _new_context(self, storage_state: dict | None = None, record_video_dir: str | None = None):
        kwargs = self._context_kwargs(record_video_dir)
        if storage_state is not None:
            kwargs["storage_state"] = storage_state
        self._context = await self._browser.new_context(**kwargs)
        # Fail fast on a missing/non-actionable selector: Playwright's 30s default makes a bad
        # selector hang the agent for half a minute before erroring. config.wait_timeout (10s) is
        # the one knob; a dead selector then surfaces as an actionable nudge (see dispatch) in ~10s.
        from interact.runtime import config

        self._context.set_default_timeout(config.wait_timeout)
        await self._context.grant_permissions(["clipboard-read", "clipboard-write"])
        page = await self._context.new_page()
        self._attach_page_listeners(page)

    def _attach_page_listeners(self, page: Page):
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
    def __init__(self, config: Config):
        self._config = config
        self._sessions: dict[str, BrowserManager] = {}

    def get(self, session_id: str) -> BrowserManager:
        if session_id not in self._sessions:
            self._sessions[session_id] = BrowserManager(self._config)
        return self._sessions[session_id]

    async def close(self, session_id: str):
        mgr = self._sessions.pop(session_id, None)
        if mgr:
            await mgr.close()

    def active(self) -> list[str]:
        return list(self._sessions.keys())

    async def close_all(self):
        for mgr in self._sessions.values():
            await mgr.close()
        self._sessions.clear()
