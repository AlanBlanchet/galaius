"""One query engine over this PC's run records (`UserPaths.agents()/<run id>.json`): the read side `galaius conversation` prints.

A debugging agent asks the same few things of the records: what ran in a conversation lately, how a run ended, counts and
spend by status / role / model / login. They are options of one `RunQuery`, never a script per question. The engine reads
the JSON records directly, with no run derived and no server asked: stdlib JSON, no registry import (the registry's own
typed read of ~5,000 records takes seconds). A cache of the fields it reads (`.run-ledger.cache`, beside the records) makes the
next read ~0.3 s. No file time is a filter: `since` judges a run by its own start and end, and a run at work always passes.

A conversation is what the registry calls one: a run's `session_id`, else its parent's, else (a conversation's root run,
which has neither) its own run id. Pointed at a run id, a conversation is that run's.
"""

import contextlib
import json
import math
import os
import re
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from galaius.config.settings import FRUSTRATION
from galaius.host.paths import UserPaths

#: A run record's file name: its full id. Locks, streams, queues and the registry's other files share the folder.
#: The registry's own rule for a run id (`registry._RUN_ID`): launched runs are uuids, a test or a foreign writer may name one otherwise. A file
#: of that shape that is no run record (the quota file, a queue) is told by its content: its `run_id` is not its name.
_ID = r"[A-Za-z0-9._:@+-]{1,160}"
RECORD = re.compile(rf"^(?![.#])(?!.*\.queue\.json$){_ID}\.json$")
#: The statuses the registry keeps a run in for good (`registry.TERMINAL_STATUSES`; a test holds the two equal).
TERMINAL = frozenset({"done", "failed", "cancelled", "interrupted", "stopped"})
#: The statuses a record keeps while its process is expected to run: only these are checked against the process table.
AT_WORK = frozenset({"starting", "running", "waiting"})
#: How long a run may be `starting` with no live process before it reads `gone`: a launch records its process a moment after its record.
LAUNCH_GRACE = 600.0
#: A status the registry once wrote and now reads as `interrupted` (`registry._LEGACY_INTERRUPTED`).
LEGACY_INTERRUPTED = "crashed"
#: What a run still at work reads when its process is gone and its record was never settled.
GONE = "gone"
SAID_CHARS = 240
#: The cache of trimmed records, beside the registry's files: a dot name, which the registry's own listings skip.
CACHE = ".run-ledger.cache"
CACHE_VERSION = 2
#: What the engine reads of a record, by type; everything else (prompts, candidate lists, lifecycle tokens) never enters the cache.
_TEXT = ("run_id", "name", "agent", "status", "kind", "provider", "model", "login", "subscription_id", "parent_run_id", "root_run_id", "session_id",
         "project", "last")
_NUMBER = ("started_at", "finished_at", "cost_usd")
_WHOLE = ("exit_code", "resumed_cuts", "pid", "pid_started")
_SINCE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw])$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class Tokens(BaseModel):
    """What a run's usage record counts (a vendor that counts none leaves it zero)."""

    model_config = ConfigDict(frozen=True)

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __add__(self, other: "Tokens") -> "Tokens":
        return Tokens(**{name: getattr(self, name) + getattr(other, name) for name in type(self).model_fields})


