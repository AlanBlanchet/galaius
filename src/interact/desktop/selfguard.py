"""Refuse to drive the editor window that is hosting the caller.

An agent can destroy the process issuing its own commands. Typing "Developer: Reload Window" into
the editor that runs the calling session ends that session mid-task — and nothing survives to
notice, report, or repair it. That happened: a reload aimed at one VS Code window landed in the
caller's own, and the session died.

So the blast radius of a desktop action must never include the actor. This identifies the
caller's own editor window by the project it has open, which is the one thing interact reliably
knows about its caller (``CLAUDE_PROJECT_DIR``, else the working directory).

Deliberately CONSERVATIVE: with no idea which project the caller has open, it claims nothing.
Guessing would block legitimate targets, and a guard that cries wolf gets bypassed — which is
worse than no guard at all.
"""

import os
import re
from pathlib import Path

#: Editors whose windows can host a coding agent. Matching an editor's window title is what makes
#: "my own window" identifiable at all; a browser or terminal of the same name is not one.
_EDITOR_MARKERS = ("visual studio code", "vscode", "code - oss", "cursor", "windsurf")


def _cwd_name() -> str:
    try:
        return Path(os.getcwd()).name
    except OSError:
        return ""


def self_window_hints() -> list[str]:
    """Project names whose editor window would be the caller's own. Empty when unknown."""
    raw = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    name = Path(raw).name if raw else _cwd_name()
    return [name] if name else []


def is_self_window(title: str) -> bool:
    """Is this window title the editor hosting the caller?

    Matched on the project name as a WHOLE word, so ``interactive-demo`` is not mistaken for
    ``interact`` — a false positive here blocks a legitimate target, which is the failure mode
    that gets a guard disabled.
    """
    if not title:
        return False
    lowered = title.lower()
    if not any(marker in lowered for marker in _EDITOR_MARKERS):
        return False
    for hint in self_window_hints():
        if re.search(rf"(?<![\w-]){re.escape(hint.lower())}(?![\w-])", lowered):
            return True
    return False


def self_target_error(title: str) -> str:
    """The refusal, naming why and how to proceed deliberately."""
    return (
        f"REFUSED: {title!r} is the editor window hosting THIS session. Driving it can end the "
        "session issuing the command — a reload or a close would kill the agent mid-task, with "
        "nothing left to notice or repair it. Open a separate window and target that instead "
        "(a fresh window also picks up a rebuilt extension). If you genuinely mean to drive your "
        "own editor, pass allow_self=True."
    )


def refusal_for(window_name: str | None, actions, *, allow_self: bool) -> str | None:
    """The refusal to return for this dispatch, or None to proceed.

    The whole policy in one place: INPUT aimed at the caller's own editor is refused, observation
    is not, and `allow_self` is the deliberate escape hatch. It lives here rather than inline at
    the call site so it can be tested without a window, a display, or a dispatch — the earlier
    test drove the real desktop, which made it depend on the user's open windows and capture
    their screen.
    """
    if window_name is None or allow_self:
        return None
    if not any(getattr(a, "mutates", True) for a in actions):
        return None  # looking is harmless; the guard is about input
    if not is_self_window(window_name):
        return None
    return self_target_error(window_name)
