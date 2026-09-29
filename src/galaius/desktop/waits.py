"""Wait on the desktop itself instead of sleeping a guessed number of seconds.

:func:`until_window` polls until a window whose title contains a substring appears (or is gone),
failing with the windows it saw. It takes a plain lister callable, so every backend (X11, Windows,
macOS, the sandbox) feeds it the same way and the tests feed it lists.
"""

import asyncio
import time
from collections.abc import Callable

from galaius.desktop.geometry import HostWindow


class DesktopWaitTimeout(RuntimeError):
    """The condition did not hold before the deadline; the message says what was seen instead."""


async def until_window(
    list_windows: Callable[[], list[HostWindow]],
    title: str,
    *,
    present: bool,
    timeout_s: float,
    poll_s: float = 0.25,
) -> str:
    """Poll until a window whose title contains ``title`` (case-insensitive) is there
    (``present``) or is not. Returns the report line; raises :class:`DesktopWaitTimeout`."""
    needle = title.lower()
    deadline = time.monotonic() + timeout_s
    while True:
        windows = await asyncio.to_thread(list_windows)
        match = next((w for w in windows if needle in w.title.lower()), None)
        if present and match is not None:
            return f"window {match.title!r} is visible at {match.geometry}"
        if not present and match is None:
            return f"no window titled like {title!r} — gone"
        if time.monotonic() >= deadline:
            titles = ", ".join(repr(w.title) for w in windows[:12]) or "none"
            state = "appear" if present else "go away"
            raise DesktopWaitTimeout(
                f"window {title!r} did not {state} within {timeout_s:g}s. Open windows: {titles}"
            )
        await asyncio.sleep(poll_s)