class RunRow(BaseModel):
    """One run as a debugger reads it: who, on what, how it ended. Never its task text beyond one line, never a secret."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    role: str
    status: str
    provider: str = ""
    model: str = ""
    login: str = ""
    parent: str | None = None
    conversation: str = ""
    project: str = ""
    started_at: float = 0.0
    finished_at: float | None = None
    cost_usd: float | None = None
    tokens: Tokens = Tokens()
    #: How the run was cut, when it was: the vendor's reason (`usage_limit`, `login_lost`, ...) or the system's (`out_of_memory`).
    cut: str = ""
    exit_code: int | None = None
    resumed_cuts: int = 0
    #: The vendor's words for a refusal that sent the brief to another model (`relaunched_from`), "" for none.
    relaunched_from: str = ""
    successor: str = ""
    task: str = ""
    said: str = ""

    def activity(self) -> float:
        """The instant this run last moved, as far as its record says: its start or its end, never its file's time (a backfill over the
        registry rewrites every record at once: 5,166 of 5,232 files were younger than 24 h when this was measured)."""
        return max(self.started_at, self.finished_at or 0.0)

    def seconds(self, now: float) -> float:
        """How long it ran, or has been running."""
        return max(0.0, (self.finished_at or now) - self.started_at) if self.started_at else 0.0


#: Who a message in a run's `.messages.jsonl` names as its sender when no run sent it: the person, or a command an agent ran without a run's identity.
OPERATOR = "operator"
#: The id a message the person typed in the web app keeps end to end (`agents send --message-id`: the app's own outbox row, a hyphenated
#: uuid); a command's own messages carry 32 hex digits. The one mark that tells his words from an agent's `agents send`.
#: What the runtime itself sends a run under the same id shape, never his words (`registry.RESUME_BRIEF`).
SYSTEM_MESSAGES = ("Resumed after your run was cut",)   # a test holds it equal to the start of `registry.RESUME_BRIEF`
#: How far before `since` his messages are read, for the ask a message raises again (`FRUSTRATION`'s repeat rule).
REPEAT_LOOKBACK = 14 * 86400.0
WEB_MESSAGE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class Said(BaseModel):
    """One message the person sent a run: when, to which conversation and run, his words, and the frustration wording they hold."""

    model_config = ConfigDict(frozen=True)

    at: float
    conversation: str
    to_run: str
    role: str
    text: str
    #: What in the text reads as frustration (`settings.FRUSTRATION`: his wording, several « ! », an ask he already made earlier in the
    #: conversation), quoted; "" for none.
    frustrated: str = ""


class RunQuery(BaseModel):
    """Every filter the verbs share. None / empty = no filter on that axis."""

    model_config = ConfigDict(frozen=True)

    conversation: str | None = None
    since: float | None = None
    until: float | None = None
    roles: frozenset[str] = frozenset()
    statuses: frozenset[str] = frozenset()
    models: frozenset[str] = frozenset()
    logins: frozenset[str] = frozenset()
    cut: bool = False
    limit: int | None = Field(default=None, ge=1)

    def keeps(self, row: RunRow) -> bool:
        return ((self.since is None or row.activity() >= self.since or row.status in AT_WORK) and (self.until is None or row.started_at <= self.until)
                and (not self.roles or row.role in self.roles)
                and (not self.statuses or row.status in self.statuses)
                and (not self.models or any(want in f"{row.provider}/{row.model}" for want in self.models))
                and (not self.logins or any(row.login.startswith(want) for want in self.logins))
                and (not self.cut or bool(row.cut)))


def _stamp(found: os.stat_result) -> list[int]:
    """What says a record's file is the one cached: its time, size, inode and change time (two rewrites in one clock tick differ in the last two)."""
    return [found.st_mtime_ns, found.st_size, found.st_ino, found.st_ctime_ns]


def parse_since(text: str, *, now: float | None = None) -> float:
    """An instant from `6h` / `30m` / `2d` (that long ago), an ISO date or datetime (local time unless it says a zone), or an epoch."""
    moment = time.time() if now is None else now
    text = text.strip()
    if found := _SINCE.match(text):
        return moment - float(found.group(1)) * _UNIT[found.group(2)]
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        raise ValueError(f"{text!r} is no time: say 6h, 30m, 2d, 2026-11-21 or 2026-11-21T10:30") from None


def own_conversation(environment: Mapping[str, str] | None = None, root: Path | None = None) -> str | None:
    """The conversation the process asking belongs to: its own run's, else the run that launched it, else the session
    the harness set. None for a person at a terminal with none of them (the verbs then say they read every conversation)."""
    environment = os.environ if environment is None else environment
    ledger = RunLedger(root)
    for name in ("GALAIUS_RUN_ID", "GALAIUS_PARENT_RUN_ID"):
        if (run_id := environment.get(name)) and (found := ledger.conversation_of(run_id)) is not None:
            return found
    return environment.get("GALAIUS_SESSION_ID") or None


