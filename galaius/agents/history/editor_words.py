"""What he typed in an editor or terminal session of an agent CLI (Claude Code, Codex), read from the conversation index `galaius agents search`
builds over each CLI's saved sessions (`conversations.v<N>.sqlite3` in the registry folder).

The index already knows which lines are a person's: a prompt of a session he types in (`tier` 0, `TYPED`), credentials masked when it was built, a
session galaius started never read as a copy. This module reads those rows with stdlib `sqlite3`, read-only, and keeps his words only: a line the index
marks as quoted (a launch brief an agent was handed, a `[Galaius: …]` block) or a harness notice is no word of his. The index lags the files by what has not
been read yet: `refresh_index` brings it up to date for a bounded time and says how much is still pending."""

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

#: The index's own tier for what a person typed (`conversations.TYPED`); a test holds the two equal.
TYPED = 0
#: The CLIs whose saved sessions he types in; `galaius` is the registry's own runs, whose messages `RunLedger.his_messages` reads once.
SOURCES = ("claude", "codex")
#: What is no word of his though a CLI saved it as a user turn: the index marks a machine-written line `(quoted) `, a harness writes these.
NOT_HIS = ("(quoted) ", "<task-notification>", "<command-", "<local-command", "Stop hook feedback", "Caveat: The messages below", "[Request interrupted by user")
#: Codex's IDE extension prefixes a turn with the editor's state (open tabs, pasted workspace text); his words follow this heading.
IDE_CONTEXT = "# Context from my IDE setup:"
IDE_REQUEST = "## My request"
#: A text this long is told from a replay (a forked or resumed session copies its history); a shorter one (`continue`, `ok`) is said again for real.
REPLAY_FROM = 20
#: The copy of a turn carries its original moment (to the second, in another session; within a minute, in the same one).
SAME_MOMENT = 2.0
SAME_SESSION_REPLAY = 60.0
#: A long text found in this many sessions or more is a replayed history, not him saying it again: only its first time counts. Said in two, it is a re-send.
REPLAYED_IN = 3


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
    #: `message`: a turn he typed; `answer`: his answer to a question card (Claude Code's `AskUserQuestion`), a tool's result in the transcript.
    kind: str = "message"


def index_path(folder: Path) -> Path | None:
    """The newest conversation index in `folder`; None when `agents search` never built one here."""
    found = sorted(folder.glob("conversations.v*.sqlite3"), key=lambda path: int(path.stem.rpartition(".v")[2] or 0))
    return found[-1] if found else None


class Reading(NamedTuple):
    """`read`'s answer: his messages, and why the index could not be read ("" when it could, or none exists: `index_path` says that)."""

    messages: list[Typed]
    problem: str = ""


def his_words(text: str) -> str:
    """What of a saved user turn is his: all of it, except a Codex turn that opens with the editor's state, where it is what follows `## My request`
    ("" when nothing does)."""
    if not text.lstrip().startswith(IDE_CONTEXT):
        return text
    _, found, after = text.partition(IDE_REQUEST)
    if not found:
        return ""
    heading, _, request = after.partition("\n")   # `## My request for Codex:` is a heading line; his words follow it (or sit after its colon)
    return (request or heading.partition(":")[2]).strip()


#: What Claude Code writes into the user turn that answers a question card.
CARD_ANSWER = b"The user answered"
#: The origins of the sessions a person types in (`ClaudeCodeProvider.typed_origins`); a test holds the two equal.
TYPED_ORIGINS = frozenset({"cli", "claude-vscode", "claude-desktop"})


def _moment(text: object) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


CARD_CACHE = ".card-answers.cache"
#: What the cache holds of a session file before the offset it was read to: this many bytes, hashed, say the file only grew since.
CACHE_TAIL = 64


def _answer_text(value: object) -> str:
    """One answer of a question card as words: a string; a list of picked options (several allowed) joined; an object (an answer with notes) by its `answer` / `text` /
    `label`. A decline (`User declined to answer`) is no word of his."""
    if isinstance(value, dict):
        value = next((value[key] for key in ("answer", "text", "label", "value") if key in value), "")
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(item) for item in value if isinstance(item, (str, int, float)))
    text = value.strip() if isinstance(value, str) else ""
    return "" if text.lower().startswith("user declined") else text


def _read_answers(path: Path, start: int, secrets: tuple[str, ...], mask) -> tuple[int, list[list]]:
    """The answers a session file holds from byte `start` on, as [at, session, text, cwd] lists, and the offset after its last whole line."""
    found: list[list] = []
    offset = start
    with path.open("rb") as lines:
        lines.seek(start)
        for line in lines:
            if not line.endswith(b"\n"):
                break   # a line still being written: read once it is whole
            offset += len(line)
            if CARD_ANSWER not in line:
                continue
            try:
                turn = json.loads(line)
            except ValueError:
                continue
            result = turn.get("toolUseResult") if isinstance(turn, dict) else None
            if (turn.get("type") != "user" or turn.get("isSidechain") or turn.get("entrypoint") not in TYPED_ORIGINS or not isinstance(result, dict)
                    or not isinstance(result.get("answers"), dict) or (at := _moment(turn.get("timestamp"))) is None):
                continue
            for value in result["answers"].values():
                if text := _answer_text(value):
                    found.append([at, str(turn.get("sessionId") or path.stem), mask(text, secrets), str(turn.get("cwd") or "")])
    return offset, found


