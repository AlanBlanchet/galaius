"""The desktop the user sits at, as one :class:`DesktopBackend` per OS — what ``target="screen"``
drives, what ``list_desktop_windows`` lists and what ``wait_for window=`` polls.

* Linux X11 → :class:`~galaius.desktop.x11.X11Display` on ``$DISPLAY``.
* Windows → :class:`WindowsDesktop`: pynput (``SendInput``) + mss (GDI) + ``EnumWindows``.
* macOS → :class:`MacDesktop`: pynput (Quartz ``CGEvent``) + mss (CoreGraphics) +
  ``CGWindowListCopyWindowInfo``.

Design note: ``host_desktop.md`` beside this file."""

import functools
import os
import sys

from galaius.desktop.backend import DesktopBackend, DesktopUnsupportedError, PortableBackend
from galaius.desktop.geometry import HostWindow
from galaius.desktop.x11 import X11Display


class WindowsDesktop(PortableBackend):
    """The Windows desktop. Coordinates are physical pixels: mss makes the process per-monitor DPI
    aware, so ``GetWindowRect``, the grab and ``SendInput`` agree."""

    # DWMWA_CLOAKED: a UWP app suspended in the background, or a window on another virtual
    # desktop, is "visible" to IsWindowVisible yet nowhere on screen.
    _DWMWA_CLOAKED = 14

    @functools.cached_property
    def _win32(self):
        """user32 + dwmapi with explicit argtypes: without them ctypes passes an HWND as a 32-bit C
        int and truncates it."""
        import ctypes  # noqa: PLC0415 — Windows-only API surface
        from ctypes import wintypes  # noqa: PLC0415

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        dwmapi = ctypes.WinDLL("dwmapi")
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetForegroundWindow.restype = wintypes.HWND
        dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]
        return ctypes, wintypes, user32, dwmapi

    def _describe(self, hwnd) -> HostWindow | None:
        """A visible, not minimised, not cloaked, drivable window, else None (title may be empty)."""
        ctypes, wintypes, user32, dwmapi = self._win32
        # A minimised window is "visible" to Windows, parked at about (-32000, -32000).
        if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return None
        cloaked = wintypes.DWORD()
        if dwmapi.DwmGetWindowAttribute(
            hwnd, self._DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        ) == 0 and cloaked.value:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if not HostWindow.drivable(w, h):
            return None
        return HostWindow(handle=int(hwnd), title=title.value, x=rect.left, y=rect.top, w=w, h=h)

    def windows(self) -> list[HostWindow]:
        ctypes, wintypes, user32, _ = self._win32
        found: list[HostWindow] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _lparam):
            window = self._describe(hwnd)
            if window is not None and window.title:
                found.append(window)
            return True

        user32.EnumWindows(visit, 0)
        return found

    def active_window(self) -> HostWindow | None:
        """``GetForegroundWindow``: the Start menu while it is open, else the window keys go to."""
        return self._describe(self._win32[2].GetForegroundWindow())


class MacDesktop(PortableBackend):
    """The macOS desktop, in points (the unit Quartz input uses). Window titles need Screen
    Recording permission; without it macOS returns only the owning app's name."""

    @staticmethod
    def windows_from_quartz(entries: list[dict]) -> list[HostWindow]:
        """``CGWindowListCopyWindowInfo`` rows → normal-layer, titled, drivable windows. A title
        reads ``"<window> — <app>"`` so a wait can name either."""
        found: list[HostWindow] = []
        for entry in entries:
            if entry.get("kCGWindowLayer", 0) != 0:  # menu bar, dock, overlays
                continue
            bounds = dict(entry.get("kCGWindowBounds") or {})
            w, h = int(bounds.get("Width", 0)), int(bounds.get("Height", 0))
            name = str(entry.get("kCGWindowName") or "")
            owner = str(entry.get("kCGWindowOwnerName") or "")
            title = f"{name} — {owner}" if name and owner else name or owner
            if not title or not HostWindow.drivable(w, h):
                continue
            found.append(HostWindow(
                handle=int(entry["kCGWindowNumber"]), title=title,
                x=int(bounds.get("X", 0)), y=int(bounds.get("Y", 0)), w=w, h=h,
            ))
        return found

    # Window layers below the Dock (20); the menu bar sits at 24/25. Spotlight and other launcher
    # panels float above normal windows (layer 0) but below these.
    _BELOW_DOCK = 20
    _NOT_WINDOWS = frozenset({"Window Server", "Dock", "SystemUIServer", "Control Center", "Notification Center"})

    @staticmethod
    def _rows() -> list[dict]:
        import Quartz  # noqa: PLC0415 — pyobjc, installed with pynput on macOS only

        rows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        return [dict(row) for row in rows or []]

    def windows(self) -> list[HostWindow]:
        return self.windows_from_quartz(self._rows())

    @classmethod
    def topmost_from_quartz(cls, entries: list[dict]) -> HostWindow | None:
        """The front window in ``CGWindowList``'s front-to-back order that is a real window or a
        launcher panel (below the Dock's layer, not a system chrome owner) — Spotlight while it is
        open, else the frontmost app's window."""
        for entry in entries:
            if not 0 <= entry.get("kCGWindowLayer", 0) < cls._BELOW_DOCK:
                continue
            if str(entry.get("kCGWindowOwnerName") or "") in cls._NOT_WINDOWS:
                continue
            bounds = dict(entry.get("kCGWindowBounds") or {})
            w, h = int(bounds.get("Width", 0)), int(bounds.get("Height", 0))
            if not HostWindow.drivable(w, h):
                continue
            name, owner = str(entry.get("kCGWindowName") or ""), str(entry.get("kCGWindowOwnerName") or "")
            return HostWindow(
                handle=int(entry["kCGWindowNumber"]), title=f"{name} — {owner}" if name and owner else name or owner,
                x=int(bounds.get("X", 0)), y=int(bounds.get("Y", 0)), w=w, h=h,
            )
        return None

    def active_window(self) -> HostWindow | None:
        return self.topmost_from_quartz(self._rows())


_WAYLAND = (
    "This is a Wayland session: the desktop itself (keys to the shell, screen capture, window "
    "list) is not drivable yet — xdotool and maim only reach XWayland windows. Launch the app in "
    "galaius's sandbox instead (launch_app + target=\"nested:<title>\"), or log into an X11 "
    "session. Tracking: https://github.com/AlanBlanchet/galaius/issues/24"
)


@functools.cache
def host_desktop() -> DesktopBackend:
    """The backend for the desktop this process runs on. Cached; ``cache_clear()`` after changing
    ``DISPLAY``. Raises :class:`DesktopUnsupportedError` with the reason when there is none."""
    if sys.platform == "win32":
        return WindowsDesktop()
    if sys.platform == "darwin":
        return MacDesktop()
    display = os.environ.get("DISPLAY")
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" or (not display and os.environ.get("WAYLAND_DISPLAY")):
        raise DesktopUnsupportedError(_WAYLAND)
    if not display:
        raise DesktopUnsupportedError(
            "No X display: DISPLAY is not set in galaius's environment, so there is no desktop to "
            "drive. Use the sandbox (launch_app) or the browser target."
        )
    return X11Display(display)
