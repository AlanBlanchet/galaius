"""Windows: the icon in the notification area beside the clock (`Shell_NotifyIcon` on a hidden window of
its own), its menu a popup at the cursor on either click. Added again when Explorer restarts (the
taskbar broadcasts `TaskbarCreated`) and retried each tick while the taskbar is not there yet (a
logon starts the service before Explorer)."""

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import ClassVar

import win32api
import win32con
import win32gui

from galaius.host.paths import UserPaths
from galaius.host.private_files import PRIVATE_FILES
from galaius.machine.tray.view import TrayBackend, TrayDriver, TrayView

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetTimer.argtypes = (wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p)
user32.SetTimer.restype = ctypes.c_size_t


class WindowsTray(TrayBackend):
    #: The window message the icon's clicks arrive as, and the tick timer's id.
    notified: ClassVar[int] = win32con.WM_USER + 20
    timer: ClassVar[int] = 1
    #: The sizes inside each icon file: Windows picks the one its scale needs (16 px at 100 %, 24 at 150 %...).
    sizes: ClassVar[tuple[int, ...]] = (16, 20, 24, 32, 40, 48, 64)

    def __init__(self) -> None:
        self.window = 0
        self.view: TrayView | None = None
        self.menu_view: TrayView | None = None
        self.added = False
        self.driver: TrayDriver | None = None
        self.icons: dict[tuple[str, int], int] = {}
        self.folder = Path(UserPaths.data()) / "tray-icons"

    def run(self, driver: TrayDriver) -> None:
        self.driver = driver
        created = win32gui.RegisterWindowMessage("TaskbarCreated")
        instance = win32api.GetModuleHandle(None)
        kind = win32gui.WNDCLASS()
        kind.hInstance, kind.lpszClassName = instance, "GalaiusTray"
        kind.lpfnWndProc = {self.notified: self._clicked, win32con.WM_COMMAND: self._command, win32con.WM_TIMER: self._ticked,
                            created: self._taskbar_created, win32con.WM_DESTROY: self._destroyed}
        win32gui.RegisterClass(kind)
        # A top-level window never shown: a message-only one would miss the taskbar's broadcast.
        self.window = win32gui.CreateWindow("GalaiusTray", "Galaius", win32con.WS_OVERLAPPED, 0, 0, 0, 0, 0, 0, instance, None)
        user32.SetTimer(self.window, self.timer, round(TrayView.frame_seconds * 1000), None)
        self._show(driver.tick())
        win32gui.PumpMessages()

    def _icon(self, view: TrayView) -> int:
        key = (view.shown, view.frame)
        if key not in self.icons:
            PRIVATE_FILES.directory(self.folder)
            path = self.folder / f"{view.shown}-{view.frame}.ico"
            view.picture(max(self.sizes)).save(path, format="ICO", sizes=[(size, size) for size in self.sizes])
            small = win32api.GetSystemMetrics(win32con.SM_CXSMICON)
            self.icons[key] = win32gui.LoadImage(0, str(path), win32con.IMAGE_ICON, small, small, win32con.LR_LOADFROMFILE)
        return self.icons[key]

    def _show(self, view: TrayView | None) -> None:
        if view is not None:
            self.view = view
        elif self.added or self.view is None:
            return
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY if self.added else win32gui.NIM_ADD,
                                      (self.window, 0, flags, self.notified, self._icon(self.view), self.view.tooltip[:127]))
            self.added = True
        except win32gui.error:  # no taskbar yet, or Explorer restarting: the next tick adds it
            self.added = False

    def _ticked(self, window: int, message: int, wparam: int, lparam: int) -> int:
        if self.driver.leaving:
            win32gui.DestroyWindow(self.window)
        else:
            self._show(self.driver.tick())
        return 0

    def _taskbar_created(self, window: int, message: int, wparam: int, lparam: int) -> int:
        self.added = False
        self._show(None)
        return 0

    def _clicked(self, window: int, message: int, wparam: int, lparam: int) -> int:
        if lparam in {win32con.WM_LBUTTONUP, win32con.WM_RBUTTONUP} and self.view is not None:
            self.menu_view = self.view
            menu = win32gui.CreatePopupMenu()
            for index, item in enumerate(self.view.items, start=1):
                win32gui.AppendMenu(menu, win32con.MF_STRING | (0 if item.action is not None else win32con.MF_GRAYED), index, item.label)
            try:
                win32gui.SetForegroundWindow(self.window)  # else the menu stays open after a click elsewhere
            except win32gui.error:
                pass
            x, y = win32gui.GetCursorPos()
            win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN | win32con.TPM_BOTTOMALIGN | win32con.TPM_RIGHTBUTTON, x, y, 0, self.window, None)
            win32gui.PostMessage(self.window, win32con.WM_NULL, 0, 0)
            win32gui.DestroyMenu(menu)
        return 0

    def _command(self, window: int, message: int, wparam: int, lparam: int) -> int:
        index = win32api.LOWORD(wparam)
        if self.menu_view is not None and 1 <= index <= len(self.menu_view.items) and (action := self.menu_view.items[index - 1].action) is not None:
            self.driver.act(action)
        return 0

    def _destroyed(self, window: int, message: int, wparam: int, lparam: int) -> int:
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.window, 0))
        except win32gui.error:
            pass
        win32gui.PostQuitMessage(0)
        return 0

    def open(self, url: str) -> None:
        os.startfile(url)  # noqa: S606 - the person's default browser, on the page this computer's connection named
