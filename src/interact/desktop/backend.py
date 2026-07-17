"""The desktop backends: the :class:`DesktopBackend` ABC and its real-session (Local) and
cross-platform (Portable) implementations, plus OS-capability probes and X11 size helpers.
The uinput/input primitives live in :mod:`interact.desktop.input`, video capture in
:mod:`interact.desktop.video`, and the nested sandbox in :mod:`interact.desktop.nested`."""


import io
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod

from interact.desktop.input import ABS_MAX, UinputPointer, _BUTTONS, _parse_chord, screen_to_abs


def _rects_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """True if axis-aligned rects ``(x, y, w, h)`` intersect — used to composite a popup into the
    window it overlaps."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


class DesktopUnsupportedError(RuntimeError):
    """Raised when native desktop automation is requested on an OS that has no backend yet
    (anything but Linux). The message is actionable and points at the browser tools, which DO
    work everywhere — so callers can surface it verbatim to the agent."""


def desktop_supported() -> bool:
    """True when this OS has a working native-desktop backend. interact drives native windows
    through Linux X11 (``/dev/uinput`` input + maim/Xephyr capture); macOS and Windows have no
    backend yet. Browser automation (Playwright) works on every platform regardless — this gates
    only the desktop tools (launch_app, target=window/screen/nested)."""
    return sys.platform.startswith("linux")


def desktop_unsupported_message() -> str:
    """The single actionable error shown when a desktop tool is used off Linux — names the OS and
    steers to the browser tools (the working path) and the tracking issue, instead of leaking a
    cryptic evdev/maim failure from deep in a backend."""
    return (
        f"Desktop automation isn't supported on {platform.system() or sys.platform} yet — "
        "interact drives native windows through Linux X11 (uinput input + maim/Xephyr capture). "
        "Browser automation works fully on this OS: use navigate / run_actions / screenshot with "
        "the default browser target (omit `target`). Track native macOS/Windows desktop support: "
        "https://github.com/AlanBlanchet/interact/issues/24"
    )


def _tail_file(path: str | None, limit: int) -> str:
    """Last ``limit`` bytes of a file, decoded — for surfacing a dead X server's / crashed app's own
    output in an error message. Empty string on any problem (best-effort diagnostics)."""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-limit, os.SEEK_END)
            except OSError:
                f.seek(0)
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _frac_dark(gray, cutoff: int = 8) -> float:
    """Fraction of (near-)true-black pixels in an 8-bit grayscale PIL image, via its histogram
    (C-fast, no Python per-pixel loop). The cutoff is deliberately low: an unrendered GL buffer is
    *exactly* black (0,0,0), whereas a real dark theme (#1e1e1e ≈ 30) sits well above it — so the
    repaint heuristic fires on the unrendered case without flagging a legitimately dark UI."""
    hist = gray.histogram()
    total = sum(hist) or 1
    return sum(hist[:cutoff]) / total


def _gl_unrendered(png: bytes, *, strip_fracs: tuple[float, ...] = (0.08, 0.12, 0.18),
                   hard: float = 0.9, body_max: float = 0.5) -> bool:
    """True when a nested GL-window capture looks like it never painted: the whole frame is
    near-black, or a black bottom strip sits over a rendered body — a blurred bottom nav
    (BottomNavigationBar / convex_bottom_bar ConvexAppBar) that software GL left black (#7/#8,
    #14-#20). Both clear after a repaint nudge. The black bar's height varies by toolkit, so scan a
    band of candidate strip fractions, not one fixed 12%. A genuinely dark theme has a dark body
    too, so the strip-only case demands a much lighter body — otherwise every capture of a dark UI
    would needlessly nudge (and reset its scroll)."""
    try:
        from PIL import Image

        gray = Image.open(io.BytesIO(png)).convert("L")
    except Exception:
        return False
    w, h = gray.size
    if not w or not h:
        return False
    if _frac_dark(gray) >= hard:
        return True
    for sf in strip_fracs:
        cut = int(h * (1 - sf))
        if cut <= 0 or cut >= h:
            continue
        strip = _frac_dark(gray.crop((0, cut, w, h)))
        body = _frac_dark(gray.crop((0, 0, w, cut)))
        if strip >= hard and body < body_max:
            return True
    return False




def _x11_screen_size(env: dict | None = None) -> tuple[int, int]:
    """Pixel size of an X display via xdotool (the display in ``env['DISPLAY']``).

    Note: on a multi-monitor display this returns only the *primary* monitor — use
    :func:`_x11_root_size` for the full extent a uinput pointer maps onto.
    """
    out = subprocess.run(
        ["xdotool", "getdisplaygeometry"], capture_output=True, text=True, env=env, check=True
    ).stdout.split()
    return int(out[0]), int(out[1])


def _x11_root_size(env: dict | None = None) -> tuple[int, int]:
    """Full X root extent across all monitors — the space a ``INPUT_PROP_DIRECT`` uinput
    device maps its absolute range onto. ``getdisplaygeometry`` gives only the primary
    monitor, so on a multi-head setup an absolute pointer would be mis-scaled in X. Reads
    ``xwininfo -root``; falls back to the primary size if xwininfo is unavailable."""
    try:
        out = subprocess.run(
            ["xwininfo", "-root"], capture_output=True, text=True, env=env, check=True
        ).stdout
        dims: dict[str, int] = {}
        for line in out.splitlines():
            stripped = line.strip()
            for key in ("Width:", "Height:"):
                if stripped.startswith(key):
                    dims[key] = int(stripped.split()[1])
        if "Width:" in dims and "Height:" in dims:
            return dims["Width:"], dims["Height:"]
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        pass
    return _x11_screen_size(env)




class DesktopBackend(ABC):
    """One desktop the agent drives: capture a frame, inject pointer/keyboard.

    Two interchangeable implementations, selected by ``config.desktop_target``:

    * :class:`LocalBackend` — the user's **real** session. Input via the system-wide
      :class:`UinputPointer` (X11 + Wayland), capture via ``maim``.
    * :class:`NestedBackend` — an **isolated** ``Xephyr`` display the agent owns. Input
      and capture are scoped to that display (``xdotool``/``maim`` with ``DISPLAY=:N``),
      so a test — or a VM-like sandbox — never touches the user's real windows or cursor.

    Coordinates are pixels in the *target's* screen space; convert other spaces in via
    :class:`interact.frames.Frame`. ``click``/``drag``/``drag_circle`` are defined here
    on top of the ``move``/``mouse_down``/``mouse_up`` primitives each backend supplies.
    """

    @abstractmethod
    def capture(self) -> bytes:
        """PNG bytes of the whole target screen."""

    @abstractmethod
    def move(self, x: float, y: float) -> None: ...

    @abstractmethod
    def mouse_down(self, button: str = "left") -> None: ...

    @abstractmethod
    def mouse_up(self, button: str = "left") -> None: ...

    def click(self, x: float, y: float, button: str = "left") -> None:
        self.move(x, y)
        self.mouse_down(button)
        time.sleep(0.02)
        self.mouse_up(button)

    def drag(self, fx: float, fy: float, tx: float, ty: float, steps: int = 20) -> None:
        self.move(fx, fy)
        self.mouse_down()
        for i in range(1, steps + 1):
            self.move(fx + (tx - fx) * i / steps, fy + (ty - fy) * i / steps)
            time.sleep(0.01)
        self.mouse_up()

    def type_text(self, text: str) -> None:
        """Type a literal string into whatever currently has focus."""
        raise NotImplementedError(f"{type(self).__name__} cannot type text")

    def scroll(self, clicks: int, horizontal: bool = False) -> None:
        """Scroll the wheel along one axis. Vertical (default): positive up, negative down.
        Horizontal (``horizontal=True``): positive right, negative left."""
        raise NotImplementedError(f"{type(self).__name__} cannot scroll")

    def key(self, name: str) -> None:
        """Press a named key/chord (backend-specific name syntax)."""
        raise NotImplementedError(f"{type(self).__name__} cannot press keys")

    def capture_window(self, name: str) -> bytes:
        """PNG of one window by title — even if it's backgrounded/occluded.

        Defaults to a full-screen grab; backends that can target a single window
        (so a window behind others still captures correctly) override this.
        """
        return self.capture()

    def start_video(self, name: str, fps: int) -> None:
        """Begin a non-blocking recording of one window — returns at once so the agent can drive
        actions during the capture, then call :meth:`stop_video` to export (#61/#62)."""
        raise NotImplementedError(f"{type(self).__name__} cannot record video sessions")

    def stop_video(self, name: str) -> bytes | None:
        """Stop the recording started by :meth:`start_video` and return its mp4 bytes, or None if no
        session was open for ``name``."""
        raise NotImplementedError(f"{type(self).__name__} cannot record video sessions")

    def is_recording(self, name: str) -> bool:
        return False

    def drag_circle(self, cx: float, cy: float, radius: float, steps: int = 24) -> None:
        """Press at ``(cx, cy)`` and orbit a circle of ``radius`` before releasing —
        e.g. grab a window's title bar and move it in a circle. Returns to the press
        point so a dragged window ends where it started (a closed loop)."""
        self.move(cx, cy)
        self.mouse_down()
        for i in range(steps + 1):
            angle = 2 * math.pi * i / steps
            self.move(cx + radius * math.cos(angle), cy + radius * math.sin(angle))
            time.sleep(0.01)
        self.move(cx, cy)
        self.mouse_up()

    def close(self) -> None:  # noqa: B027 — optional teardown
        pass

    def __enter__(self) -> "DesktopBackend":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class LocalBackend(DesktopBackend):
    """The real session: :class:`UinputPointer` input + ``maim`` capture."""

    def __init__(self, screen_w: int | None = None, screen_h: int | None = None):
        if screen_w is None or screen_h is None:
            # The whole X root (all monitors) — the uinput DIRECT device maps onto it.
            screen_w, screen_h = _x11_root_size()
        self._pointer = UinputPointer(screen_w, screen_h)

    def capture(self) -> bytes:
        return subprocess.run(["maim"], capture_output=True, check=True).stdout

    def spawn(self, argv: list[str], cwd: str | None = None) -> subprocess.Popen:
        """Launch a process on the real session (caller manages its lifetime)."""
        return subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def move(self, x: float, y: float) -> None:
        self._pointer.move(x, y)

    def mouse_down(self, button: str = "left") -> None:
        self._pointer.press(button)

    def mouse_up(self, button: str = "left") -> None:
        self._pointer.release(button)

    def scroll(self, clicks: int, horizontal: bool = False) -> None:
        self._pointer.scroll(clicks, horizontal=horizontal)

    def key(self, name: str) -> None:
        self._pointer.key(name)

    def type_text(self, text: str) -> None:
        self._pointer.type_text(text)

    def capture_window(self, name: str) -> bytes:
        """Grab one window by title via ``maim -i <id>`` on the real display.

        Resolves the id with ``xdotool search``. On X11 with a compositor (GNOME/KDE
        redirect every window to an offscreen pixmap) this captures correctly even when
        the window is occluded; on a bare non-compositing X11 the occluded region may
        show whatever covers it — the ``nested`` target avoids that entirely (the app is
        alone on its own display)."""
        found = subprocess.run(
            ["xdotool", "search", "--name", name], capture_output=True, text=True
        ).stdout.split()
        if not found:
            return self.capture()
        return subprocess.run(["maim", "-i", found[0]], capture_output=True, check=True).stdout

    def close(self) -> None:
        self._pointer.close()


class PortableBackend(DesktopBackend):
    """A cross-platform real-session backend — the one selected on **macOS / Windows**, where the
    Linux uinput/X11 path doesn't exist. Screen capture via **mss**, pointer + keyboard via
    **pynput**, both pure-Python and OS-native underneath (Quartz on macOS, Win32 SendInput/GDI on
    Windows, Xlib on Linux). It drives the whole virtual desktop in screen pixels, so
    ``target="screen"`` works everywhere; per-window targeting on macOS/Windows is a follow-up
    (mss/pynput don't enumerate windows). On macOS the process needs Screen-Recording (capture) +
    Accessibility (input) permission, granted once to the host terminal/app.

    Linux keeps :class:`LocalBackend` (deeper, works on X11 + Wayland); this is the portable
    fallback. It's verifiable on Linux too (mss/pynput honour ``DISPLAY``), so the macOS/Windows
    behaviour is exercised in CI on real runners."""

    _BUTTONS = ("left", "right", "middle")
    # chord tokens → pynput Key attribute names
    _KEYS = {
        "return": "enter", "enter": "enter", "tab": "tab", "esc": "esc", "escape": "esc",
        "space": "space", "backspace": "backspace", "delete": "delete", "del": "delete",
        "up": "up", "down": "down", "left": "left", "right": "right", "home": "home", "end": "end",
        "pageup": "page_up", "pagedown": "page_down", "ctrl": "ctrl", "control": "ctrl",
        "alt": "alt", "option": "alt", "shift": "shift", "cmd": "cmd", "super": "cmd",
        "meta": "cmd", "win": "cmd",
    }

    def __init__(self):
        try:
            import mss  # noqa: PLC0415 — optional cross-platform deps, present on macOS/Windows
            from pynput import keyboard, mouse  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "the portable desktop backend (macOS/Windows) needs `mss` + `pynput` "
                "(`uv add mss pynput`)"
            ) from exc
        self._mss = mss
        self._mouse = mouse.Controller()
        self._Button = mouse.Button
        self._kbd = keyboard.Controller()
        self._Key = keyboard.Key
        with mss.mss() as sct:
            mon = sct.monitors[0]  # [0] = the full virtual screen across all monitors
        self.screen_w, self.screen_h = int(mon["width"]), int(mon["height"])

    def is_alive(self) -> bool:
        return True  # the real desktop is always there (no nested server to die)

    def capture(self) -> bytes:
        from PIL import Image  # noqa: PLC0415

        with self._mss.mss() as sct:
            shot = sct.grab(sct.monitors[0])  # [0] = the full virtual screen across all monitors
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def move(self, x: float, y: float) -> None:
        self._mouse.position = (int(x), int(y))

    def _button(self, name: str):
        return getattr(self._Button, name if name in self._BUTTONS else "left")

    def mouse_down(self, button: str = "left") -> None:
        self._mouse.press(self._button(button))

    def mouse_up(self, button: str = "left") -> None:
        self._mouse.release(self._button(button))

    def scroll(self, clicks: int, horizontal: bool = False) -> None:
        # pynput scroll(dx, dy): positive dy scrolls up, positive dx scrolls right.
        self._mouse.scroll(clicks, 0) if horizontal else self._mouse.scroll(0, clicks)

    def type_text(self, text: str) -> None:
        self._kbd.type(text)

    def _resolve_key(self, token: str):
        if len(token) == 1:
            return token  # a literal character
        return getattr(self._Key, self._KEYS.get(token.lower(), token.lower()), token)

    def key(self, name: str) -> None:
        mods, final = _parse_chord(name)
        held = [self._resolve_key(m) for m in mods]
        target = self._resolve_key(final)
        for k in held:
            self._kbd.press(k)
        self._kbd.press(target)
        self._kbd.release(target)
        for k in reversed(held):
            self._kbd.release(k)


def nested_server_command(display: str, size: str, headless: bool) -> list[str]:
    """The nested X server command line: ``Xvfb`` when headless (runs in the
    background, no window — for CI/servers), else ``Xephyr`` (renders as a window on
    the real desktop so you can *watch* the agent act). Both give an isolated display
    the agent drives via ``DISPLAY=:N``."""
    if headless:
        return ["Xvfb", display, "-screen", "0", f"{size}x24", "-nolisten", "tcp"]
    return ["Xephyr", display, "-screen", size, "-br", "-ac", "-noreset", "-no-host-grab"]




def select_desktop_backend(config) -> DesktopBackend:
    """Build the backend named by ``config.desktop_target`` (``local`` | ``nested``).

    - ``nested`` → an isolated Xephyr/Xvfb display (Linux-only; raises
      :class:`DesktopUnsupportedError` elsewhere — the nested sandbox needs an X server).
    - ``local`` → the real session: :class:`LocalBackend` (uinput/maim) on Linux, the
      cross-platform :class:`PortableBackend` (pynput/mss) on macOS/Windows so ``target="screen"``
      automation works there too.
    """
    if config.desktop_target == "nested":
        if not desktop_supported():
            raise DesktopUnsupportedError(desktop_unsupported_message())
        return NestedBackend(
            config.nested_display, config.nested_size, headless=config.nested_headless
        )
    return LocalBackend() if desktop_supported() else PortableBackend()