class RunLedger:
    """The records in one folder, read on demand. One instance per command: its parent cache never outlives it."""

    def __init__(self, root: Path | None = None, *, cache: bool = True) -> None:
        """`cache=False` reads and writes no `.run-ledger.cache`: a folder this process does not own (the app host's mirror of a PC's records)."""
        self.root = UserPaths.agents() if root is None else root
        self._use_cache = cache
        self._raw: dict[str, dict | None] = {}
        self._keys: dict[str, str] = {}
        self._cache: dict[str, list] | None = None
        self._dirty = False
        self._listed: set[str] | None = None

    def status_of(self, raw: dict, *, depth: int = 0) -> str:
        """The status the registry would list the run under (`registry._status_for`), without deriving it: a record at work whose process is
        gone reads `gone` (the registry says `interrupted` and settles it later); a conversation whose process is gone is `interrupted` as
        there; an exit code the record never turned into a status gives its end; a run its provider hosts is as alive as its host; a run
        `starting` with no process recorded is launching for `LAUNCH_GRACE`, then `gone`. One case is not mirrored: a run coming back for a
        queued message (`registry._resuming`, read from the run's queue) reads as its record says."""
        status, kind = str(raw.get("status") or "?"), raw.get("kind") or "process"
        if status == LEGACY_INTERRUPTED:
            return "interrupted"
        if status not in AT_WORK and kind != "conversation":
            return status
        if kind == "provider_child":
            host = self._record(raw.get("parent_run_id") or raw.get("root_run_id") or "") if depth < 5 else None
            return status if host is None or self.status_of(host, depth=depth + 1) in AT_WORK else GONE
        if status == "starting" and not raw.get("pid") and not isinstance(raw.get("exit_code"), int):   # launching: its process is recorded a moment after its record
            return status if time.time() - (raw.get("started_at") or 0.0) < LAUNCH_GRACE else GONE
        if kind == "conversation":
            if status in TERMINAL or status == "waiting" or alive(raw, strict=True):
                return status
            return GONE if status in AT_WORK else "interrupted"
        code = raw.get("exit_code")
        if isinstance(code, int):
            return "stopped" if code == -15 else "done" if code == 0 else "failed"
        return status if alive(raw, strict=False) else GONE

    # --- reading -------------------------------------------------------------------------------------------------

    def _cached(self) -> dict[str, list]:
        """The trimmed records kept from earlier reads: file name -> its file's stamp (`_stamp`) and the trimmed record. A record is parsed again only when its
        file moved; a missing or unreadable cache is no cache (5,000 records parse in ~1 s, from the cache in ~0.2 s)."""
        if self._cache is None and not self._use_cache:
            self._cache = {}
        if self._cache is None:
            try:
                loaded = json.loads((self.root / CACHE).read_text(encoding="utf-8"))
                records = loaded.get("records") if isinstance(loaded, dict) and loaded.get("version") == CACHE_VERSION else None
                self._cache = records if isinstance(records, dict) else {}
            except (OSError, ValueError):
                self._cache = {}
        return self._cache

    def _record(self, run_id: str, stamp: list[int] | None = None) -> dict | None:
        if run_id in self._raw:
            return self._raw[run_id]
        name = f"{run_id}.json"
        try:
            if stamp is None:
                found = os.stat(self.root / name)
                stamp = _stamp(found)
            hit = self._cached().get(name)
            if isinstance(hit, dict) and hit.get("stamp") == stamp and isinstance(hit.get("record"), dict):
                self._raw[run_id] = trimmed = _trim(hit["record"])
                return trimmed
            loaded = json.loads((self.root / name).read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            loaded = None
        trimmed = _trim(loaded) if isinstance(loaded, dict) and loaded.get("run_id") == run_id else None
        if trimmed is not None and stamp is not None:
            self._cached()[name] = {"stamp": stamp, "record": trimmed}
            self._dirty = True
        self._raw[run_id] = trimmed
        return trimmed

    def keep_cache(self) -> None:
        """Write what this instance read afresh, if anything, for the next command: one rename, a failed write loses only the speed-up."""
        if self._cache is None or not self._use_cache:
            return
        target = self.root / CACHE
        if self._listed is not None and (gone := self._cache.keys() - self._listed):   # a run forgotten since leaves nothing of its words behind
            self._cache = {name: kept for name, kept in self._cache.items() if name not in gone}
            self._dirty = True
        if not self._dirty:
            return
        temporary = target.with_name(f"{CACHE}.{os.getpid()}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": CACHE_VERSION, "records": self._cache}, handle, separators=(",", ":"))
            os.replace(temporary, target)
            self._dirty = False
        except OSError:
            with contextlib.suppress(OSError):
                temporary.unlink()

    def conversation_key(self, run_id: str, raw: dict | None = None, *, depth: int = 0) -> str:
        """The conversation of the run `run_id`: its session, else its parent's, else itself."""
        if (known := self._keys.get(run_id)) is not None:
            return known
        raw = raw if raw is not None else self._record(run_id)
        if raw is None:
            return run_id
        parent = raw.get("parent_run_id")
        key = (raw.get("session_id") or (self.conversation_key(parent, depth=depth + 1) if parent and depth < 16 else None)
               or run_id)
        self._keys[run_id] = key
        return key

    def conversation_of(self, ref: str) -> str | None:
        """The conversation a run id (full, or its 8-character prefix) belongs to; None when no record has it."""
        for run_id in self._ids_like(ref):
            return self.conversation_key(run_id)
        return None

    def _ids_like(self, ref: str) -> list[str]:
        if not re.fullmatch(_ID, ref):
            return []
        if (self.root / f"{ref}.json").exists():
            return [ref]
        try:
            return sorted(name.removesuffix(".json") for name in os.listdir(self.root) if name.startswith(ref) and RECORD.match(name))
        except OSError:
            return []

    def resolve_conversation(self, ref: str) -> str:
        """The conversation key `ref` names: a conversation id (or its prefix, found among the runs' conversations), else the
        conversation of the run it is the id of. Refused (`LookupError`) when it names none or several."""
        if (self.root / f"{ref}.json").exists():   # a full run id: no scan of the other records
            return self.conversation_key(ref)
        keys = {self.conversation_key(run_id, raw) for run_id, raw in self._scan() if run_id.startswith(ref) or (raw.get("session_id") or "").startswith(ref)}
        exact = {key for key in keys if key.startswith(ref)}
        runs = {self.conversation_key(run_id) for run_id in self._ids_like(ref)}
        if exact and runs - exact:
            raise LookupError(f"{ref!r} is both a conversation ({', '.join(sorted(k[:12] for k in exact))}) and a run of another ({', '.join(sorted(k[:12] for k in runs - exact))}): give more of it")
        if len(exact) == 1:
            return next(iter(exact))
        if (run_conversation := self.conversation_of(ref)) is not None and not exact:
            return run_conversation
        if len(exact) > 1:
            raise LookupError(f"{ref!r} names {len(exact)} conversations: " + ", ".join(sorted(key[:12] for key in exact)))
        raise UnknownConversation(f"no conversation or run {ref!r} on this computer")

    def _scan(self) -> Iterator[tuple[str, dict]]:
        """Each record of the folder, parsed (from the cache when its file has not moved). A record unreadable (half-written, foreign) is
        skipped. No file time is a filter: a backfill rewrites every record at once (5,166 of 5,232 files were younger than 24 h), and a
        run at work may have written nothing for hours. Having listed the folder, it also tells `keep_cache` which records are gone."""
        try:
            entries = [entry for entry in os.scandir(self.root) if RECORD.match(entry.name)]
        except OSError:
            return
        self._listed = {entry.name for entry in entries}
        for entry in entries:
            try:
                found = entry.stat()
            except OSError:
                continue
            run_id = entry.name.removesuffix(".json")
            if (raw := self._record(run_id, _stamp(found))) is not None:
                yield run_id, raw

    def rows(self, query: RunQuery = RunQuery(), *, now: float | None = None) -> list[RunRow]:
        """The runs `query` keeps, oldest start first, `query.limit` of the newest when it names one."""
        moment = time.time() if now is None else now
        wanted = None if query.conversation is None else self.resolve_conversation(query.conversation)
        found = []
        for run_id, raw in self._scan():
            if query.since and raw.get("status") not in AT_WORK and max(raw.get("started_at") or 0.0, raw.get("finished_at") or 0.0) < query.since:
                continue   # cheap cut before a row is built: only a run at work outlives `since`
            key = self.conversation_key(run_id, raw)
            if wanted is not None and key != wanted:
                continue
            row = _row(run_id, raw, key, self.status_of(raw))
            if query.keeps(row):
                found.append(row)
        found.sort(key=lambda row: (row.started_at, row.run_id))
        self.keep_cache()
        return found if query.limit is None else found[-query.limit:]

    def his_messages(self, *, since: float, conversation: str | None = None, frustrated: bool = False) -> list[Said]:
        """The messages the person typed in the web app since `since`, oldest first, each once (a message in two runs' files shows once).
        A run's `.messages.jsonl` is append only, so a file older than `since` holds none. What he types in an editor or terminal
        session of an agent CLI is in that CLI's own transcript (`galaius agents search`), not here."""
        wanted = None if conversation is None else self.resolve_conversation(conversation)
        found: dict[str, tuple[float, str, str, str, str]] = {}
        try:
            entries = [entry for entry in os.scandir(self.root) if entry.name.endswith(".messages.jsonl") and not entry.name.startswith(".")]
        except OSError:
            return []
        for entry in entries:
            try:
                if entry.stat().st_mtime < since - REPEAT_LOOKBACK:
                    continue
                with open(entry.path, encoding="utf-8", errors="replace") as lines:
                    for line in lines:
                        if OPERATOR not in line:
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(event, dict) or event.get("from_run") != OPERATOR:
                            continue
                        at, text, target = event.get("at"), event.get("text"), event.get("to_run")
                        if (not WEB_MESSAGE_ID.match(str(event.get("event_id") or "")) or not isinstance(at, (int, float)) or at < since - REPEAT_LOOKBACK
                                or not isinstance(text, str) or not isinstance(target, str) or not text.strip() or text.startswith(SYSTEM_MESSAGES)):
                            continue
                        key = self.conversation_key(target)
                        if wanted is not None and key != wanted:
                            continue
                        raw = self._record(target) or {}
                        found.setdefault(str(event["event_id"]), (float(at), key, target, str(raw.get("agent") or raw.get("name") or "?"), text))
            except OSError:
                continue
        said_before: dict[str, list[str]] = {}
        heard = []
        for at, key, target, role, text in sorted(found.values()):
            hit = FRUSTRATION.matched(text, said_before.get(key, ()))
            said_before.setdefault(key, []).append(text)
            if at >= since and (hit or not frustrated):
                heard.append(Said(at=at, conversation=key, to_run=target, role=role, text=text, frustrated=hit or ""))
        return heard

    def last_words(self, run_id: str, *, text_chars: int | None = None) -> "Ending":
        """How run `run_id` ended and what it said last: its last `done` / `error` event (the report an asker reads), else, for a
        run still going, its latest words. Raises `LookupError` for an id no record has."""
        found = self._ids_like(run_id)
        if len(found) != 1:
            raise LookupError(f"no run {run_id!r} on this computer" if not found else f"{run_id!r} names {len(found)} runs: " + ", ".join(rid[:8] for rid in found))
        full, raw = found[0], self._record(found[0])
        if raw is None:
            raise LookupError(f"the record of {run_id!r} is unreadable")
        row = _row(full, raw, self.conversation_key(full, raw), self.status_of(raw))
        kind, text = _closing_event(self.root / f"{full}.jsonl")
        return Ending(run=row, event=kind, text=text if text_chars is None else text[:text_chars], chars=len(text), digest=_digest_said(raw))


