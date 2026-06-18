"""Cross-platform desktop input — the "deeper driver" leg.

Input injection uses the deepest portable Linux path: a ``/dev/uinput`` **absolute
virtual touchscreen** (``INPUT_PROP_DIRECT`` + ``ABS_X/ABS_Y``) via python-evdev.
libinput projects its coordinates onto the screen identically on **X11 and Wayland**
(GNOME / KDE / wlroots) — true absolute positioning, avoiding the relative-touchpad
demotion that breaks ``ydotool --absolute``. One driver for both display servers.

Requires ``/dev/uinput`` access — a udev rule plus membership of the ``input`` group,
no root (``interact doctor`` checks this). Linux-only; ``evdev`` is imported lazily so
this module imports everywhere. Capture and window enumeration stay per-display-server
(maim on X11, xdg-desktop-portal on Wayland) and are layered on top separately.

Coordinates are screen pixels; map other spaces in via :class:`interact.frames.Frame`.
"""

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

ABS_MAX = 32767
_BUTTONS = {"left": 1, "middle": 2, "right": 3}


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


def screen_to_abs(
    x: float, y: float, screen_w: int, screen_h: int, abs_max: int = ABS_MAX
) -> tuple[int, int]:
    """Map a screen-pixel point into a uinput absolute device's ``0..abs_max`` range.

    Pure — the testable core of absolute positioning. Clamps to the screen so an
    out-of-bounds detection can't fling the pointer off-screen.
    """
    if screen_w <= 0 or screen_h <= 0:
        return 0, 0
    cx = min(max(x, 0.0), float(screen_w))
    cy = min(max(y, 0.0), float(screen_h))
    return round(cx / screen_w * abs_max), round(cy / screen_h * abs_max)


