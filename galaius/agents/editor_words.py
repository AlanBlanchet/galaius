"""What he typed in an editor or terminal session of an agent CLI (Claude Code, Codex), read from the conversation index `galaius agents search`
builds over each CLI's saved sessions (`conversations.v<N>.sqlite3` in the registry folder).

The index already knows which lines are a person's: a prompt of a session he types in (`tier` 0, `TYPED`), credentials masked when it was built, a
session galaius started never read as a copy. This module reads those rows with stdlib `sqlite3`, read-only, and keeps his words only: a line the index
marks as quoted (a launch brief an agent was handed, a `[Galaius: …]` block) or a harness notice is no word of his. The index lags the files by what has not
been read yet: `refresh_index` brings it up to date for a bounded time and says how much is still pending."""

import sqlite3
from pathlib import Path
from typing import NamedTuple

#: The index's own tier for what a person typed (`conversations.TYPED`); a test holds the two equal.
TYPED = 0
#: The CLIs whose saved sessions he types in; `galaius` is the registry's own runs, whose messages `RunLedger.his_messages` reads once.
SOURCES = ("claude", "codex")
#: What is no word of his though a CLI saved it as a user turn: the index marks a machine-written line `(quoted) `, a harness writes these.
NOT_HIS = ("(quoted) ", "<task-notification>", "<command-", "<local-command", "Stop hook feedback", "Caveat: The messages below")


class Typed(NamedTuple):
    """One message of his before its frustration is read: when, the conversation (a session id), the run it reached ("" for a session no run has),
    the role of that run, his words, where it was typed (`claude`, `codex`, `web`) and the project's name."""

    at: float
    conversation: str
    to_run: str
    role: str
    text: str
    source: str
    project: str


def index_path(folder: Path) -> Path | None:
    """The newest conversation index in `folder`; None when `agents search` never built one here."""
    found = sorted(folder.glob("conversations.v*.sqlite3"), key=lambda path: int(path.stem.rpartition(".v")[2] or 0))
    return found[-1] if found else None


def read(folder: Path, since: float) -> list[Typed]:
    """His messages of editor and terminal sessions from `since` on, oldest first. An index that is absent, locked or of a shape this build does not know
    reads as none: the caller says so (`index_path`)."""
    path = index_path(folder)
    if path is None:
        return []
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = db.execute(
                "SELECT l.at, l.conversation, t.text, c.source, c.project FROM lines l JOIN line_texts t ON t.id = l.id JOIN conversations c ON c.id = l.conversation "
                f"WHERE l.tier = {TYPED} AND l.kind = 'prompt' AND l.at IS NOT NULL AND l.at >= ? AND c.source IN ({','.join('?' * len(SOURCES))}) ORDER BY l.at",
                (since, *SOURCES)).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return []
    return [Typed(at=float(at), conversation=str(conversation), to_run="", role="you", text=text, source=str(source), project=str(project or ""))
            for at, conversation, text, source, project in rows if isinstance(text, str) and text.strip() and not text.lstrip().startswith(NOT_HIS)]


class Coverage(NamedTuple):
    """How much of the CLIs' saved sessions the index holds after `refresh_index`: its conversations and the files still unread (None: another process reads)."""

    conversations: int
    pending: int | None


def refresh_index(folder: Path, seconds: float) -> Coverage | None:
    """Bring the index up to date for at most `seconds` (`ConversationIndex.refresh`); None when it could not be (the answer then is the index as it stands).
    Imported here only: the registry's import costs seconds the other verbs never pay."""
    from galaius.agents.conversations import ConversationIndex

    try:
        covered = ConversationIndex(folder=folder).refresh(seconds)
    except (OSError, sqlite3.Error, ValueError):
        return None
    return Coverage(covered.conversations, covered.pending)


__all__ = ["Coverage", "Typed", "index_path", "read", "refresh_index"]
