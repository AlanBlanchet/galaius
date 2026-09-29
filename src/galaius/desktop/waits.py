"""Wait on the desktop itself instead of sleeping a guessed number of seconds.

* :func:`until_window` — a window whose title contains a substring appears (or is gone).
* :func:`until_text` — text read off the target (OCR, :mod:`galaius.desktop.ocr`) appears or
  is gone: the launcher's result row, a dialog's message.

Both poll plain callables until a deadline and fail with what they last saw, so every backend
(X11, Windows, macOS, the sandbox) feeds them the same way and the tests feed them lists.
"""

import asyncio
import re
import time
from collections.abc import Callable
from difflib import SequenceMatcher

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


#: How alike the read words must be to the wanted ones: one OCR slip in a word ("whisperlng",
#: "0pen") passes, a prefix still being typed ("notep" for "Notepad") does not.
TEXT_SIMILARITY = 0.85
_WORD = re.compile(r"\w+")


def text_matches(needle: str, seen: str, similarity: float = TEXT_SIMILARITY) -> bool:
    """Do the words of ``needle`` occur, in order, among the words read in ``seen``?

    Case, punctuation and spacing are ignored, and OCR noise is tolerated: runs of n-1 to n+1 read
    words are compared to the n wanted ones with spaces removed (OCR splits and merges words), and
    pass at ``similarity``. Words match words, never letters inside a longer word."""
    want = _WORD.findall(needle.lower())
    got = _WORD.findall(seen.lower())
    if not want:
        return False
    target = "".join(want)
    for size in {max(1, len(want) - 1), len(want), len(want) + 1}:
        for i in range(len(got) - size + 1):
            window = "".join(got[i:i + size])
            if window == target or SequenceMatcher(None, target, window).ratio() >= similarity:
                return True
    return False


async def until_text(
    read: Callable[[], str],
    needle: str,
    *,
    present: bool,
    timeout_s: float,
    poll_s: float = 0.25,
) -> str:
    """Poll ``read`` (an OCR pass over the target, seconds each) until ``needle`` is there
    (``present``) or is not. Returns the report line; raises :class:`DesktopWaitTimeout` quoting
    the last text read."""
    start = time.monotonic()
    while True:
        seen = await asyncio.to_thread(read)
        elapsed = time.monotonic() - start
        if text_matches(needle, seen) == present:
            return f"text {needle!r} {'appeared' if present else 'gone'} after {elapsed:.1f}s"
        if elapsed >= timeout_s:
            snippet = " ".join(seen.split())[:200] or "(no text)"
            state = "appear" if present else "go away"
            raise DesktopWaitTimeout(
                f"text {needle!r} did not {state} within {timeout_s:g}s. Last read: {snippet!r}"
            )
        await asyncio.sleep(poll_s)

