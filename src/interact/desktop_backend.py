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
import shutil
import subprocess
import time
from abc import ABC, abstractmethod

ABS_MAX = 32767
_BUTTONS = {"left": 1, "middle": 2, "right": 3}


def _frac_dark(gray, cutoff: int = 8) -> float:
    """Fraction of (near-)true-black pixels in an 8-bit grayscale PIL image, via its histogram
    (C-fast, no Python per-pixel loop). The cutoff is deliberately low: an unrendered GL buffer is
    *exactly* black (0,0,0), whereas a real dark theme (#1e1e1e ≈ 30) sits well above it — so the
    repaint heuristic fires on the unrendered case without flagging a legitimately dark UI."""
    hist = gray.histogram()
    total = sum(hist) or 1
    return sum(hist[:cutoff]) / total


def _gl_unrendered(png: bytes, *, strip_frac: float = 0.12, hard: float = 0.9) -> bool:
    """True when a nested GL-window capture looks like it never painted: the whole frame is
    near-black, or a black bottom strip sits over a rendered body — a blurred BottomNavigationBar
    that software GL (``LIBGL_ALWAYS_SOFTWARE=1``) left black (#7/#8). Both clear after a repaint
    nudge. A genuinely dark theme has a dark body too, so the strip-only case demands a much lighter
    body — otherwise every capture of a dark UI would needlessly nudge (and reset its scroll)."""
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
    cut = int(h * (1 - strip_frac))
    if cut <= 0 or cut >= h:
        return False
    strip = _frac_dark(gray.crop((0, cut, w, h)))
    body = _frac_dark(gray.crop((0, 0, w, cut)))
    return strip >= hard and body < 0.5


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
        self.display = f":{display}"
        self.size = size
        self.headless = headless
        self.env = {**os.environ, "DISPLAY": self.display}
        width, height = size.split("x")
        self.screen_w, self.screen_h = int(width), int(height)
        self._procs: list[subprocess.Popen] = []
        # Windows whose black frame a repaint did NOT change — an intentionally pure-black/OLED UI,
        # not an unrendered GL buffer. Don't nudge them again (a resize on every capture would reset
        # the app's scroll); see capture_window.
        self._repaint_useless: set[str] = set()
        command = nested_server_command(self.display, size, headless)
        self.server_name = command[0]
        if shutil.which(self.server_name) is None:
            pkg = "xvfb" if headless else "xserver-xephyr"
            raise RuntimeError(f"{self.server_name} not installed (apt install {pkg})")
        self._xserver = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._await_ready(ready_timeout)

    def _await_ready(self, timeout: float) -> None:
        """Block until the nested server answers, so spawn/capture don't race startup."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._xserver.poll() is not None:
                raise RuntimeError(f"{self.server_name} {self.display} exited (rc={self._xserver.returncode})")
            try:
                _x11_screen_size(self.env)
                return
            except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
                time.sleep(0.1)
        raise RuntimeError(f"{self.server_name} {self.display} did not become ready in {timeout}s")

    def spawn(self, argv: list[str]) -> subprocess.Popen:
        """Launch a process inside the nested display (tracked for teardown)."""
        proc = subprocess.Popen(argv, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs.append(proc)
        return proc

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
        return subprocess.run(["maim"], env=self.env, capture_output=True, check=True).stdout

    def _maim_window(self, wid: str) -> bytes:
        return subprocess.run(["maim", "-i", wid], env=self.env, capture_output=True, check=True).stdout

    def capture_window(self, name: str) -> bytes:
        """PNG of one nested window by title (``maim -i <id>``). Nothing can occlude it here, so this
        is its true content — except a software-GL surface can present a stale black buffer until it
        repaints, so a frame that looks unrendered (:func:`_gl_unrendered`) triggers exactly one
        repaint nudge + recapture. The nudge is idempotent and the repaint persists, so a rendered
        frame is returned untouched (no scroll-resetting resize on every screenshot)."""
        wid = self._window_id(name)
        if wid is None:
            return self.capture()
        img = self._maim_window(wid)
        useless = getattr(self, "_repaint_useless", set())
        if _gl_unrendered(img) and name not in useless and self.force_repaint(name):
            wid = self._window_id(name) or wid
            img = self._maim_window(wid)
            if _gl_unrendered(img):
                # The repaint didn't change a black frame → this UI is intentionally black, not an
                # unrendered GL surface. Remember it so future captures don't nudge (and scroll-reset)
                # it every time. The #7/#8 case repaints to a rendered frame, so it's never marked.
                useless.add(name)
        return img

    def force_repaint(self, name: str) -> bool:
        """Force a full repaint by nudging the window's size (shrink 2px, restore); returns True if
        it nudged. A Flutter/GL app under software GL presents a stale/uninitialised buffer to X
        until a configure event makes it relayout — so a fresh launch (or its blurred bottom bar)
        captures solid black. One resize triggers a complete repaint that then persists for later
        frames. Verified live driving aino's GPU UI in the sandbox."""
        wid = self._window_id(name)
        geo = self.window_geometry(name)
        if wid is None or geo is None:
            return False
        _, _, w, h = geo
        if w < 4 or h < 4:
            return False
        self._xdotool_ok("windowsize", wid, str(w), str(h - 2))
        time.sleep(0.35)
        self._xdotool_ok("windowsize", wid, str(w), str(h))
        time.sleep(0.4)
        return True

    def focus(self, name: str) -> None:
        """Give the named window X input focus so keyboard events reach it. The sandbox has no
        window manager, so nothing holds the focus by default and keys would go nowhere (pointer
        events route by position regardless). Uses ``windowfocus`` (XSetInputFocus), which works
        WM-less — unlike ``windowactivate``, which needs ``_NET_ACTIVE_WINDOW`` (the very error
        that drove a consumer to abandon interact, #6)."""
        wid = self._window_id(name)
        if wid is not None:
            self._xdotool_ok("windowfocus", wid)

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
        """The wid of the window titled ``name`` — visible matches first, and among several the
        largest. A toolkit (Flutter/GTK) spawns hidden same-titled helper windows (a 10x10 GL
        surface); without this filter the helper wins and capture/input hit the wrong window."""
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

        return max(ids, key=_area)

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


def select_desktop_backend(config) -> DesktopBackend:
    """Build the backend named by ``config.desktop_target`` (``local`` | ``nested``).

    For ``nested``, ``config.nested_headless`` picks Xvfb (background) vs Xephyr (visible).
    """
    if config.desktop_target == "nested":
        return NestedBackend(
            config.nested_display, config.nested_size, headless=config.nested_headless
        )
    return LocalBackend()
