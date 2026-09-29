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
from collections.abc import Callable, Sequence

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


_WORD = re.compile(r"\w+")
#: Characters OCR confuses with each other, folded onto one form before comparing. A look-alike is
#: the only slack: no edit distance, so "notepa" is never "Notepad" and "Saved" never "Save".
_LOOKALIKES = str.maketrans({"0": "o", "1": "l", "i": "l", "|": "l", "5": "s", "8": "b"})


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text)


def _fold(token: str) -> str:
    return token.lower().translate(_LOOKALIKES)


#: Tokens this short may share a line with the echo: a search icon read as "Q", a glyph as "x".
_GLYPH = 2


def _echo_mask(lines: list[list[str]], typed: Sequence[str]) -> list[list[bool]]:
    """Tokens that are galaius's own typing echoed by the field it typed into. A field shows what
    was typed verbatim (exact case) as its whole line, give or take an icon glyph; a launcher's
    result row usually spells the name its own way or says more ("Results for …"). For each typed
    string only the FIRST such line is masked."""
    masked = [[False] * len(line) for line in lines]
    for text in typed:
        want = _tokens(text)
        n = len(want)
        for li, line in enumerate(lines):
            hit = next(
                (i for i in range(len(line) - n + 1)
                 if line[i:i + n] == want and not any(masked[li][i:i + n])
                 and all(len(t) <= _GLYPH for t in line[:i] + line[i + n:])),
                None,
            )
            if hit is not None:
                masked[li][hit:hit + n] = [True] * n
                break
    return masked


def text_matches(needle: str, seen: str, typed: Sequence[str] = ()) -> bool:
    """Do the words of ``needle`` occur, as whole tokens and in order, in the text ``seen``?

    Case and punctuation are ignored; OCR look-alikes (0/o, 1/l/i, 5/s, 8/b) are folded; a run of
    read tokens may join into the wanted ones (OCR splits "whisper ing" and merges "Resultsfor"),
    but always from a token boundary to a token boundary. ``typed`` is text galaius typed into
    this target: its echo in the focused field never satisfies the wait."""
    want = "".join(_fold(t) for t in _tokens(needle))
    if not want:
        return False
    # Form feeds separate independent reads of one frame (two OCR passes): never join across one.
    return any(_matches_in(want, block, typed) for block in seen.split("\f"))


def _matches_in(want: str, block: str, typed: Sequence[str]) -> bool:
    lines = [_tokens(line) for line in block.splitlines()]
    masks = _echo_mask(lines, typed)
    got = [t for line in lines for t in line]
    masked = [m for mask in masks for m in mask]
    for start in range(len(got)):
        joined = ""
        for end in range(start, len(got)):
            if masked[end]:
                break
            joined += _fold(got[end])
            if joined == want:
                return True
            if len(joined) >= len(want) or not want.startswith(joined):
                break
    return False


async def until_text(
    read: Callable[[], str],
    needle: str,
    *,
    present: bool,
    timeout_s: float,
    poll_s: float = 0.25,
    typed: Sequence[str] = (),
) -> str:
    """Poll ``read`` (an OCR pass over the target, seconds each) until ``needle`` is there
    (``present``) or is not — never counting the echo of ``typed``. Returns the report line;
    raises :class:`DesktopWaitTimeout` quoting the last text read."""
    start = time.monotonic()
    while True:
        seen = await asyncio.to_thread(read)
        elapsed = time.monotonic() - start
        if text_matches(needle, seen, typed) == present:
            return f"text {needle!r} {'appeared' if present else 'gone'} after {elapsed:.1f}s"
        if elapsed >= timeout_s:
            snippet = " ".join(seen.split())[:200] or "(no text)"
            state = "appear" if present else "go away"
            raise DesktopWaitTimeout(
                f"text {needle!r} did not {state} within {timeout_s:g}s. Last read: {snippet!r}"
            )
        await asyncio.sleep(poll_s)

