"""X11 cursor-shape detection via XFixes (ctypes, no pip deps) — reports what the pointer
looks like (text / pointer / grab / …) so the agent can tell a link from plain text."""

import ctypes
import ctypes.util
import logging

_log = logging.getLogger("interact")


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


