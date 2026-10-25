"""The badge itself: an always-on-top window saying this screen is being watched, with its Stop, and
the hotkey that presses it from the keyboard (Ctrl+Alt+Shift+S).

Standard library ONLY, run by its path (`ScreenMarker`), so ANY Python whose tkinter can draw on
this desktop runs it — on Linux the runtime's own (uv's standalone build) aborts in XCB, the
system's draws. It says ``up`` once its window is on screen; exit 3 = Stop (button or hotkey), 2 =
no toolkit or display to draw with. It writes nothing: its parent switches sharing off on exit 3,
so the badge needs no part of galaius.

    python screen_badge.py "<who is watching>"
"""

import ctypes
import ctypes.util
import sys
import threading

TITLE = "Screen shared"
HOTKEY_SAID = "Ctrl+Alt+Shift+S"
#: Exit codes the parent reads (`ScreenMarker`).
STOPPED, NO_TOOLKIT = 3, 2


def windows_hotkey(pressed: threading.Event) -> bool:
    """Ctrl+Alt+Shift+S as a system-wide hotkey, in its own thread: Windows posts it to the thread
    that registered it, and Tk's own message loop would swallow a message posted to Tk's."""
    ready, registered = threading.Event(), [False]

    def listen() -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        mod_alt, mod_control, mod_shift, mod_norepeat, vk_s, wm_hotkey = 0x1, 0x2, 0x4, 0x4000, 0x53, 0x0312
        registered[0] = bool(user32.RegisterHotKey(None, 1, mod_alt | mod_control | mod_shift | mod_norepeat, vk_s))
        ready.set()
        if not registered[0]:
            return
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == wm_hotkey:
                pressed.set()
                return

    threading.Thread(target=listen, daemon=True, name="badge-hotkey").start()
    ready.wait(5)
    return registered[0]


class X11Hotkey:
    """Ctrl+Alt+Shift+S grabbed on the root window through its own X connection, read from Tk's
    loop (`poll`): one thread, so no Xlib call ever races another. Grabbed with and without Caps
    Lock and Num Lock, or a keyboard with Num Lock on would never trigger it."""

    SHIFT, LOCK, CONTROL, MOD1, MOD2 = 1, 2, 4, 8, 16
    KEY_PRESS = 2

    def __init__(self) -> None:
        self.ok = False
        name = ctypes.util.find_library("X11")
        if name is None:
            return
        x = self.x = ctypes.CDLL(name)
        x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
        x.XDefaultRootWindow.restype, x.XDefaultRootWindow.argtypes = ctypes.c_ulong, [ctypes.c_void_p]
        x.XStringToKeysym.restype, x.XStringToKeysym.argtypes = ctypes.c_ulong, [ctypes.c_char_p]
        x.XKeysymToKeycode.restype, x.XKeysymToKeycode.argtypes = ctypes.c_ubyte, [ctypes.c_void_p, ctypes.c_ulong]
        x.XGrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XPending.argtypes = [ctypes.c_void_p]
        x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        handler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        self.failed = [False]
        # Kept on self: X calls it later. A grab another program holds is BadAccess — reported here
        # instead of Xlib's default handler, which would end the badge (and with it the view).
        self._on_error = handler(lambda display, event: self.failed.__setitem__(0, True) or 0)
        x.XSetErrorHandler(self._on_error)
        self.display = x.XOpenDisplay(None)
        if not self.display:
            return
        root = x.XDefaultRootWindow(self.display)
        code = x.XKeysymToKeycode(self.display, x.XStringToKeysym(b"s"))
        for extra in (0, self.LOCK, self.MOD2, self.LOCK | self.MOD2):
            x.XGrabKey(self.display, code, self.SHIFT | self.CONTROL | self.MOD1 | extra, root, 1, 1, 1)
        x.XSync(self.display, 0)
        self.ok = not self.failed[0]
        self._event = (ctypes.c_byte * 192)()  # an XEvent: a union of 24 longs

    def poll(self) -> bool:
        """True once the hotkey was pressed (never blocks)."""
        while self.ok and self.x.XPending(self.display):
            self.x.XNextEvent(self.display, ctypes.byref(self._event))
            if int.from_bytes(bytes(self._event)[:4], sys.byteorder) == self.KEY_PRESS:
                return True
        return False


def badge(viewer: str) -> int:
    """Draw the badge until its parent takes it down (0) or Stop is pressed (3); 2 when this
    desktop has nothing to draw it with."""
    try:
        import tkinter as tk
    except ImportError:
        return NO_TOOLKIT
    try:
        root = tk.Tk()
    except tk.TclError:
        return NO_TOOLKIT
    stopped = [False]
    pressed = threading.Event()
    if sys.platform == "win32":
        hotkey = windows_hotkey(pressed)
        x11 = None
    elif sys.platform.startswith("linux"):
        x11 = X11Hotkey()
        hotkey = x11.ok
    else:
        hotkey, x11 = False, None

    def press() -> None:
        stopped[0] = True
        root.destroy()

    root.title(TITLE)
    root.overrideredirect(True)  # no title bar: a badge, not a window to manage
    root.attributes("-topmost", True)
    root.configure(background="#b3261e")
    said = f"{TITLE}: {viewer}" if viewer else TITLE
    tk.Label(root, text=said, fg="#ffffff", bg="#b3261e", padx=12, pady=6, font=("TkDefaultFont", 10, "bold")).pack(side="left")
    tk.Button(root, text=f"Stop ({HOTKEY_SAID})" if hotkey else "Stop", command=press, padx=10, relief="flat",
              fg="#b3261e", bg="#ffffff", activebackground="#f2b8b5").pack(side="left", padx=(0, 8), pady=6)
    root.update_idletasks()
    width, height = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry(f"{width}x{height}+{max(root.winfo_screenwidth() - width - 24, 0)}+24")

    def keep_above() -> None:
        """A window manager drops a borderless window behind a new full-screen one: put back every
        second, so nothing the viewer opens covers it."""
        root.attributes("-topmost", True)
        root.lift()
        root.after(1000, keep_above)

    def watch_hotkey() -> None:
        if pressed.is_set() or (x11 is not None and x11.poll()):
            press()
            return
        root.after(100, watch_hotkey)

    keep_above()
    watch_hotkey()
    root.update()
    print("up", flush=True)
    root.mainloop()
    return STOPPED if stopped[0] else 0


if __name__ == "__main__":
    sys.exit(badge(sys.argv[1] if len(sys.argv) > 1 else ""))