class UnknownConversation(LookupError):
    """No conversation or run on this computer has the id asked: unlike an ambiguous or clashing one, a fact a caller may take as « none »."""


class Ending(BaseModel):
    """`RunLedger.last_words`: the run's row, the event the text comes from (`done` / `error` / `text`, "" for none), the text, its full length."""

    run: RunRow
    event: Literal["done", "error", "text", ""]
    text: str
    chars: int
    #: The ending sentence the record kept (`stream_digest.ending_text`), useful when the transcript is gone.
    digest: str = ""


def _closing_event(path: Path) -> tuple[str, str]:
    """The last `done` or `error` event of a transcript with its text, else the last `text` event; ("", "") when it has none."""
    ended, spoke = None, None
    try:
        with path.open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                if '"kind":"done"' in line or '"kind":"error"' in line:
                    ended = line
                elif '"kind":"text"' in line:
                    spoke = line
    except OSError:
        return "", ""
    for line, fallback in ((ended, None), (spoke, "text")):
        if line is None:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        return str(event.get("kind") or fallback), str(event.get("text") or "")
    return "", ""


def _number(value: object) -> float | None:
    """`value` as a finite float; None for anything else (a bool, a text, an integer too large for a float, infinity, NaN)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _counts(value: object) -> dict[str, int]:
    return {name: count for name in Tokens.model_fields if isinstance((count := value.get(name)), int) and not isinstance(count, bool)} if isinstance(value, dict) else {}


def _trim(raw: dict) -> dict:
    """The fields of one record the engine reads, each of the type it is read as (a field of another type is left out), its task cut to its
    first line. The one gate for a record read from its file AND from the cache: a foreign file or a damaged cache entry never reaches a row."""
    trimmed: dict = {name: raw[name] for name in _TEXT if isinstance(raw.get(name), str)}
    trimmed |= {name: number for name in _NUMBER if (number := _number(raw.get(name))) is not None}
    trimmed |= {name: raw[name] for name in _WHOLE if isinstance(raw.get(name), int) and not isinstance(raw.get(name), bool)}
    trimmed["task"] = str(raw.get("task") or "").lstrip().split("\n", 1)[0][:160] if isinstance(raw.get("task"), str) else ""
    if usage := _counts(raw.get("usage")):
        trimmed["usage"] = usage
    if isinstance(digest := raw.get("stream_digest"), dict):
        kept: dict = {name: digest[name][:600 if name == "ending_text" else 300] for name in ("ending_text", "last", "cut") if isinstance(digest.get(name), str)}
        if (cost := _number(digest.get("cost_usd"))) is not None:
            kept["cost_usd"] = cost
        if usage := _counts(digest.get("usage")):
            kept["usage"] = usage
        trimmed["stream_digest"] = kept
    for name, key in (("interruption", "cause"), ("relaunched_from", "said"), ("successor", "run_id")):
        if isinstance(raw.get(name), dict) and isinstance(raw[name].get(key), str):
            trimmed[name] = {key: raw[name][key]}
    return trimmed


def _digest_said(raw: dict) -> str:
    digest = raw.get("stream_digest")
    return _line(digest.get("ending_text") if isinstance(digest, dict) else "", 600)


def _line(text: object, limit: int = SAID_CHARS) -> str:
    """`text` on one line, clipped to `limit` with an ellipsis."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def alive(raw: dict, *, strict: bool) -> bool:
    """A run's recorded process still runs and is still that process (the registry's `process_running`, from /proc, no import). With no pid
    recorded: running when not `strict` (a run still launching), else not (a conversation holds its process from its start).
    Where /proc is absent (macOS, Windows) the process is taken as running: this verb never claims a death it cannot see."""
    pid = raw.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return not strict
    stat = Path(f"/proc/{pid}/stat")
    if not stat.parent.parent.exists():
        return True
    try:
        fields = stat.read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return False
    if fields[0] == "Z":
        return False
    started = raw.get("pid_started")
    return not isinstance(started, int) or int(fields[19]) == started


