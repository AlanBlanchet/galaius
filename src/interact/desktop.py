import asyncio
import ctypes
import ctypes.util
import io
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Self

from PIL import Image, ImageChops
from pydantic import BaseModel, PrivateAttr, computed_field

from interact.desktop_backend import _ffmpeg_grab_args, _VideoSession
from interact.geometry import Box as _GeoBox, BoxArray
from interact.parsing import Parse
from interact.state import Element, InteractiveElement

# Live record sessions for real-display (`:0`) window targets, keyed by wid — the DesktopWindow is
# re-resolved per tool call, so the session must outlive it here (the nested path keeps its own on
# the persistent backend instead). See start_video/stop_video (#61/#62).
_LOCAL_VIDEO_SESSIONS: dict[int, _VideoSession] = {}

_log = logging.getLogger("interact")
_MIN_AREA = 500
_FOCUS_DELAY = 0.05
_TYPE_DELAY_MS = 12
_DRAG_STEPS = 24  # Flutter needs a fine, slow pointer path to register a kinetic drag/scroll (#13)
_DRAG_STEP_DELAY = 0.015
_MOTION_FRACTION = 0.001
_MOTION_DELTA = 10
_IOU_OVERLAP_THRESHOLD = 0.3
_IOU_MERGE_MIN = 0.05
_IOU_MERGE_HIGH = 0.5
_MERGE_CENTER_DIST = 50
_MIN_DIM = 10
_MIN_DIM_BOTH = 15
_TITLEBAR_Y = 40
_LINE_RE = re.compile(
    r"(0x[0-9a-fA-F]+)\s+\"([^\"]+)\".*?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)"
)

_KEY_MAP = {
    "Enter": "Return",
    "ArrowDown": "Down",
    "ArrowUp": "Up",
    "ArrowLeft": "Left",
    "ArrowRight": "Right",
    "Backspace": "BackSpace",
    "Delete": "Delete",
    "Escape": "Escape",
    "Tab": "Tab",
    "Control": "ctrl",
    "Shift": "shift",
    "Alt": "alt",
    "Meta": "super",
}

_SCROLL_BUTTON = {"down": 5, "up": 4, "left": 6, "right": 7}

# --- X11 cursor detection via XFixes (ctypes, no pip deps) ---

_libx11_path = ctypes.util.find_library("X11")
_libxfixes_path = ctypes.util.find_library("Xfixes")
_libx11 = ctypes.CDLL(_libx11_path) if _libx11_path else None
_libxfixes = ctypes.CDLL(_libxfixes_path) if _libxfixes_path else None

if _libx11:
    _libx11.XOpenDisplay.restype = ctypes.c_void_p
    _libx11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    _libx11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    _libx11.XFree.argtypes = [ctypes.c_void_p]

_CURSOR_NAME_MAP: dict[str, str] = {}
for _names, _label in [
    (("text", "xterm", "ibeam"), "text"),
    (("pointer", "hand", "hand1", "hand2", "pointing_hand"), "pointer"),
    (("default", "left_ptr", "arrow"), "default"),
    (("grab", "fleur", "move", "all-scroll", "grabbing", "closedhand"), "grab"),
    (("crosshair", "cross"), "crosshair"),
    (("not-allowed", "forbidden", "x_cursor", "circle"), "not-allowed"),
    (("wait", "watch", "progress"), "wait"),
]:
    for _n in _names:
        _CURSOR_NAME_MAP[_n] = _label


class _XFixesCursorImage(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_short),
        ("y", ctypes.c_short),
        ("width", ctypes.c_ushort),
        ("height", ctypes.c_ushort),
        ("xhot", ctypes.c_ushort),
        ("yhot", ctypes.c_ushort),
        ("cursor_serial", ctypes.c_ulong),
        ("pixels", ctypes.POINTER(ctypes.c_ulong)),
        ("atom", ctypes.c_ulong),
        ("name", ctypes.c_char_p),
    ]


if _libxfixes:
    _libxfixes.XFixesGetCursorImage.restype = ctypes.POINTER(_XFixesCursorImage)
    _libxfixes.XFixesGetCursorImage.argtypes = [ctypes.c_void_p]


_CURSOR_LABELS: dict[str, str] = {
    "pointer": "clickable",
    "text": "text-input",
    "default": "normal",
    "grab": "draggable",
    "not-allowed": "disabled",
    "wait": "loading",
    "crosshair": "precision-select",
    "resize": "resizable",
}


class Cursor:
    """X11/XFixes cursor inspection."""

    @staticmethod
    def classify(name: str) -> str:
        low = name.lower()
        if low in _CURSOR_NAME_MAP:
            return _CURSOR_NAME_MAP[low]
        if "resize" in low or "size" in low:
            return "resize"
        return low

    @staticmethod
    def label(cursor_type: str) -> str:
        return _CURSOR_LABELS.get(cursor_type, cursor_type)

    @classmethod
    def current_type(cls) -> str:
        """Current X11 cursor type via XFixes."""
        try:
            if not _libx11 or not _libxfixes:
                return "unknown"

            display = _libx11.XOpenDisplay(None)
            if not display:
                return "unknown"

            try:
                cursor_ptr = _libxfixes.XFixesGetCursorImage(display)
                if not cursor_ptr:
                    return "unknown"

                try:
                    cursor = cursor_ptr.contents
                    if cursor.name:
                        return cls.classify(
                            cursor.name.decode("utf-8", errors="replace")
                        )
                    # Fallback: dimension heuristics
                    w, h = cursor.width, cursor.height
                    if h > 0 and w / h < 0.4:
                        return "text"
                    return "default"
                finally:
                    _libx11.XFree(cursor_ptr)
            finally:
                _libx11.XCloseDisplay(display)
        except Exception:
            return "unknown"