def _keyboard_codes(ecodes) -> list[int]:
    """evdev key codes the virtual keyboard declares — letters, digits, and the common
    editing/modifier keys ``type_text``/``key`` emit. A device can only send keys it
    declares at creation."""
    names = (
        [f"KEY_{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
        + [f"KEY_{d}" for d in "0123456789"]
        + [
            "KEY_SPACE", "KEY_ENTER", "KEY_TAB", "KEY_BACKSPACE", "KEY_ESC", "KEY_DELETE",
            "KEY_MINUS", "KEY_EQUAL", "KEY_DOT", "KEY_COMMA", "KEY_SLASH", "KEY_SEMICOLON",
            "KEY_APOSTROPHE", "KEY_LEFTBRACE", "KEY_RIGHTBRACE", "KEY_BACKSLASH", "KEY_GRAVE",
            "KEY_LEFTSHIFT", "KEY_LEFTCTRL", "KEY_LEFTALT",
            "KEY_HOME", "KEY_END", "KEY_PAGEUP", "KEY_PAGEDOWN",
            "KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT",
        ]
    )
    return [getattr(ecodes, n) for n in names if hasattr(ecodes, n)]


class UinputPointer:
    """Absolute mouse + named-key input over ``/dev/uinput`` (X11 and Wayland).

    Declared as an ``INPUT_PROP_DIRECT`` touchscreen so libinput maps ``ABS_X/ABS_Y``
    onto the output. ``move``/``click``/``drag``/``scroll`` take screen-pixel coords;
    ``key`` presses a named ecode (``"KEY_ENTER"``, ``"KEY_TAB"``, …) — text typing
    is layout-dependent and handled separately.
    """

    def __init__(self, screen_w: int, screen_h: int, abs_max: int = ABS_MAX):
        try:
            from evdev import AbsInfo, UInput, ecodes
        except ImportError as exc:
            raise RuntimeError(
                "uinput input needs python-evdev (Linux only): `uv add evdev`"
            ) from exc

        self._ecodes = ecodes
        self.screen_w, self.screen_h, self.abs_max = screen_w, screen_h, abs_max
        capabilities = {
            ecodes.EV_KEY: [
                ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE, ecodes.BTN_TOUCH,
            ],
            ecodes.EV_ABS: [
                (ecodes.ABS_X, AbsInfo(0, 0, abs_max, 0, 0, 0)),
                (ecodes.ABS_Y, AbsInfo(0, 0, abs_max, 0, 0, 0)),
            ],
            ecodes.EV_REL: [ecodes.REL_WHEEL],
        }
        try:
            self._ui = UInput(
                capabilities,
                name="interact-virtual-pointer",
                input_props=[ecodes.INPUT_PROP_DIRECT],
            )
            # A SEPARATE keyboard node: the kernel drops key events a device never
            # declared, and a touchscreen (INPUT_PROP_DIRECT) + keyboard on one node
            # confuses libinput's classification — so typing/keys get their own device.
            self._kbd = UInput({ecodes.EV_KEY: _keyboard_codes(ecodes)}, name="interact-virtual-keyboard")
        except (PermissionError, FileNotFoundError) as exc:
            raise RuntimeError(
                "cannot open /dev/uinput — add a udev rule and join the `input` group "
                "(no root); see `interact doctor`"
            ) from exc

    def move(self, x: float, y: float) -> None:
        ax, ay = screen_to_abs(x, y, self.screen_w, self.screen_h, self.abs_max)
        self._ui.write(self._ecodes.EV_ABS, self._ecodes.ABS_X, ax)
        self._ui.write(self._ecodes.EV_ABS, self._ecodes.ABS_Y, ay)
        self._ui.syn()

    def _btn_code(self, button: str) -> int:
        return {
            "left": self._ecodes.BTN_LEFT,
            "right": self._ecodes.BTN_RIGHT,
            "middle": self._ecodes.BTN_MIDDLE,
        }[button]

    def press(self, button: str = "left") -> None:
        self._ui.write(self._ecodes.EV_KEY, self._btn_code(button), 1)
        self._ui.syn()

    def release(self, button: str = "left") -> None:
        self._ui.write(self._ecodes.EV_KEY, self._btn_code(button), 0)
        self._ui.syn()

    def click(self, x: float, y: float, button: str = "left") -> None:
        self.move(x, y)
        self.press(button)
        time.sleep(0.02)
        self.release(button)

    def scroll(self, clicks: int) -> None:
        self._ui.write(self._ecodes.EV_REL, self._ecodes.REL_WHEEL, clicks)
        self._ui.syn()

    def key(self, name: str) -> None:
        code = getattr(self._ecodes, name if name.startswith("KEY_") else f"KEY_{name.upper()}")
        self._kbd.write(self._ecodes.EV_KEY, code, 1)
        self._kbd.syn()
        self._kbd.write(self._ecodes.EV_KEY, code, 0)
        self._kbd.syn()

    def _char_spec(self, ch: str) -> tuple[str, bool] | None:
        """Map a character to its evdev key name + whether Shift is held (US layout).

        Covers the printable ASCII a desktop agent realistically types; unknown chars
        are skipped. Typing is inherently layout-dependent — this assumes a US keymap,
        the common case, and is enough for labels/identifiers/URLs.
        """
        named = {
            " ": ("KEY_SPACE", False), "\n": ("KEY_ENTER", False), "\t": ("KEY_TAB", False),
            "-": ("KEY_MINUS", False), "_": ("KEY_MINUS", True), "=": ("KEY_EQUAL", False),
            "+": ("KEY_EQUAL", True), ".": ("KEY_DOT", False), ",": ("KEY_COMMA", False),
            "/": ("KEY_SLASH", False), "?": ("KEY_SLASH", True), ":": ("KEY_SEMICOLON", True),
            ";": ("KEY_SEMICOLON", False), "@": ("KEY_2", True), "!": ("KEY_1", True),
        }
        if ch in named:
            return named[ch]
        if ch.isalpha() and ch.isascii():
            return (f"KEY_{ch.upper()}", ch.isupper())
        if ch.isdigit():
            return (f"KEY_{ch}", False)
        return None

    def type_text(self, text: str) -> None:
        shift = self._ecodes.KEY_LEFTSHIFT
        for ch in text:
            spec = self._char_spec(ch)
            if spec is None:
                continue
            code = getattr(self._ecodes, spec[0])
            if spec[1]:
                self._kbd.write(self._ecodes.EV_KEY, shift, 1)
            self._kbd.write(self._ecodes.EV_KEY, code, 1)
            self._kbd.write(self._ecodes.EV_KEY, code, 0)
            if spec[1]:
                self._kbd.write(self._ecodes.EV_KEY, shift, 0)
            self._kbd.syn()
            time.sleep(0.01)

    def close(self) -> None:
        self._ui.close()
        self._kbd.close()


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

    def scroll(self, clicks: int) -> None:
        """Scroll the wheel; positive is up, negative is down."""
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

    def spawn(self, argv: list[str]) -> subprocess.Popen:
        """Launch a process on the real session (caller manages its lifetime)."""
        return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def move(self, x: float, y: float) -> None:
        self._pointer.move(x, y)

    def mouse_down(self, button: str = "left") -> None:
        self._pointer.press(button)

    def mouse_up(self, button: str = "left") -> None:
        self._pointer.release(button)

    def scroll(self, clicks: int) -> None:
        self._pointer.scroll(clicks)

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

    def scroll(self, clicks: int) -> None:
        self._mouse.scroll(0, clicks)  # pynput: positive dy scrolls up

    def type_text(self, text: str) -> None:
        self._kbd.type(text)

    def _resolve_key(self, token: str):
        if len(token) == 1:
            return token  # a literal character
        return getattr(self._Key, self._KEYS.get(token.lower(), token.lower()), token)

    def key(self, name: str) -> None:
        *mods, final = name.split("+")
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


class NestedBackend(DesktopBackend):
    """An isolated nested X display the agent owns end to end.

    Starts its own X server — **Xephyr** (visible: rendered as one window on the real
    desktop, so you can watch the agent) or **Xvfb** when ``headless`` (background, no
    window: for CI / servers) — then scopes every action to it with ``DISPLAY=:N``.
    Input goes to the *nested* pointer (not the user's), capture grabs only the nested
    screen. Use :meth:`spawn` to launch the app under test inside it. This is the
    "VM-like" target: reproducible, non-intrusive, and the basis of the desktop test
    suite. Needs ``xdotool`` + ``maim`` plus the chosen server (``apt install
    xserver-xephyr`` / ``xvfb``)."""

    def __init__(self, display: int = 99, size: str = "1280x800", *,
                 headless: bool = False, ready_timeout: float = 5.0):
        self.size = size
        self.headless = headless
        width, height = size.split("x")
        self.screen_w, self.screen_h = int(width), int(height)
        self._procs: list[subprocess.Popen] = []
        self._logs: dict[int, str] = {}  # pid -> temp logfile of a launched app's stdout/stderr
        # Windows whose black frame a repaint did NOT change — an intentionally pure-black/OLED UI,
        # not an unrendered GL buffer. Don't nudge them again (a resize on every capture would reset
        # the app's scroll); see capture_window.
        self._repaint_useless: set[str] = set()
        self._repaint_attempts: dict[str, int] = {}  # per-window nudge count before giving up
        self._repaint_delta = 60  # px the repaint nudge resizes by — big enough to rebind a blur layer
        self.server_name = "Xvfb" if headless else "Xephyr"
        if shutil.which(self.server_name) is None:
            pkg = "xvfb" if headless else "xserver-xephyr"
            raise RuntimeError(f"{self.server_name} not installed (apt install {pkg})")
        # Start the X server on a FREE display, trying the next free one if a concurrent interact
        # server grabbed it between our check and the server's claim. Hardcoding :99 made several
        # MCP servers fight over it — the loser's Xephyr died seconds in, taking the launched app's
        # windows with it (#33). Picking a free number also sidesteps a stale lock from a crashed
        # prior server.
        last_err: Exception | None = None
        for candidate in self._free_displays(display):
            self.display = f":{candidate}"
            # Force software GL for everything in the sandbox. A nested Xephyr/Xvfb display has no
            # usable hardware GL, so a GPU app (Flutter/Electron/games) that tries hardware EGL hits
            # `DRI2: failed to create any config` and renders BLACK; Mesa's swrast always provides a
            # config. setdefault so an explicit global override still wins.
            self.env = {**os.environ, "DISPLAY": self.display}
            self.env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
            try:
                self._start_server(ready_timeout)
                return
            except RuntimeError as exc:
                last_err = exc  # display raced/unusable → reap the failed server, try the next
        raise last_err or RuntimeError(f"could not start {self.server_name} on any free display")

    @staticmethod
    def _free_displays(preferred: int) -> list[int]:
        """Display numbers to try, free ones first from ``preferred`` up — a display is taken if its
        X lock (``/tmp/.X<n>-lock``) or socket exists. Read-only probe; never writes (#33)."""
        free = [
            n for n in range(preferred, preferred + 64)
            if not os.path.exists(f"/tmp/.X{n}-lock") and not os.path.exists(f"/tmp/.X11-unix/X{n}")
        ]
        return free or [preferred]

    def _start_server(self, ready_timeout: float) -> None:
        """Spawn the X server on ``self.display`` and wait until it answers. On failure, reap the
        process (so a raced display doesn't leave a Xephyr zombie) and re-raise so __init__ can try
        the next display."""
        command = nested_server_command(self.display, self.size, self.headless)
        # Log the X server's own output so a death mid-session can be explained, not just "rc=1".
        self._xserver_log_path = self._open_log(f"{self.server_name.lower()}{self.display}")
        with open(self._xserver_log_path, "wb") as f:
            self._xserver = subprocess.Popen(command, stdout=f, stderr=subprocess.STDOUT)
        try:
            self._await_ready(ready_timeout)
        except RuntimeError:
            self._reap_server()
            raise

    def _reap_server(self) -> None:
        """Tear down (and reap) the X server process + its log — for a failed/raced startup so no
        ``<defunct>`` Xephyr lingers."""
        srv = getattr(self, "_xserver", None)
        if srv is not None:
            if srv.poll() is None:
                srv.terminate()
                try:
                    srv.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    srv.kill()
        path = getattr(self, "_xserver_log_path", None)
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    @staticmethod
    def _open_log(label: str) -> str:
        fd, path = tempfile.mkstemp(prefix=f"interact-{label}-", suffix=".log")
        os.close(fd)
        return path

    def _await_ready(self, timeout: float) -> None:
        """Block until the nested server answers, so spawn/capture don't race startup."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._xserver.poll() is not None:
                tail = _tail_file(self._xserver_log_path, 600)
                why = f": {tail}" if tail else ""
                raise RuntimeError(
                    f"{self.server_name} {self.display} exited (rc={self._xserver.returncode}){why}"
                )
            try:
                _x11_screen_size(self.env)
                return
            except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
                time.sleep(0.1)
        raise RuntimeError(f"{self.server_name} {self.display} did not become ready in {timeout}s")

    def is_alive(self) -> bool:
        """True if the nested X server is still running AND answering. A long session can exhaust the
        display (dozens of leaked GPU apps) so the server dies; the cached backend would then reject
        every launch — even ``xterm`` — until it is respawned (#10)."""
        if self._xserver.poll() is not None:
            return False
        try:
            _x11_screen_size(self.env)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
            return False

    def display_health(self) -> str:
        """One-line diagnostic for when a launch produced no window: whether the nested X server is
        alive, and its recent output if it died — so launch_app can explain a dead display instead
        of only listing the generic Qt-helper windows (#33)."""
        if self.is_alive():
            return ""
        tail = _tail_file(getattr(self, "_xserver_log_path", None), 400)
        rc = getattr(getattr(self, "_xserver", None), "returncode", "?")
        return f"The sandbox {self.server_name} {self.display} is DOWN (rc={rc})" + (
            f": {tail}" if tail else " — call reset_sandbox to respawn it."
        )

    def _reap(self) -> None:
        """Drop exited child apps (and unlink their logs) so a long session doesn't accumulate dead
        entries — the leak behind a display that eventually refuses new clients (#10)."""
        alive: list[subprocess.Popen] = []
        for proc in self._procs:
            if proc.poll() is None:
                alive.append(proc)
            else:
                stale = self._logs.pop(proc.pid, None)
                if stale:
                    try:
                        os.unlink(stale)
                    except OSError:
                        pass
        self._procs = alive

    def spawn(self, argv: list[str]) -> subprocess.Popen:
        """Launch a process inside the nested display (tracked for teardown), capturing its
        stdout/stderr so a crash can be explained. Reaps previously-exited apps first."""
        self._reap()
        path = self._open_log("app")
        with open(path, "wb") as f:
            proc = subprocess.Popen(argv, env=self.env, stdout=f, stderr=subprocess.STDOUT)
        self._procs.append(proc)
        self._logs[proc.pid] = path
        return proc

    def proc_output(self, proc: subprocess.Popen, limit: int = 1500) -> str:
        """Tail of what a launched process wrote (stdout+stderr) — to tell an app crash from a dead
        display in a launch error. Empty if the proc isn't tracked."""
        return _tail_file(self._logs.get(proc.pid), limit)

    def _xdotool(self, *args: str) -> None:
        subprocess.run(["xdotool", *args], env=self.env, check=True)

    def _xdotool_ok(self, *args: str) -> None:
        """Best-effort xdotool that never raises — for repaint/focus nudges where a transient
        failure must not crash a capture."""
        subprocess.run(
            ["xdotool", *args], env=self.env, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def capture(self) -> bytes:
        self._reap()  # drop apps that exited since the last spawn so zombies don't accumulate (#11)
        return subprocess.run(["maim"], env=self.env, capture_output=True, check=True).stdout

    def _maim_window(self, wid: str) -> bytes:
        return subprocess.run(["maim", "-i", wid], env=self.env, capture_output=True, check=True).stdout

    def _maim_region(self, x: int, y: int, w: int, h: int) -> bytes:
        return subprocess.run(
            ["maim", "-g", f"{w}x{h}+{x}+{y}"], env=self.env, capture_output=True, check=True
        ).stdout

    def _overlay_rects(self) -> list[tuple[int, int, int, int]]:
        """Absolute ``(x, y, w, h)`` of mapped override-redirect windows on the nested display —
        menus, Qt/GTK combo drop-downs, tooltips. These open as SEPARATE top-level windows (the WM
        is bypassed, so they're root children), which a per-window ``maim -i`` never includes; we
        composite them into the capture (#31). Best-effort: no python-xlib → empty → plain grab."""
        try:
            from Xlib import display as _xdisplay  # lazy: Linux X11 only, optional
        except ImportError:
            return []
        rects: list[tuple[int, int, int, int]] = []
        try:
            disp = _xdisplay.Display(self.env["DISPLAY"])
            try:
                root = disp.screen().root
                for win in root.query_tree().children:
                    attrs = win.get_attributes()
                    if attrs.map_state != 2 or not attrs.override_redirect:  # 2 = IsViewable
                        continue
                    geo = win.get_geometry()
                    if geo.width <= 4 or geo.height <= 4:
                        continue
                    abs_pos = root.translate_coords(win, 0, 0)
                    rects.append((abs_pos.x, abs_pos.y, geo.width, geo.height))
            finally:
                disp.close()
        except Exception:  # any X error → degrade to no overlays, never break a capture
            return []
        return rects

    def _composited_grab(self, name: str, wid: str) -> bytes:
        """``maim`` of the window, expanded right/down to also capture any override-redirect popup
        overlapping it (#31). Anchored at the window's own top-left so image coordinates stay
        window-relative (what the click path expects) — a popup that opens upward/leftward is
        clipped (rare; use target="nested" for that), but a normal downward drop-down is included."""
        try:
            geo = self.window_geometry(name)
        except (subprocess.SubprocessError, OSError, ValueError, KeyError):
            geo = None  # can't read geometry → just grab the window, never fail the capture
        if geo is None:
            return self._maim_window(wid)
        wx, wy, ww, wh = geo
        overlays = [r for r in self._overlay_rects() if _rects_overlap(r, geo)]
        if not overlays:
            return self._maim_window(wid)
        right = max([wx + ww] + [r[0] + r[2] for r in overlays])
        bottom = max([wy + wh] + [r[1] + r[3] for r in overlays])
        x0, y0 = max(0, wx), max(0, wy)
        x1 = min(self.screen_w, right)
        y1 = min(self.screen_h, bottom)
        return self._maim_region(x0, y0, x1 - x0, y1 - y0)

    def _grab_window(self, name: str, wid: str) -> bytes:
        """Capture the window (compositing in any popup, #31), resilient to a wid that went stale
        between enumeration and capture: a multi-process app (Chrome) recreates its top-level
        window, so the enumerated id can already be dead by the time maim runs (the recurring
        real-world ``maim -i N returned non-zero``). Re-resolve the title once and retry; if the
        window is truly gone, fall back to a full nested-screen grab rather than a hard error."""
        try:
            return self._composited_grab(name, wid)
        except subprocess.CalledProcessError:
            fresh = self._window_id(name)
            if fresh and fresh != wid:
                try:
                    return self._composited_grab(name, fresh)
                except subprocess.CalledProcessError:
                    pass
            return self.capture()  # whole nested display — last resort, never crash-or-black

    def capture_window(self, name: str) -> bytes:
        """PNG of one nested window by title (``maim -i <id>``). Nothing can occlude it here, so this
        is its true content — except a software-GL surface can present a stale black buffer until it
        repaints, so a frame that looks unrendered (:func:`_gl_unrendered`) triggers a repaint nudge
        + recapture. Up to 2 nudges are tried (a blurred ConvexAppBar can need a stronger relayout),
        then the window is left alone so a genuinely-black UI isn't resized on every screenshot. A
        window that renders is re-armed, so a later navigation that goes black is nudged again."""
        self._reap()  # reap exited apps every capture, not only on spawn (#11)
        wid = self._window_id(name)
        if wid is None:
            return self.capture()
        img = self._grab_window(name, wid)
        if _gl_unrendered(img) and name not in self._repaint_useless:
            n = self._repaint_attempts.get(name, 0)
            if n < 2 and self.force_repaint(name):
                self._repaint_attempts[name] = n + 1
                wid = self._window_id(name) or wid
                img = self._grab_window(name, wid)
                if not _gl_unrendered(img):
                    self._repaint_attempts.pop(name, None)  # rendered → re-arm for the next screen
                elif n + 1 >= 2:
                    # Still black after 2 nudges → intentionally black (OLED) or a software-GL
                    # BackdropFilter blur that won't composite under X11 (#14-#20). Stop nudging (and
                    # scroll-resetting) it; the Wayland/sway backend renders this class correctly.
                    self._repaint_useless.add(name)
                    self._repaint_attempts.pop(name, None)
        return img

    def capture_video(self, name: str, duration: float = 3.0, fps: int = 10) -> bytes:
        """Record one nested window via ffmpeg x11grab on THIS display (``DISPLAY=:N``), not ``:0``.
        Recording a sandbox window grabbed the real display and returned all-black frames while
        screenshot() worked (#18). Forces a repaint first so the first frame isn't a black GL
        buffer (same software-GL cause as the still-capture nudge)."""
        geo = self.window_geometry(name)
        x, y, w, h = geo if geo is not None else (0, 0, self.screen_w, self.screen_h)
        self.force_repaint(name)
        fd, out = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-f", "x11grab", "-video_size", f"{w}x{h}",
                 "-framerate", str(fps), "-i", f"{self.env['DISPLAY']}+{max(0, x)},{max(0, y)}",
                 "-c:v", "libx264", "-preset", "ultrafast", "-t", str(duration),
                 "-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", out],
                env=self.env, check=True, capture_output=True, timeout=duration + 10,
            )
            with open(out, "rb") as fh:
                return fh.read()
        finally:
            try:
                os.unlink(out)
            except OSError:
                pass

    def force_repaint(self, name: str) -> bool:
        """Force a full repaint by nudging the window's size (shrink, restore); returns True if it
        nudged. A Flutter/GL app under software GL presents a stale/uninitialised buffer to X until a
        configure event makes it relayout — so a fresh launch (or its blurred bottom bar) captures
        solid black. The resize delta (``_repaint_delta``, default 60px, capped at h/4) is large
        enough to make Skia rebind a blurred bar's layer, not just relayout the body. The repaint
        then persists for later frames. Verified live driving aino's GPU UI in the sandbox."""
        wid = self._window_id(name)
        geo = self.window_geometry(name)
        if wid is None or geo is None:
            return False
        _, _, w, h = geo
        if w < 4 or h < 4:
            return False
        delta = max(2, min(getattr(self, "_repaint_delta", 60), h // 4))
        self._xdotool_ok("windowsize", wid, str(w), str(h - delta))
        time.sleep(0.35)
        self._xdotool_ok("windowsize", wid, str(w), str(h))
        time.sleep(0.4)
        return True

    def focus(self, name: str) -> None:
        """Give the named window X input focus so keyboard events reach it. Resolves by title, then
        delegates to :meth:`focus_wid`."""
        self.focus_wid(self._window_id(name))

    def focus_wid(self, wid) -> None:
        """Give a SPECIFIC window X input focus (XSetInputFocus) so the XTEST keystrokes that
        follow land in it. The sandbox has no window manager, so nothing holds focus by default and
        keys would go nowhere (pointer events route by position regardless). ``--sync`` blocks until
        the server confirms the focus change, so a separate ``xdotool type`` process can't fire
        before focus settles. ``windowfocus`` (not ``windowactivate``, which needs
        ``_NET_ACTIVE_WINDOW`` — the error that drove a consumer to abandon interact, #6) works
        WM-less. Bounded so a window that refuses focus can't hang keyboard input (#25)."""
        if wid in (None, 0, "0"):
            return
        try:
            subprocess.run(
                ["xdotool", "windowfocus", "--sync", str(wid)],
                env=self.env, check=False, timeout=3,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            pass

    def move(self, x: float, y: float) -> None:
        self._xdotool("mousemove", "--sync", str(round(x)), str(round(y)))

    def mouse_down(self, button: str = "left") -> None:
        self._xdotool("mousedown", str(_BUTTONS[button]))

    def mouse_up(self, button: str = "left") -> None:
        self._xdotool("mouseup", str(_BUTTONS[button]))

    def type_text(self, text: str) -> None:
        self._xdotool("type", "--delay", "20", text)

    def scroll(self, clicks: int) -> None:
        button = "4" if clicks > 0 else "5"  # X11 wheel: 4=up, 5=down
        for _ in range(abs(clicks)):
            self._xdotool("click", button)

    def key(self, name: str) -> None:
        self._xdotool("key", name)  # xdotool keysym syntax, e.g. "ctrl+a", "Return"

    def _window_id(self, name: str) -> str | None:
        """The wid of the window titled ``name``. A toolkit spawns several same-/substring-titled
        top-levels: a Flutter app exposes both its app-id window (``com.example.aino``) and the
        titled one (``aino``), and a 10x10 GL/clipboard helper. Title-substring matching hits them
        all, so rank candidates: a RENDERED window beats a black/transient helper (so capture +
        input never land on the unrendered one, #28/#1.4), then the largest. Single match → fast
        path, no grab."""
        visible = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", name],
            env=self.env, capture_output=True, text=True,
        ).stdout.split()
        ids = visible or subprocess.run(
            ["xdotool", "search", "--name", name], env=self.env, capture_output=True, text=True
        ).stdout.split()
        if not ids:
            return None
        if len(ids) == 1:
            return ids[0]

        def _area(wid: str) -> int:
            try:
                info = subprocess.run(
                    ["xdotool", "getwindowgeometry", "--shell", wid],
                    env=self.env, capture_output=True, text=True, check=True,
                ).stdout
                v = dict(ln.split("=", 1) for ln in info.splitlines() if "=" in ln)
                return int(v["WIDTH"]) * int(v["HEIGHT"])
            except (subprocess.SubprocessError, KeyError, ValueError):
                return 0

        def _rank(wid: str) -> tuple[int, int]:
            rendered = 1
            try:
                if _gl_unrendered(self._maim_window(wid)):
                    rendered = 0  # a solid-black helper/transient → deprioritise vs a real window
            except subprocess.CalledProcessError:
                rendered = 0
            return (rendered, _area(wid))

        return max(ids, key=_rank)

    def window_geometry(self, name: str) -> tuple[int, int, int, int] | None:
        """``(x, y, w, h)`` of the first window whose title matches ``name`` (or None)."""
        wid = self._window_id(name)
        if wid is None:
            return None
        info = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", wid],
            env=self.env, capture_output=True, text=True, check=True,
        ).stdout
        vals = dict(line.split("=", 1) for line in info.splitlines() if "=" in line)
        return int(vals["X"]), int(vals["Y"]), int(vals["WIDTH"]), int(vals["HEIGHT"])

    def list_windows(self) -> list[tuple[int, str]]:
        """``(wid, title)`` of named windows on the nested display, one per distinct title. There's
        no WM here, so query X directly. Falls back to non-visible matches: a window that's mapped
        but not yet marked viewable (an app mid-startup) must still be reported, or launch_app's
        poll would say nothing appeared when it did."""
        ids = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", ".+"],
            env=self.env, capture_output=True, text=True,
        ).stdout.split() or subprocess.run(
            ["xdotool", "search", "--name", ".+"], env=self.env, capture_output=True, text=True
        ).stdout.split()
        out: list[tuple[int, str]] = []
        seen: set[str] = set()
        for wid in ids:
            name = subprocess.run(
                ["xdotool", "getwindowname", wid], env=self.env, capture_output=True, text=True
            ).stdout.strip()
            if name and name not in seen:
                seen.add(name)
                out.append((int(wid), name))
        return out

    def close(self) -> None:
        for proc in (*self._procs, self._xserver):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for path in (*self._logs.values(), getattr(self, "_xserver_log_path", None)):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        self._logs.clear()


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