def _row(run_id: str, raw: dict, conversation: str, status: str) -> RunRow:
    digest = raw.get("stream_digest") if isinstance(raw.get("stream_digest"), dict) else {}
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else digest.get("usage") if isinstance(digest.get("usage"), dict) else {}
    interruption = raw.get("interruption") if isinstance(raw.get("interruption"), dict) else {}
    relaunched = raw.get("relaunched_from") if isinstance(raw.get("relaunched_from"), dict) else {}
    successor = raw.get("successor") if isinstance(raw.get("successor"), dict) else {}
    cost = raw.get("cost_usd") if raw.get("cost_usd") is not None else digest.get("cost_usd")
    cut = digest.get("cut") if isinstance(digest.get("cut"), str) else ""
    return RunRow(
        run_id=run_id, role=str(raw.get("agent") or raw.get("name") or "?"), status=status,
        provider=str(raw.get("provider") or ""), model=str(raw.get("model") or ""), login=str(raw.get("login") or raw.get("subscription_id") or ""),
        parent=raw.get("parent_run_id"), conversation=conversation, project=str(raw.get("project") or ""),
        started_at=float(raw.get("started_at") or 0.0), finished_at=raw.get("finished_at"),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        tokens=Tokens(**{name: _int(usage.get(name)) for name in Tokens.model_fields}),
        cut=cut or str(interruption.get("cause") or ""), exit_code=raw.get("exit_code") if isinstance(raw.get("exit_code"), int) else None,
        resumed_cuts=_int(raw.get("resumed_cuts")),
        relaunched_from=_line(relaunched.get("said")) if relaunched else "",
        successor=str(successor.get("run_id") or "")[:8] if successor else "",
        task=_line(str(raw.get("task") or "").lstrip().split("\n", 1)[0], 160),
        said=_line(digest.get("ending_text") or digest.get("last") or raw.get("last")),
    )