def card_answers(folder: Path, since: float) -> list[Typed]:
    """His answers to question cards (`AskUserQuestion`) in Claude Code sessions since `since`: the transcript keeps them as a tool result
    (`toolUseResult.answers`, question -> answer), which the index never reads (it keeps a tool call's lead only). Files read are Claude's saved sessions
    changed since `since` (`ConversationIndex.files`), each from where the last call stopped (a cache of offsets and answers beside the registry: a session
    file runs to hundreds of megabytes, and only its end changes); a line is looked at only when it holds the answer marker; credentials are masked as the index masks them.
    Imported here only: the registry's import costs seconds the other verbs never pay."""
    from galaius.agents.conversations import ConversationIndex

    index = ConversationIndex(folder=folder)
    try:
        files = [found for found in index.files() if found.provider is not None and found.provider.name == "claude" and found.mtime / 1e9 >= since]
        secrets = index.secrets
    except (OSError, ValueError):
        return []
    cache_path = folder / CARD_CACHE
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
        kept = loaded.get("files", {}) if isinstance(loaded, dict) and loaded.get("version") == 1 else {}
    except (OSError, ValueError):
        kept = {}
    changed = False
    answered: list[Typed] = []
    for found in files:
        try:
            entry = kept.get(str(found.path))
            start, before = 0, []
            if isinstance(entry, dict) and isinstance(entry.get("offset"), int) and 0 < entry["offset"] <= found.size and isinstance(entry.get("answers"), list):
                with found.path.open("rb") as handle:
                    handle.seek(max(0, entry["offset"] - CACHE_TAIL))
                    if hashlib.sha256(handle.read(min(entry["offset"], CACHE_TAIL))).hexdigest() == entry.get("tail"):
                        start, before = entry["offset"], entry["answers"]
            if start != found.size or not before and start == 0:
                offset, fresh = _read_answers(found.path, start, secrets, ConversationIndex.masked)
                with found.path.open("rb") as handle:
                    handle.seek(max(0, offset - CACHE_TAIL))
                    tail = hashlib.sha256(handle.read(min(offset, CACHE_TAIL))).hexdigest()
                before = [*before, *fresh]
                kept[str(found.path)] = {"offset": offset, "tail": tail, "answers": before}
                changed = True
        except OSError:
            continue
        answered += [Typed(at=at, conversation=conversation, to_run="", role="you", text=text, source="claude", project=Path(cwd).name, kind="answer")
                     for at, conversation, text, cwd in before if at >= since]
    if changed:
        try:
            temporary = cache_path.with_name(f"{CARD_CACHE}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps({"version": 1, "files": kept}, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, cache_path)
        except OSError:
            pass
    return answered


def read(folder: Path, since: float, *, cards: bool = True) -> Reading:
    """His messages of editor and terminal sessions from `since` on, oldest first, a replayed turn counted once (`SAME_MOMENT`, `REPLAYED_IN`). An index that cannot be read says why (`Reading.problem`); one never built reads as none (`index_path`)."""
    path, rows, problem = index_path(folder), [], ""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) if path is not None else None
        try:
            rows = [] if db is None else db.execute(
                "SELECT l.at, l.conversation, t.text, c.source, c.project FROM lines l JOIN line_texts t ON t.id = l.id JOIN conversations c ON c.id = l.conversation "
                f"WHERE l.tier = {TYPED} AND l.kind = 'prompt' AND l.at IS NOT NULL AND l.at >= ? AND c.source IN ({','.join('?' * len(SOURCES))}) ORDER BY l.at",
                (since, *SOURCES)).fetchall()
        finally:
            if db is not None:
                db.close()
    except sqlite3.Error as error:
        problem = f"{path.name} cannot be read ({error}): run `galaius agents search` once to rebuild it"
    kept: list[Typed] = []
    long: dict[tuple[str, str], list[Typed]] = {}
    typed_turns = [Typed(at=float(at), conversation=str(conversation), to_run="", role="you", text=his_words(raw), source=str(source), project=str(project or ""))
                   for at, conversation, raw, source, project in rows if isinstance(raw, str) and not raw.lstrip().startswith(NOT_HIS)]
    for typed in sorted([*typed_turns, *(card_answers(folder, since) if cards else ())]):
        text = typed.text
        if not text.strip():
            continue
        if len(text) < REPLAY_FROM:
            kept.append(typed)
        else:
            long.setdefault((typed.source, " ".join(text.split())), []).append(typed)
    for told in long.values():   # `rows` are in time order
        firsts: list[Typed] = []
        for typed in told:
            replay = any(abs(typed.at - other.at) <= (SAME_SESSION_REPLAY if typed.conversation == other.conversation else SAME_MOMENT) for other in firsts)
            if not replay and not any(typed.conversation == other.conversation and abs(typed.at - other.at) <= SAME_SESSION_REPLAY for other in firsts):
                firsts.append(typed)
        kept += firsts[:1] if len({typed.conversation for typed in firsts}) >= REPLAYED_IN else firsts
    return Reading(sorted(kept), problem)


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
