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

import os
import time


ABS_MAX = 32767
_BUTTONS = {"left": 1, "middle": 2, "right": 3}


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


def _parse_chord(name: str) -> tuple[list[str], str]:
    """Split a key spec like ``"ctrl+shift+a"`` into (held modifiers, final key) — the shared
    grammar for every backend that synthesises a chord (uinput, pynput). A bare ``"a"`` → ([], "a")."""
    *mods, final = name.split("+")
    return mods, final


# Modifier token → evdev LEFT_* key name, for the uinput chord path.
_UINPUT_MODIFIERS = {
    "ctrl": "KEY_LEFTCTRL",
    "control": "KEY_LEFTCTRL",
    "shift": "KEY_LEFTSHIFT",
    "alt": "KEY_LEFTALT",
    "super": "KEY_LEFTMETA",
    "meta": "KEY_LEFTMETA",
    "cmd": "KEY_LEFTMETA",
}


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
            ecodes.EV_REL: [ecodes.REL_WHEEL, ecodes.REL_HWHEEL],
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

    def scroll(self, clicks: int, horizontal: bool = False) -> None:
        axis = self._ecodes.REL_HWHEEL if horizontal else self._ecodes.REL_WHEEL
        self._ui.write(self._ecodes.EV_REL, axis, clicks)
        self._ui.syn()

    def _key_code(self, token: str) -> int:
        """evdev code for a key token: a modifier name (ctrl/shift/alt/super) maps to its LEFT_*
        code, else KEY_<UPPER> (or a literal KEY_ name)."""
        name = _UINPUT_MODIFIERS.get(token.lower()) or (
            token if token.startswith("KEY_") else f"KEY_{token.upper()}"
        )
        return getattr(self._ecodes, name)

    def key(self, name: str) -> None:
        # Hold modifiers, tap the final key, release modifiers — so a chord like "ctrl+a" works,
        # not just a single key (previously getattr(ecodes, "KEY_CTRL+A") raised). Shared chord
        # split with the portable backend via _parse_chord.
        mods, final = _parse_chord(name)
        held = [self._key_code(m) for m in mods]
        target = self._key_code(final)
        for code in held:
            self._kbd.write(self._ecodes.EV_KEY, code, 1)
        self._kbd.write(self._ecodes.EV_KEY, target, 1)
        self._kbd.syn()
        self._kbd.write(self._ecodes.EV_KEY, target, 0)
        for code in reversed(held):
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