# --- stats ---------------------------------------------------------------------------------------------------------


class Group(BaseModel):
    """What one value of an axis (a role, a model, a login, a status) adds up to."""

    name: str
    runs: int = 0
    cost_usd: float = 0.0
    tokens: Tokens = Tokens()


class Stats(BaseModel):
    """Counts and sums over the runs a query kept, by each axis a debugger cuts by."""

    runs: int
    first_at: float | None
    last_at: float | None
    cost_usd: float
    #: Runs whose spend the record does not state (a subscription's turns carry no price): counted apart, never as zero.
    unpriced: int
    tokens: Tokens
    by_status: list[Group]
    by_role: list[Group]
    by_model: list[Group]
    by_login: list[Group]
    #: Runs cut short by a limit or the system, by reason (`usage_limit`, `out_of_memory`, ...), and runs relaunched on another model.
    cuts: dict[str, int]
    relaunched: int
    resumed: int


def stats(rows: Iterable[RunRow]) -> Stats:
    rows = list(rows)
    axes: dict[str, dict[str, Group]] = {"status": {}, "role": {}, "model": {}, "login": {}}
    total, tokens = 0.0, Tokens()
    for row in rows:
        total += row.cost_usd or 0.0
        tokens += row.tokens
        for axis, name in (("status", row.status), ("role", row.role), ("model", f"{row.provider}/{row.model}".strip("/") or "?"), ("login", (row.login or "default")[:8])):
            group = axes[axis].setdefault(name, Group(name=name))
            axes[axis][name] = group.model_copy(update={"runs": group.runs + 1, "cost_usd": group.cost_usd + (row.cost_usd or 0.0),
                                                        "tokens": group.tokens + row.tokens})
    ranked = lambda axis: sorted(axes[axis].values(), key=lambda group: (-group.runs, group.name))  # noqa: E731
    return Stats(
        runs=len(rows), first_at=min((row.started_at for row in rows if row.started_at), default=None), last_at=max((row.activity() for row in rows), default=None),
        cost_usd=total, unpriced=sum(row.cost_usd is None for row in rows), tokens=tokens,
        by_status=ranked("status"), by_role=ranked("role"), by_model=ranked("model"), by_login=ranked("login"),
        cuts=dict(Counter(row.cut for row in rows if row.cut).most_common()), relaunched=sum(bool(row.relaunched_from) for row in rows),
        resumed=sum(row.resumed_cuts > 0 for row in rows),
    )
