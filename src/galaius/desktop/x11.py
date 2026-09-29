"""An X11 display driven through XTEST (xdotool), maim, RandR and python-xlib.

The user's own X session (``host_desktop()`` on Linux) and the nested sandbox
(:class:`~galaius.desktop.nested.NestedBackend`, which owns its X server) are the same thing to
drive: this class is that one implementation, scoped to its ``DISPLAY`` through ``env``."""

import os
import re
import subprocess

from galaius.desktop.backend import DesktopBackend
from galaius.desktop.geometry import HostWindow, Monitor
from galaius.desktop.input import (
    MULTI_CLICK_GAP_MS,
    _BUTTONS,
    check_xdotool_key_output,
    to_xdotool_key,
)

# `xrandr --listmonitors` line: " 0: +*DP-1 2560/598x1440/336+0+0  DP-1". The token after the
# index is the MONITOR name behind its flags (+ active, * primary); the trailing outputs are absent
# for a virtual monitor (`xrandr --setmonitor L … none`, how an ultrawide is split in two).
_MONITOR_LINE = re.compile(r"\s*(\d+):\s+[+*]*(\S+)\s+(\d+)/\d+x(\d+)/\d+([+-]\d+)([+-]\d+)")


class X11Display(DesktopBackend):
    """Capture, input, monitors and windows of the X display named ``display`` (``":0"``)."""

    # Milliseconds between synthesised wheel clicks. Several clicks fired as separate processes
    # reached a toolkit interleaved or out of order — a dock tab switching, a splitter jumping,
    # once a context menu opening (#88). One xdotool process with an explicit delay keeps them
    # ordered and evenly spaced.
    _WHEEL_DELAY_MS = 25
    _TYPE_DELAY_MS = 20

    def __init__(self, display: str):
        self.display = display
        self.env = {**os.environ, "DISPLAY": display}

    def _xdotool(self, *args: str) -> None:
        subprocess.run(["xdotool", *args], env=self.env, check=True)

    def _xdotool_ok(self, *args: str) -> None:
        """Best-effort xdotool that never raises — for repaint/focus nudges where a transient
        failure must not crash a capture."""
        subprocess.run(
            ["xdotool", *args], env=self.env, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # --- capture -----------------------------------------------------------------------------

    def capture(self) -> bytes:
        return subprocess.run(
            ["maim", "--hidecursor"], env=self.env, capture_output=True, check=True
        ).stdout

    def _maim_window(self, wid: str) -> bytes:
        return subprocess.run(
            ["maim", "--hidecursor", "-i", wid], env=self.env, capture_output=True, check=True
        ).stdout

    def _maim_region(self, x: int, y: int, w: int, h: int) -> bytes:
        # --hidecursor: a region grab reads the live root framebuffer, into which maim otherwise
        # superimposes the X pointer sprite — it would land mid-screenshot.
        return subprocess.run(
            ["maim", "--hidecursor", "-g", f"{w}x{h}+{x}+{y}"],
            env=self.env, capture_output=True, check=True,
        ).stdout

    def capture_region(self, x: int, y: int, w: int, h: int) -> bytes:
        return self._maim_region(x, y, w, h)

    # --- monitors + windows ------------------------------------------------------------------

    def _xrandr_monitors(self) -> str:
        try:
            return subprocess.run(
                ["xrandr", "--listmonitors"], env=self.env, capture_output=True, text=True,
                timeout=5, check=True,
            ).stdout
        except (FileNotFoundError, subprocess.SubprocessError):
            return ""

    def monitors(self) -> list[Monitor]:
        """Connected monitors from RandR, in xrandr's own order (the ``screen:<index>`` numbers)."""
        return [
            Monitor(index=int(m[1]), name=m[2], w=int(m[3]), h=int(m[4]), x=int(m[5]), y=int(m[6]))
            for m in (_MONITOR_LINE.match(line) for line in self._xrandr_monitors().splitlines())
            if m
        ]

    def windows(self) -> list[HostWindow]:
        """Titled top-level windows: the window manager's ``_NET_CLIENT_LIST`` when one runs, else
        (no WM — Xvfb, the sandbox) the viewable children of the root. One X connection per call."""
        from Xlib import X, Xatom  # noqa: PLC0415 — Linux-only dependency
        from Xlib import display as xdisplay  # noqa: PLC0415
        from Xlib import error as xerror  # noqa: PLC0415

        disp = xdisplay.Display(self.display)
        try:
            root = disp.screen().root
            clients = root.get_full_property(disp.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
            managed = clients is not None and len(clients.value) > 0
            if managed:
                candidates = [disp.create_resource_object("window", wid) for wid in clients.value]
            else:
                candidates = root.query_tree().children
            utf8 = disp.intern_atom("UTF8_STRING")
            net_name = disp.intern_atom("_NET_WM_NAME")
            out: list[HostWindow] = []
            for win in candidates:
                try:
                    # Minimised (iconic) and other-workspace windows are unmapped: not "visible".
                    if win.get_attributes().map_state != X.IsViewable:
                        continue
                    prop = win.get_full_property(net_name, utf8) or win.get_full_property(
                        Xatom.WM_NAME, X.AnyPropertyType
                    )
                    raw = prop.value if prop is not None else b""
                    title = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                    if not title:
                        continue
                    geom = win.get_geometry()
                    if not HostWindow.drivable(geom.width, geom.height):
                        continue
                    origin = root.translate_coords(win, 0, 0)
                    out.append(HostWindow(
                        handle=win.id, title=title, x=origin.x, y=origin.y,
                        w=geom.width, h=geom.height,
                    ))
                except xerror.XError:
                    continue  # the window closed while it was being listed
            return out
        finally:
            disp.close()

    # --- input ---------------------------------------------------------------------------------

    def move(self, x: float, y: float) -> None:
        self._xdotool("mousemove", "--sync", str(round(x)), str(round(y)))

    def mouse_down(self, button: str = "left") -> None:
        self._xdotool("mousedown", str(_BUTTONS[button]))

    def mouse_up(self, button: str = "left") -> None:
        self._xdotool("mouseup", str(_BUTTONS[button]))

    def click(self, x: float, y: float, button: str = "left", count: int = 1) -> None:
        if count == 1:
            super().click(x, y, button)
            return
        # A multi-click must COALESCE, so it is ONE xdotool process with an explicit inter-click
        # delay: presses fired as separate processes arrive as unevenly spaced as process
        # start-up, and a toolkit reads two of them as one double-click only by luck (#116).
        self.move(x, y)
        self._xdotool(
            "click", "--repeat", str(count), "--delay", str(MULTI_CLICK_GAP_MS),
            str(_BUTTONS[button]),
        )

    def type_text(self, text: str) -> None:
        # --clearmodifiers: a key the user is physically holding must not turn "a" into ctrl+a.
        # `--` so text starting with "-" is typed, not parsed as an option.
        self._xdotool("type", "--clearmodifiers", "--delay", str(self._TYPE_DELAY_MS), "--", text)

    def scroll(self, clicks: int, horizontal: bool = False) -> None:
        # X11 wheel buttons: 4=up, 5=down, 6=left, 7=right. Horizontal must use 6/7 — a left/right
        # request once fell through to a vertical button and a carousel never moved (#54).
        if horizontal:
            button = "7" if clicks > 0 else "6"
        else:
            button = "4" if clicks > 0 else "5"
        n = abs(clicks)
        if not n:
            return
        self._xdotool("click", "--repeat", str(n), "--delay", str(self._WHEEL_DELAY_MS), button)

    def key(self, name: str) -> None:
        """Press a key or chord (``super``, ``alt+Tab``, ``ctrl+shift+t``) on this display.

        The name must be one X knows — ``enter`` is not a keysym, ``Return`` is — and xdotool
        ANSWERS AN UNKNOWN NAME BY IGNORING IT AND EXITING 0, so the output is read to turn that
        silent no-op into an error (#115)."""
        proc = subprocess.run(
            ["xdotool", "key", "--clearmodifiers", "--", to_xdotool_key(name)],
            env=self.env, check=True, capture_output=True, text=True,
        )
        check_xdotool_key_output(name, (proc.stderr or "") + (proc.stdout or ""))
