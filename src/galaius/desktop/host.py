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

    def windows(self) -> list[HostWindow]:
        import ctypes  # noqa: PLC0415 — Windows-only API surface
        from ctypes import wintypes  # noqa: PLC0415

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        dwmapi = ctypes.WinDLL("dwmapi")
        # Explicit argtypes: without them ctypes passes an HWND as a 32-bit C int and truncates it.
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]
        found: list[HostWindow] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _lparam):
            # A minimised window is "visible" to Windows, parked at about (-32000, -32000).
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            cloaked = wintypes.DWORD()
            if dwmapi.DwmGetWindowAttribute(
                hwnd, self._DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
            ) == 0 and cloaked.value:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if HostWindow.drivable(w, h):
                found.append(HostWindow(handle=int(hwnd), title=title.value, x=rect.left, y=rect.top, w=w, h=h))
            return True

        user32.EnumWindows(visit, 0)
        return found


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

    def windows(self) -> list[HostWindow]:
        import Quartz  # noqa: PLC0415 — pyobjc, installed with pynput on macOS only

        rows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        return self.windows_from_quartz([dict(row) for row in rows or []])


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
