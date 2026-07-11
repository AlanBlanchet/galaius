"""The desktop-automation subsystem, grouped into a package.

``window`` (the ``DesktopWindow`` target + coordinate transform, cursor, element, motion helpers),
``backend`` (the ``DesktopBackend`` ABC and the Local / Portable / Nested backends + uinput/video
primitives), ``atspi`` (AT-SPI accessibility), ``frames`` and ``geometry`` (coordinate + box
primitives). This ``__init__`` re-exports the public surface so ``from interact.desktop import
DesktopWindow`` and ``from interact import desktop; desktop.Motion`` keep resolving; sibling modules
import each other by their submodule path (``interact.desktop.backend`` etc.).
"""

# stdlib re-exports: some desktop tests patch these on the `desktop` namespace.
import asyncio  # noqa: F401
import subprocess  # noqa: F401

from interact.desktop.geometry import BoxArray  # noqa: F401 (geometry's Box is at .geometry.Box)
from interact.desktop.frames import Frame  # noqa: F401
from interact.desktop.backend import (  # noqa: F401
    ABS_MAX,
    DesktopBackend,
    DesktopUnsupportedError,
    LocalBackend,
    NestedBackend,
    PortableBackend,
    UinputPointer,
    _VideoSession,
    _ffmpeg_grab_args,
    _gl_unrendered,
    _parse_chord,
    _rects_overlap,
    _x11_root_size,
    _x11_screen_size,
    desktop_supported,
    desktop_unsupported_message,
    nested_server_command,
    screen_to_abs,
    select_desktop_backend,
)
from interact.desktop.window import (  # noqa: F401
    Box,
    CaptureError,
    CoordTransform,
    Cursor,
    DesktopElement,
    DesktopWindow,
    Motion,
    _DRAG_STEPS,
    _IOU_OVERLAP_THRESHOLD,
    _SCREEN_WID,
    _TITLEBAR_Y,
    _WM_BUTTON_NAMES,
    _XFixesCursorImage,
    _element_cache,
    _is_blank_png,
    _page_sig,
    gpu_surface_error,
)
from interact.desktop.atspi import AtSpi  # noqa: F401