_coord_cache: dict = {}


class CoordTransform(BaseModel):
    """Affine coordinate transform: maps VLM/AT-SPI coords to screenshot to xdotool space.

    Composes: VLM resize scaling, crop offset translation, frame offset translation.
    All transformations are (scale, then translate).
    """

    scale_x: float = 1.0
    scale_y: float = 1.0
    crop_x: int = 0
    crop_y: int = 0
    shadow_left: int = 0
    shadow_right: int = 0
    shadow_top: int = 0
    shadow_bottom: int = 0
    decoration_top: int = 0

    @classmethod
    def from_xprop(cls, wid: int) -> Self:
        shadow_left = shadow_right = shadow_top = shadow_bottom = decoration_top = 0
        try:
            xprop = subprocess.check_output(
                ["xprop", "-id", str(wid)], text=True, timeout=5
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return cls()
        m = re.search(
            r"_GTK_FRAME_EXTENTS\(CARDINAL\)\s*=\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)",
            xprop,
        )
        if m:
            shadow_left = int(m.group(1))
            shadow_right = int(m.group(2))
            shadow_top = int(m.group(3))
            shadow_bottom = int(m.group(4))
        m = re.search(
            r"_MUTTER_FRAME_EXTENTS\(CARDINAL\)\s*=\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)",
            xprop,
        )
        if m:
            decoration_top = int(m.group(3))
        return cls(
            shadow_left=shadow_left,
            shadow_right=shadow_right,
            shadow_top=shadow_top,
            shadow_bottom=shadow_bottom,
            decoration_top=decoration_top,
        )

    @classmethod
    def store(cls, wid: int, offsets: Self):
        _coord_cache[wid] = offsets

    @classmethod
    def get(cls, wid: int) -> Self:
        return _coord_cache.get(wid, CoordTransform())

    @classmethod
    def has(cls, wid: int) -> bool:
        return wid in _coord_cache

    @classmethod
    def for_resize(
        cls, orig_w: int, orig_h: int, max_dim: int = 1280, min_dim: int = 768
    ) -> Self:
        mx = max(orig_w, orig_h)
        if mx > max_dim:
            scale = mx / max_dim
        elif mx < min_dim:
            scale = mx / min_dim
        else:
            return cls()
        return cls(scale_x=scale, scale_y=scale)

    def screenshot_to_xdotool(self, x: int, y: int) -> tuple[int, int]:
        return x + self.shadow_left, y + self.shadow_top

    def with_crop(self, crop_x: int, crop_y: int) -> Self:
        return self.model_copy(update={"crop_x": crop_x, "crop_y": crop_y})

    def resize_image(
        self, png_bytes: bytes, orig_w: int, orig_h: int
    ) -> tuple[bytes, int, int]:
        if self.scale_x == 1.0 and self.scale_y == 1.0:
            return png_bytes, orig_w, orig_h
        new_w = round(orig_w / self.scale_x)
        new_h = round(orig_h / self.scale_y)
        img = Image.open(io.BytesIO(png_bytes))
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        return buf.getvalue(), new_w, new_h


_SCREEN_WID = -1  # synthetic wid base for screen targets — a cache key, never a real X window


class CaptureError(RuntimeError):
    """A window/screen capture could not produce real pixels (e.g. an unreadable GPU surface).
    Surfaced to the agent as a clear, actionable error instead of a black image."""


def gpu_surface_error(name: str) -> "CaptureError":
    """The one diagnostic for a uniform-black grab — an X screen-grab (maim or ffmpeg x11grab)
    can't read a GPU-rendered surface. Names the cause + the fixes, shared by capture() and the
    record path so the agent gets the same actionable message wherever it hits this."""
    return CaptureError(
        f"Capture of {name!r} came back a single uniform colour — an X screen-grab can't read it. "
        "That's the signature of a GPU-rendered surface (Android emulator, game, hardware-"
        "accelerated video) that isn't in the X framebuffer. Fixes: run a compositing manager "
        "(e.g. picom) so the surface is redirected and grabbable, or capture the app's own "
        "framebuffer — for an Android emulator: `adb exec-out screencap -p` rather than a desktop grab."
    )


def _is_blank_png(data: bytes) -> bool:
    """True if the PNG is a single uniform colour (e.g. all black) — the signature of a failed
    window-id capture of a hardware-accelerated surface (so we can retry by geometry)."""
    try:
        lo, hi = Image.open(io.BytesIO(data)).convert("L").getextrema()
    except Exception:
        return False
    return lo == hi


class DesktopWindow(BaseModel):
    name: str
    wid: int
    w: int
    h: int
    x: int
    y: int

    # Set for a whole-screen / single-monitor target: capture via `maim` geometry instead of a
    # window id, and treat detected coords as screen-relative (no window to activate, no
    # decoration offset). "" = the whole virtual screen; "WxH+X+Y" = one monitor's region.
    screen_geometry: str | None = None

    # When bound (e.g. to a nested sandbox backend), input/capture route through the
    # DesktopBackend instead of the default real-display xdotool path. Left unset for the
    # ordinary local case, so that path is unchanged.
    _backend: object | None = PrivateAttr(default=None)

    @computed_field
    @property
    def area(self) -> int:
        return self.w * self.h

    @property
    def is_screen(self) -> bool:
        return self.screen_geometry is not None

    @classmethod
    def monitors(cls) -> list[dict]:
        """Connected monitors via ``xrandr --listmonitors`` — index, output name, and pixel
        geometry — so an agent can target a specific screen on a multi-monitor setup."""
        try:
            out = subprocess.check_output(
                ["xrandr", "--listmonitors"], text=True, timeout=5
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return []
        # `xrandr --listmonitors` line, e.g.:
        #   " 0: +*DP-1 2560/598x1440/336+0+0  DP-1"
        # The first token after the index carries flags (+*); the clean output name is the LAST
        # token. Capture: index, WxH+X+Y from the geometry token, and the trailing output name.
        mons: list[dict] = []
        for line in out.splitlines():
            m = re.match(
                r"\s*(\d+):\s+\S+\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)\s+(\S+)", line
            )
            if m:
                mons.append(
                    {
                        "index": int(m.group(1)),
                        "name": m.group(6),
                        "w": int(m.group(2)),
                        "h": int(m.group(3)),
                        "x": int(m.group(4)),
                        "y": int(m.group(5)),
                    }
                )
        return mons

    @classmethod
    def screen(cls, spec: str = "screen") -> "DesktopWindow | str":
        """Build a capture target for the whole virtual screen (``spec="screen"``) or a single
        monitor (``"screen:0"`` by index, or ``"screen:HDMI-1"`` by output name). Returns an
        error string listing the monitors if the requested one isn't found."""
        rest = spec.split(":", 1)[1].strip() if ":" in spec else ""
        mons = cls.monitors()
        if not rest:  # whole virtual screen = bounding box of every monitor; bare `maim` captures it
            w = max((mm["x"] + mm["w"] for mm in mons), default=0)
            h = max((mm["y"] + mm["h"] for mm in mons), default=0)
            return cls(name="screen", wid=_SCREEN_WID, x=0, y=0, w=w, h=h, screen_geometry="")
        mon = next((mm for mm in mons if rest.isdigit() and mm["index"] == int(rest)), None)
        if mon is None:
            mon = next((mm for mm in mons if mm["name"].lower() == rest.lower()), None)
        if mon is None:
            listing = ", ".join(f"{mm['index']}:{mm['name']}" for mm in mons) or "none detected"
            return f"No monitor '{rest}'. Available monitors: {listing}"
        return cls(
            name=f"screen:{mon['index']} ({mon['name']})",
            wid=_SCREEN_WID - 1 - mon["index"],  # distinct per monitor → no ref-cache collision
            x=mon["x"],
            y=mon["y"],
            w=mon["w"],
            h=mon["h"],
            screen_geometry=f"{mon['w']}x{mon['h']}+{mon['x']}+{mon['y']}",
        )

    @classmethod
    def find_in(cls, backend, title: str) -> Self | None:
        """Find a window by title on a bound backend's display (e.g. the nested sandbox)
        and return it wired to drive input/capture through that backend."""
        geometry = backend.window_geometry(title)
        if geometry is None:
            return None
        x, y, w, h = geometry
        wid = backend._window_id(title)
        win = cls(name=title, wid=int(wid) if wid else 0, x=x, y=y, w=w, h=h)
        win._backend = backend
        return win

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        """Map a window-content coordinate to its display's screen coordinate (for the
        backend path; the bound backend is WM-less, so there is no decoration offset)."""
        return self.x + x, self.y + y

    @classmethod
    def all(cls) -> list[Self]:
        try:
            tree = subprocess.check_output(
                ["xwininfo", "-root", "-tree"], text=True, timeout=5
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return []
        client_wids = cls._net_client_list()
        candidates = [
            cls(
                name=m.group(2),
                wid=int(m.group(1), 16),
                w=int(m.group(3)),
                h=int(m.group(4)),
                x=int(m.group(5)),
                y=int(m.group(6)),
            )
            for m in _LINE_RE.finditer(tree)
            if int(m.group(3)) * int(m.group(4)) >= _MIN_AREA
        ]
        # If we got a client list, filter to only real client windows
        if client_wids:
            candidates = [w for w in candidates if w.wid in client_wids]
        return candidates

    @classmethod
    def matching(cls, title: str, windows: list[Self] | None = None) -> list[Self]:
        """Windows whose title contains ``title`` (case-insensitive), most-likely-intended first:
        an exact title match leads, then larger windows. So a title that is *also* a substring of a
        longer one (``"aino"`` vs ``"aino - Visual Studio Code"``) still resolves to the exact window."""
        if windows is None:
            windows = cls.all()
        hint = title.strip().lower()
        matches = [w for w in windows if hint in w.name.lower()]
        return sorted(matches, key=lambda w: (w.name.lower() != hint, -w.area))

    @classmethod
    def find(cls, title: str, windows: list[Self] | None = None) -> Self | None:
        matches = cls.matching(title, windows)
        return matches[0] if matches else None

    @classmethod
    def listing(cls, windows: list[Self]) -> str:
        # Include the window id: when no title is unique (an app titled "aino" is a substring of
        # "aino - Visual Studio Code"), target="wid:<id>" is the only unambiguous selector (#5).
        return "\n".join(
            f"  {w.name} ({w.w}x{w.h}, wid:{w.wid})" for w in sorted(windows, key=lambda w: w.name)
        )

    def _raise_window(self) -> None:
        """Bring this window to the front and focus it (xdotool, best-effort) before capture or
        record. Without this a window the user moved or buried hands back occluded pixels — the
        bug that made a consumer abandon interact and raise windows by hand. No-op for screen
        targets (no window) and the nested backend (isolated, nothing can occlude)."""
        if self.is_screen or self._backend is not None:
            return
        try:
            subprocess.run(
                ["xdotool", "windowactivate", "--sync", str(self.wid)],
                check=False,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            pass  # raising is best-effort; capture still attempts

    def capture(self) -> bytes:
        if self._backend is not None:
            return self._backend.capture_window(self.name)
        if self.screen_geometry is not None:
            # whole virtual screen → bare `maim`; a single monitor → `maim -g WxH+X+Y`.
            cmd = ["maim", "-g", self.screen_geometry] if self.screen_geometry else ["maim"]
            img = subprocess.check_output(cmd, timeout=10)
        else:
            self._raise_window()  # a moved/buried window must come to the front first
            img = subprocess.check_output(["maim", "-i", str(self.wid)], timeout=10)
            if _is_blank_png(img):
                # window-id capture of a hardware-accelerated surface can come back blank; retry by
                # geometry (reads the framebuffer region) — recovers non-GPU cases.
                geom = self._geometry_now()
                if geom:
                    try:
                        img = subprocess.check_output(["maim", "-g", geom], timeout=10)
                    except subprocess.SubprocessError:
                        pass
        if _is_blank_png(img):
            # Still uniform → an X screen-grab genuinely can't read this surface. Don't hand back a
            # black image the model will misread as a broken UI; say what it is and how to capture it.
            raise gpu_surface_error(self.name)
        return img

    def _geometry_now(self) -> str | None:
        """Current on-screen geometry as ``WxH+X+Y`` (for region capture), via xdotool."""
        try:
            out = subprocess.check_output(
                ["xdotool", "getwindowgeometry", "--shell", str(self.wid)],
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        p = dict(ln.split("=", 1) for ln in out.strip().splitlines() if "=" in ln)
        try:
            return f"{p['WIDTH']}x{p['HEIGHT']}+{max(0, int(p['X']))}+{max(0, int(p['Y']))}"
        except (KeyError, ValueError):
            return None

    def _grab_region(self) -> tuple[int, int, int, int]:
        """Pixel region ``(w, h, x, y)`` to grab for video via ffmpeg x11grab. A screen/monitor
        target uses its known geometry — it has no window, so querying xdotool for the synthetic
        wid would fail (#3). A window target reads its current on-screen geometry from xdotool, so
        a window the user moved still records correctly."""
        if self.is_screen:
            return self.w, self.h, max(0, self.x), max(0, self.y)
        out = subprocess.check_output(
            ["xdotool", "getwindowgeometry", "--shell", str(self.wid)], text=True, timeout=5
        )
        p = dict(ln.split("=", 1) for ln in out.strip().splitlines() if "=" in ln)
        return int(p["WIDTH"]), int(p["HEIGHT"]), max(0, int(p["X"])), max(0, int(p["Y"]))

    def capture_video(self, duration: float = 3.0, fps: int = 10) -> bytes:
        if self._backend is not None:
            # A sandbox window lives on the backend's display (:N), not :0 — record there, or every
            # frame is a black grab of the wrong display (#18). Mirrors capture()'s backend dispatch.
            return self._backend.capture_video(self.name, duration, fps)
        self._raise_window()  # record the target window's own pixels, not whatever buried it
        grab_w, grab_h, grab_x, grab_y = self._grab_region()

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            output_path = Path(f.name)

        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "x11grab",
                    "-video_size",
                    f"{grab_w}x{grab_h}",
                    "-framerate",
                    str(fps),
                    "-i",
                    f":0+{grab_x},{grab_y}",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-t",
                    str(duration),
                    "-pix_fmt",
                    "yuv420p",
                    "-vf",
                    "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                    str(output_path),
                ],
                check=True,
                capture_output=True,
                timeout=duration + 10,
            )
            return output_path.read_bytes()
        finally:
            output_path.unlink(missing_ok=True)

    def start_video(self, fps: int = 10) -> None:
        """Begin a non-blocking recording of this window — returns at once so the agent can drive
        actions during capture, then :meth:`stop_video` to export (#61/#62). Routes to the bound
        backend for a nested window, else records the real-display (``:0``) window region."""
        if self._backend is not None:
            self._backend.start_video(self.name, fps)
            return
        if self.wid in _LOCAL_VIDEO_SESSIONS:
            return  # idempotent — a second start while one is live is a no-op
        self._raise_window()
        grab_w, grab_h, grab_x, grab_y = self._grab_region()
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            out = f.name
        _LOCAL_VIDEO_SESSIONS[self.wid] = _VideoSession(
            _ffmpeg_grab_args(":0", grab_x, grab_y, grab_w, grab_h, fps, out, duration=None), out
        )

    def stop_video(self) -> bytes | None:
        """Stop the session started by :meth:`start_video` and return its mp4 bytes, or None if none
        is open for this target."""
        if self._backend is not None:
            return self._backend.stop_video(self.name)
        session = _LOCAL_VIDEO_SESSIONS.pop(self.wid, None)
        return session.stop() if session else None

    def _to_xdotool(self, x: int, y: int) -> tuple[int, int]:
        return CoordTransform.get(self.wid).screenshot_to_xdotool(x, y)

    def _input_xy(self, x: int, y: int) -> tuple[int, int]:
        """Capture-space (x, y) → absolute screen coords for xdotool. A screen/monitor target's
        detected coords are relative to the captured region, so map by the region origin; a
        window target maps through its stored CoordTransform (decoration/shadow offsets)."""
        if self.is_screen:
            return self.x + x, self.y + y
        return self._to_xdotool(x, y)

    async def _mousemove(self, x: int, y: int):
        """Position the pointer. A window target moves window-relative (``--window <wid>``); a
        screen/monitor target has no window, so move to the absolute screen coordinate — using
        ``--window`` with its synthetic wid would fail."""
        if self.is_screen:
            await self._run("xdotool", "mousemove", str(x), str(y))
        else:
            await self._xdo(self.wid, "mousemove", str(x), str(y))

    async def _clickbtn(self, button: str):
        """Press a mouse button — window-scoped for a window target, absolute for a screen
        target (no window to scope to)."""
        if self.is_screen:
            await self._run("xdotool", "click", button)
        else:
            await self._xdo(self.wid, "click", button)

    async def _activate(self):
        """Raise the target window before input — a no-op for a screen target (no window to
        raise; the local pointer is already absolute over the whole root)."""
        if not self.is_screen:
            await self._run("xdotool", "windowactivate", "--sync", str(self.wid))

    async def _focus(self):
        if self.is_screen:
            return
        await self._run("xdotool", "windowactivate", "--sync", str(self.wid))
        await self._run("xdotool", "windowfocus", "--sync", str(self.wid))

    _BUTTON_NAMES = {1: "left", 2: "middle", 3: "right"}

    async def _backend_focus(self) -> None:
        """Focus the target window on a bound (sandbox) backend before keyboard input — WM-less,
        nothing holds focus by default so keys would otherwise go nowhere. Pointer events route by
        position, so clicks don't need this. Best-effort; no-op if the backend can't focus.

        Focus the EXACT window this DesktopWindow resolved to (``self.wid``) — the same window
        click/scroll act on — not a re-search by title: a title can match a hidden helper window
        (Chrome spawns a 10x10 "clipboard" window), so re-resolving could focus the wrong one and
        the keystrokes land nowhere ("clicks work, typing doesn't", #25)."""
        backend = self._backend
        focus_wid = getattr(backend, "focus_wid", None)
        if focus_wid is not None and self.wid:
            await asyncio.to_thread(focus_wid, self.wid)
        else:
            focus = getattr(backend, "focus", None)
            if focus is not None:
                await asyncio.to_thread(focus, self.name)
        await asyncio.sleep(_FOCUS_DELAY)  # let focus settle before the XTEST keystrokes

    async def click(self, x: int, y: int, button: int = 1):
        _log.debug("desktop_click wid=%s x=%s y=%s button=%s", self.wid, x, y, button)
        if self._backend is not None:
            sx, sy = self.to_screen(x, y)
            await asyncio.to_thread(self._backend.click, sx, sy, self._BUTTON_NAMES.get(button, "left"))
            return
        xdo_x, xdo_y = self._input_xy(x, y)
        await self._activate()
        await asyncio.sleep(_FOCUS_DELAY)
        await self._mousemove(xdo_x, xdo_y)
        await asyncio.sleep(_FOCUS_DELAY)
        await self._run("xdotool", "click", str(button))

    async def type_text(self, text: str):
        if self._backend is not None:
            await self._backend_focus()
            await asyncio.to_thread(self._backend.type_text, text)
            return
        await self._focus()
        await asyncio.sleep(_FOCUS_DELAY)
        await self._run(
            "xdotool",
            "type",
            "--clearmodifiers",
            "--delay",
            str(_TYPE_DELAY_MS),
            "--",
            text,
        )

    async def press_key(self, key: str):
        if self._backend is not None:
            await self._backend_focus()
            await asyncio.to_thread(self._backend.key, self.map_key(key))
            return
        await self._focus()
        await asyncio.sleep(_FOCUS_DELAY)
        await self._run("xdotool", "key", "--clearmodifiers", "--", self.map_key(key))

    async def scroll(self, x: int, y: int, direction: str, amount: int = 3):
        if self._backend is not None:
            await self._backend_focus()  # focus first so the toolkit accepts the wheel (#12/#13)
            sx, sy = self.to_screen(x, y)
            # Pick the axis, not just the sign: a left/right scroll must reach the backend as a
            # HORIZONTAL wheel (X buttons 6/7), not collapse into a vertical button — that silent
            # collapse left a Flutter horizontal carousel unable to advance (#54).
            horizontal = direction in ("left", "right")
            positive = direction in ("up", "right")  # up / right are the +clicks directions
            clicks = amount if positive else -amount

            def _do():
                self._backend.move(sx, sy)
                self._backend.scroll(clicks, horizontal=horizontal)

            await asyncio.to_thread(_do)
            return
        xdo_x, xdo_y = self._input_xy(x, y)
        # A wheel event is delivered to the window under the pointer that also holds focus — so
        # raise + focus the window first (clicks worked without this only because they self-focus).
        # Without it a Flutter/GTK surface silently drops the synthetic wheel (#12, #13).
        await self._activate()
        await asyncio.sleep(_FOCUS_DELAY)
        await self._mousemove(xdo_x, xdo_y)
        await self._focus()
        await asyncio.sleep(_FOCUS_DELAY)
        button = str(_SCROLL_BUTTON[direction])
        for _ in range(amount):
            await self._clickbtn(button)

    async def drag(self, fx: int, fy: int, tx: int, ty: int, steps: int = _DRAG_STEPS):
        steps = max(1, steps)
        if self._backend is not None:
            await self._backend_focus()  # focus first so the toolkit accepts the drag (#12/#13)
            sfx, sfy = self.to_screen(fx, fy)
            stx, sty = self.to_screen(tx, ty)
            await asyncio.to_thread(self._backend.drag, sfx, sfy, stx, sty, steps)
            return
        xfx, xfy = self._input_xy(fx, fy)
        xtx, xty = self._input_xy(tx, ty)
        await self._activate()
        await asyncio.sleep(_FOCUS_DELAY)
        await self._mousemove(xfx, xfy)
        await asyncio.sleep(_FOCUS_DELAY)
        await self._run("xdotool", "mousedown", "1")
        # float division (not //) so mid-points aren't quantized to the same pixel, plus a small
        # per-step delay — Flutter needs a continuous, time-spread pointer path to recognise a drag
        # and fling, not a couple of teleports (#12, #13).
        for i in range(1, steps + 1):
            ix = round(xfx + (xtx - xfx) * i / steps)
            iy = round(xfy + (xty - xfy) * i / steps)
            await self._mousemove(ix, iy)
            await asyncio.sleep(_DRAG_STEP_DELAY)
        await self._run("xdotool", "mouseup", "1")

    async def hover(self, x: int, y: int):
        if self._backend is not None:
            sx, sy = self.to_screen(x, y)
            await asyncio.to_thread(self._backend.move, sx, sy)
            return
        xdo_x, xdo_y = self._input_xy(x, y)
        await self._activate()
        await asyncio.sleep(_FOCUS_DELAY)
        await self._mousemove(xdo_x, xdo_y)

    @staticmethod
    def _net_client_list() -> set[int]:
        """Return window IDs from _NET_CLIENT_LIST (real app windows only, excludes compositor frames)."""
        try:
            out = subprocess.check_output(
                ["xprop", "-root", "_NET_CLIENT_LIST"], text=True, timeout=5
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return set()
        # Format: _NET_CLIENT_LIST(WINDOW): window id # 0x1600007, 0x2400004, ...
        if "#" not in out:
            return set()
        hex_ids = out.split("#", 1)[1].strip().split(",")
        return {int(h.strip(), 16) for h in hex_ids if h.strip()}

    @staticmethod
    async def _run(*args: str):
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"{args[0]} failed (rc={proc.returncode}): {stderr.decode().strip()}"
            )

    @classmethod
    async def _xdo(cls, wid: int, subcmd: str, *args: str):
        await cls._run("xdotool", subcmd, "--window", str(wid), *args)

    @staticmethod
    async def active_id() -> str | None:
        proc = await asyncio.create_subprocess_exec(
            "xdotool",
            "getactivewindow",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            return stdout.decode().strip()
        return None

    @staticmethod
    def map_key(key: str) -> str:
        parts = key.split("+")
        return "+".join(_KEY_MAP.get(p, p) for p in parts)


class Box(Element):
    @computed_field
    @property
    def center_x(self) -> int:
        return self.x + self.w // 2

    @computed_field
    @property
    def center_y(self) -> int:
        return self.y + self.h // 2

    @computed_field
    @property
    def x2(self) -> int:
        return self.x + self.w

    @computed_field
    @property
    def y2(self) -> int:
        return self.y + self.h

    def as_xyxy(self) -> tuple[float, float, float, float]:
        return (
            float(self.x),
            float(self.y),
            float(self.x + self.w),
            float(self.y + self.h),
        )

    def iou(self, other: Self) -> float:
        return _GeoBox.from_xyxy(self.as_xyxy()).iou(_GeoBox.from_xyxy(other.as_xyxy()))

    def clamp(self, img_w: int, img_h: int) -> Self | None:
        x2 = min(self.x + self.w, img_w)
        y2 = min(self.y + self.h, img_h)
        x, y = max(0, self.x), max(0, self.y)
        w, h = x2 - x, y2 - y
        if w <= 0 or h <= 0:
            return None
        return self.model_copy(update={"x": x, "y": y, "w": w, "h": h})

    def scale(self, sx: float, sy: float) -> Self:
        return self.model_copy(
            update={
                "x": round(self.x * sx),
                "y": round(self.y * sy),
                "w": round(self.w * sx),
                "h": round(self.h * sy),
            }
        )

    def translate(self, dx: int, dy: int) -> Self:
        return self.model_copy(update={"x": self.x + dx, "y": self.y + dy})

    def transform(self, coord: CoordTransform) -> Self:
        return self.scale(coord.scale_x, coord.scale_y).translate(
            coord.crop_x, coord.crop_y
        )


_element_cache: dict[int, list] = {}
_page_sig: dict[int, str] = {}  # last page signature (screenshot content hash) per wid → clear refs on change


class DesktopElement(Box):
    @classmethod
    def store(cls, wid: int, elements: list[Self]):
        _element_cache[wid] = elements

    @classmethod
    def merge_into(cls, wid: int, elements: list[Self], signature: str) -> list[Self]:
        """Accumulate detections for a window across detect calls, keyed by a page
        ``signature`` (a content fingerprint of the screenshot — NOT the title, which is
        constant in single-window apps). Same screen → union with the existing refs (a
        second/targeted detect *adds* to what we already found). Screen changed → drop the
        now-stale refs first, since those elements are gone. Returns the full current set
        (re-indexed), which becomes the live ref table for this window.
        """
        if _page_sig.get(wid) != signature:
            _element_cache[wid] = []
            _page_sig[wid] = signature
        merged = cls.merge_keeping(_element_cache.get(wid, []), elements)
        _element_cache[wid] = merged
        return merged

    @classmethod
    def get_by_index(cls, wid: int, index: int) -> Self | None:
        for el in _element_cache.get(wid, []):
            if el.index == index:
                return el
        return None

    @classmethod
    def cached(cls, wid: int) -> list[Self] | None:
        return _element_cache.get(wid) or None

    @classmethod
    def cached_for(cls, wid: int, signature: str) -> list[Self] | None:
        """Cached refs ONLY if they were detected on the currently-displayed frame (its content
        ``signature`` matches the one stored when the refs were detected). After a navigation the
        live frame's signature differs, so the prior screen's refs are NOT surfaced — preventing a
        screenshot from listing refs for a screen that's no longer shown, and clicks landing on gone
        targets (#19)."""
        if _page_sig.get(wid) != signature:
            return None
        return _element_cache.get(wid) or None

    @staticmethod
    def ref_to_index(ref: str) -> int:
        return int(ref.removeprefix("e"))

    @classmethod
    def to_interactive(cls, elements: list[Self]) -> list[InteractiveElement]:
        return [
            InteractiveElement(
                index=e.index,
                role=e.role,
                name=e.name,
                x=e.x,
                y=e.y,
                w=e.w,
                h=e.h,
            )
            for e in elements
        ]

    @classmethod
    def format_list(cls, elements: list[Self]) -> str:
        # LLM-facing output: emit ref (index) + role/name only. Pixel coords
        # are an implementation detail of the click/hover dispatch and must
        # NOT leak into model context — agents reference elements by index.
        return "\n".join(f"  [{el.index}] {el.role}: {el.name!r}" for el in elements)

    @staticmethod
    def infer_role(label: str) -> str:
        for role, pattern in _ROLE_PATTERNS:
            if pattern.search(label):
                return role
        return "element"

    @classmethod
    def from_vlm_dict(cls, entry: dict, index: int) -> Self:
        return cls(
            index=index,
            x=int(entry["x"]),
            y=int(entry["y"]),
            w=int(entry["w"]),
            h=int(entry["h"]),
            role=str(entry.get("role", "element")),
            name=str(entry.get("name", "")),
        )

    @classmethod
    def parse_vlm(cls, response: str) -> list[Self] | None:
        raw = Parse.extract_json_array(response)
        if raw is None:
            raw = []
            for m in re.finditer(r"\{[^{}]+\}", response):
                try:
                    raw.append(json.loads(m.group()))
                except json.JSONDecodeError:
                    continue
        if not raw:
            return None
        elements = []
        for i, entry in enumerate(raw):
            try:
                elements.append(cls.from_vlm_dict(entry, i + 1))
            except (KeyError, ValueError, TypeError):
                continue
        return elements or None

    @classmethod
    def fuse(cls, vlm_els: list[Self], atspi_els: list[Self]) -> list[Self]:
        matched_atspi: set[int] = set()
        result = []
        if vlm_els and atspi_els:
            iou_mat = BoxArray.from_boxes(vlm_els).iou_matrix(
                BoxArray.from_boxes(atspi_els)
            )
        else:
            iou_mat = None
        for i, vel in enumerate(vlm_els):
            best_iou, best_idx, best_ael = 0.0, -1, None
            if iou_mat is not None:
                row = iou_mat[i]
                j = int(row.argmax())
                score = float(row[j])
                if score > best_iou:
                    best_iou, best_idx, best_ael = score, j, atspi_els[j]
            if best_ael and best_iou > _IOU_OVERLAP_THRESHOLD:
                matched_atspi.add(best_idx)
                result.append(
                    cls(
                        index=len(result) + 1,
                        x=best_ael.x,
                        y=best_ael.y,
                        w=best_ael.w,
                        h=best_ael.h,
                        role=vel.role,
                        name=vel.name or best_ael.name,
                    )
                )
            else:
                result.append(vel.model_copy(update={"index": len(result) + 1}))
        for j, ael in enumerate(atspi_els):
            if j not in matched_atspi:
                result.append(ael.model_copy(update={"index": len(result) + 1}))
        return result

    @classmethod
    def filter_junk(
        cls, elements: list[Self], titlebar_y: int = _TITLEBAR_Y
    ) -> list[Self]:
        filtered: list[Self] = []
        for el in elements:
            name = el.name.strip()
            if _JUNK_NAME_RE.match(name):
                continue
            if min(el.w, el.h) < _MIN_DIM:
                continue
            if el.w < _MIN_DIM_BOTH and el.h < _MIN_DIM_BOTH:
                continue
            if el.y < titlebar_y and name in _WM_BUTTON_NAMES:
                continue
            if _NUMERIC_NAME_RE.match(name):
                continue
            if len(name) == 1:
                continue
            if name in ("", "button", "element", el.role):
                el.name = f"unnamed at ({el.x},{el.y})"
            if el.role == "element":
                el.role = cls.infer_role(el.name)
            filtered.append(el)
        for i, el in enumerate(filtered):
            el.index = i + 1
        return filtered

    @classmethod
    def merge_keeping(cls, existing: list[Self], extra: list[Self]) -> list[Self]:
        """NMS-style union: append boxes from ``extra`` that don't overlap (IoU >
        threshold) anything already kept, then re-index. Used to fold in additional
        detection passes — region refinement and targeted re-prompts — without
        duplicating boxes the first pass already found.
        """
        merged = list(existing)
        for element in extra:
            if not any(element.iou(kept) > _IOU_OVERLAP_THRESHOLD for kept in merged):
                merged.append(element)
        for i, element in enumerate(merged):
            element.index = i + 1
        return merged

    @classmethod
    def merge_fragments(cls, elements: list[Self]) -> list[Self]:
        merged = list(elements)
        changed = True
        while changed:
            changed = False
            result: list[Self] = []
            used: set[int] = set()
            for i, a in enumerate(merged):
                if i in used:
                    continue
                best = a
                for j, b in enumerate(merged):
                    if j <= i or j in used:
                        continue
                    na, nb = a.name.lower().strip(), b.name.lower().strip()
                    names_related = na and nb and (na in nb or nb in na)
                    names_exact = na and na == nb
                    overlap = a.iou(b)
                    center_dist = abs(a.center_x - b.center_x) + abs(
                        a.center_y - b.center_y
                    )
                    should_merge = (
                        (overlap > _IOU_MERGE_MIN and names_related)
                        or (names_exact and center_dist < _MERGE_CENTER_DIST)
                        or overlap > _IOU_MERGE_HIGH
                    )
                    if should_merge:
                        if b.w * b.h > best.w * best.h:
                            best = b
                        used.add(j)
                        changed = True
                result.append(best)
                if best is not a:
                    used.add(i)
            merged = result
        for i, el in enumerate(merged):
            el.index = i + 1
        return merged


_ROLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "tab",
        re.compile(
            r"\b(problems|output|debug console|terminal|ports|gitlens|explorer|extensions|source control|testing)\b",
            re.IGNORECASE,
        ),
    ),
    ("menu", re.compile(r"\b(file|edit|selection|view|go|run|help)\b", re.IGNORECASE)),
    (
        "button",
        re.compile(
            r"\b(button|btn|save|open|close|cancel|ok|apply|submit|send|undo|redo|refresh|collapse|new|delete|remove|copy|paste|cut)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "input",
        re.compile(
            r"\b(search|filter|find|input|describe|type|enter)\b|text\s*(box|field|input|area)",
            re.IGNORECASE,
        ),
    ),
    ("link", re.compile(r"\b(link|href|url)\b", re.IGNORECASE)),
    (
        "dropdown",
        re.compile(
            r"\b(select|choose|pick|dropdown|combobox|combo\s*box|branch|model)\b",
            re.IGNORECASE,
        ),
    ),
    ("toggle", re.compile(r"\b(toggle|switch|checkbox|check\s*box)\b", re.IGNORECASE)),
    (
        "icon-button",
        re.compile(
            r"\b(notifications?|bell|settings?|gear|account|profile|volume|mute)\b",
            re.IGNORECASE,
        ),
    ),
]


_WM_BUTTON_NAMES = frozenset(
    {"Minimise", "Minimize", "Maximise", "Maximize", "Close", "Restore"}
)
_JUNK_NAME_RE = re.compile(
    r"^(Ctrl|Alt|Shift|Cmd|Meta|Tab|Enter|Esc|Space|Backspace|Delete|Home|End|PageUp|PageDown|F\d+|[A-Z])$"
)
_NUMERIC_NAME_RE = re.compile(r"^[+-]?\d+$")


class Motion:
    """Video motion detection helpers."""

    @staticmethod
    def is_blank(video_bytes: bytes) -> bool:
        """True if the recording's first frame is a single uniform colour — a GPU surface that
        ffmpeg x11grab couldn't read (distinct from a static-but-real frame)."""
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                vp = Path(tmpdir) / "in.mp4"
                vp.write_bytes(video_bytes)
                out = Path(tmpdir) / "f.png"
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(vp), "-vframes", "1", str(out)],
                    check=True,
                    capture_output=True,
                    timeout=15,
                )
                if not out.exists():
                    return False
                lo, hi = Image.open(out).convert("L").getextrema()
                return lo == hi
        except Exception:
            return False

    @staticmethod
    def detect(
        video_bytes: bytes,
        pixel_delta: int = _MOTION_DELTA,
        changed_fraction: float = _MOTION_FRACTION,
    ) -> bool:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                video_path = Path(tmpdir) / "input.mp4"
                video_path.write_bytes(video_bytes)
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(video_path),
                        "-vf",
                        "select='eq(n\\,0)+eq(n\\,5)+eq(n\\,10)+eq(n\\,15)+eq(n\\,20)+eq(n\\,25)'",
                        "-vsync",
                        "vfr",
                        str(Path(tmpdir) / "frame_%02d.png"),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=15,
                )
                frames = sorted(Path(tmpdir).glob("frame_*.png"))
                if len(frames) < 2:
                    return False
                images = [Image.open(f).convert("L") for f in frames]
                for a, b in zip(images, images[1:]):
                    diff = ImageChops.difference(a, b)
                    total_pixels = a.size[0] * a.size[1]
                    changed = sum(
                        count
                        for value, count in enumerate(diff.histogram())
                        if value > pixel_delta
                    )
                    if changed > total_pixels * changed_fraction:
                        return True
                return False
        except Exception:
            return True
