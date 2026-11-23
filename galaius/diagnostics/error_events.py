"""Errors this computer meets, counted and sent to its server as typed facts — only while the account that owns this
computer accepted that in the conditions (the server decides: it answers `tracking: "off"` and stores nothing).

What leaves: the exception's CLASS, the logger name, up to 12 `module:function:line` frames of galaius's own code, and for an
error logged without an exception its message TEMPLATE (`record.msg`, never the formatted text). An exception's own
message leaves only for the classes whose words Python writes (`TEMPLATE_CLASSES`: `AttributeError`, `TypeError`…); any
other class sends its name and frames alone. Never an argument, a local, a file's content, a prompt or a path: the server
also rewrites every word on arrival.

An error that repeats (one raise 4,000 times an hour) is ONE entry with a count: this process keeps at most `MAX_KEYS`
distinct ones and flushes them at most once a minute. A delivery that did not land is kept as the same batch (same id, so
the server never counts it twice) and retried after 2, 8, then 30 seconds before it is dropped; a server answering `off`,
or refusing the token, silences this module for an hour. Its own failures never feed it."""

import logging
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import uuid4

import httpx

import galaius

logger = logging.getLogger(__name__)

MAX_KEYS = 200
BATCH_EVENTS = 20
FLUSH_SECONDS = 60
SILENCE_SECONDS = 3600
TRIES = (0, 2, 8, 30)
FRAMES = 12
#: The classes whose message is a fixed template with names in it; the same list the server keeps a message for.
TEMPLATE_CLASSES = frozenset({"AttributeError", "NameError", "UnboundLocalError", "ImportError", "ModuleNotFoundError", "TypeError"})
_OWN_CODE = str(Path(galaius.__file__).resolve().parent)
#: Loggers whose records are never fed back: this module's own delivery, and the HTTP client it delivers with.
_SILENT = ("galaius.diagnostics.error_events", "httpx", "httpcore")


class MachineEndpoint(Protocol):
    def endpoint(self, path: str, *, socket: bool = False) -> str: ...

    @property
    def authorization(self) -> dict[str, str]: ...


def _frames(trace) -> list[str]:
    own = [frame for frame in traceback.extract_tb(trace) if frame.filename.startswith(_OWN_CODE)]
    shown = []
    for frame in own[-FRAMES:]:
        module = "galaius." + frame.filename[len(_OWN_CODE):].lstrip("/\\").removesuffix(".py").replace("/", ".").replace("\\", ".")
        shown.append(f"{module}:{frame.name}:{frame.lineno}")
    return shown


class ErrorEvents(logging.Handler):
    """A handler on the `galaius` logger (ERROR and up) that counts, and `deliver` that sends."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self._lock = Lock()
        #: (name, where, top frame, template) -> [count, last seen, the event's frames]
        self._seen: dict[tuple, list] = {}
        self._pending: tuple[str, list[dict]] | None = None
        self._tries = 0
        self._not_before = 0.0
        self._silent_until = 0.0
        self._last_flush = 0.0
        self.dropped = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(_SILENT):
                return
            error = record.exc_info[0] if record.exc_info and record.exc_info[0] else None
            name = error.__name__ if error else "LogTemplate"
            frames = _frames(record.exc_info[2]) if error and record.exc_info else []
            template = (str(record.exc_info[1]) if name in TEMPLATE_CLASSES else "") if error else (record.msg if isinstance(record.msg, str) else "")
            key = (name, record.name, frames[-1] if frames else "", template[:300])
            with self._lock:
                if key in self._seen:
                    self._seen[key][0] += 1
                    self._seen[key][1] = datetime.now(UTC)
                elif len(self._seen) >= MAX_KEYS:
                    self.dropped += 1
                else:
                    self._seen[key] = [1, datetime.now(UTC), frames]
        except Exception:  # noqa: BLE001 - a logging handler never raises into the code that logged
            self.handleError(record)

    def deliver(self, config: MachineEndpoint, now: float | None = None) -> int:
        """One flush attempt: the events the server stored (0 when nothing was due, silent, or not landed yet)."""
        now = time.monotonic() if now is None else now
        if now < self._silent_until or now < self._not_before:
            return 0
        if self._pending is None:
            if now - self._last_flush < FLUSH_SECONDS or not self._seen:
                return 0
            self._pending = self._take()
            self._last_flush = now
        batch_id, events = self._pending
        try:
            answer = httpx.post(config.endpoint("/v1/machine/error-events"), json={"batch_id": batch_id, "events": events}, headers=config.authorization, timeout=5)
        except httpx.HTTPError as error:
            return self._failed(now, f"unreachable ({type(error).__name__})")
        if answer.status_code in (401, 403, 404):
            self._pending, self._silent_until = None, now + SILENCE_SECONDS
            return 0
        if answer.status_code == 429:
            self._not_before = now + min(float(answer.headers.get("Retry-After", "60") or 60), SILENCE_SECONDS)
            return 0
        if answer.status_code != 200:
            return self._failed(now, f"answered {answer.status_code}")
        self._pending, self._tries = None, 0
        try:
            body = answer.json()
        except ValueError:
            return 0
        if body.get("tracking") == "off":
            self._silent_until = now + SILENCE_SECONDS
            return 0
        return int(body.get("stored", 0))

    def _take(self) -> tuple[str, list[dict]]:
        with self._lock:
            seen, self._seen = list(self._seen.items())[:BATCH_EVENTS], dict(list(self._seen.items())[BATCH_EVENTS:])
        events = [{"kind": "pc_exception", "source": "pc", "name": name, "where": where, "message": template or None, "frames": frames,
                   "occurred_at": last.isoformat(), "count": count} for (name, where, _frame, template), (count, last, frames) in seen]
        return str(uuid4()), events

    def _failed(self, now: float, why: str) -> int:
        self._tries += 1
        if self._tries >= len(TRIES):
            self._pending, self._tries = None, 0
            logger.warning("error events dropped after %d tries: %s", len(TRIES) - 1, why)
        else:
            self._not_before = now + TRIES[self._tries]
        return 0


def install() -> ErrorEvents:
    """The one handler of this process, attached to the `galaius` logger (a second call returns the first)."""
    package = logging.getLogger("galaius")
    for handler in package.handlers:
        if isinstance(handler, ErrorEvents):
            return handler
    handler = ErrorEvents()
    package.addHandler(handler)
    return handler
