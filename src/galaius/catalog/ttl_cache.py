"""A JSON cache file that ages, shared by every live external source.

The model catalog (prices) and the benchmark board (scores) are the same thing twice: fetch from a
vendor, keep the answer in a fixed file under ``~/.galaius/out/`` that the CLI and the VS Code
extension read DIRECTLY, and be honest about how old it is. They had that logic written out
separately — same TTL constant, same read, same write, same age arithmetic — so a fix to one
(making the write atomic) would silently have missed the other.

The path is fixed rather than following ``GALAIUS_DEBUG_DIR``: two different front ends read it,
so it must not move.

A refresh that did not happen is said, never folded into success: every fetch goes through
:meth:`TTLCache.refetch`, which turns a failure into :class:`RefreshFailed` (why, how old the copy
standing in is, when to try again) and honours a vendor's ``Retry-After`` across every process
sharing the file — Artificial Analysis's free tier is 100 calls a day, and one galaius server per
editor window each refreshing on startup spent it, after which `galaius refresh` still printed
"Refreshed" for a board half a day old.
"""

import json
import os
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict

T = TypeVar("T")

#: Prices and scores both move on the order of days, so half a day keeps a panel responsive
#: without ever being meaningfully behind. One constant, so the two sources cannot drift apart.
TTL_SECONDS = 12 * 60 * 60
#: How long a 429 that names no ``Retry-After`` holds further calls off.
RATE_LIMIT_BACKOFF_SECONDS = 15 * 60
#: How long any other failed fetch (unreachable, an error status, an unusable answer) holds off the
#: reads that refetch on their own: a ranking pass reads a stale cache thousands of times, and with
#: the leaderboards unreachable each read tried the download again (18,606 tries, ~190 s per pass).
FAILURE_BACKOFF_SECONDS = 10 * 60


def describe_span(seconds: float) -> str:
    """Coarsest unit that is not zero: ``45s``, ``7m``, ``8h``, ``3d``."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return f"{max(0, int(seconds))}s"


def describe_age(seconds: float) -> str:
    return "just now" if seconds < 60 else f"{describe_span(seconds)} ago"


class RefreshFailed(Exception):
    """A refresh that did not happen: WHY, until when the source asked us to wait, and when the
    copy still standing in was fetched (0 = nothing cached)."""

    def __init__(self, reason: str, *, retry_at: float | None = None, cached_at: float = 0.0, vendor: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.retry_at = retry_at
        self.cached_at = cached_at
        #: The source itself asked for the wait (``Retry-After``, a 429): it binds an asked-for
        #: refresh too. False: our own back-off after a failure, which only the automatic reads keep.
        self.vendor = vendor

    @classmethod
    def from_response(cls, vendor: str, response: httpx.Response) -> "RefreshFailed":
        wait = _retry_after(response.headers.get("retry-after"))
        if response.status_code == 429:
            return cls(f"rate-limited by {vendor} (HTTP 429)",
                       retry_at=time.time() + (wait if wait is not None else RATE_LIMIT_BACKOFF_SECONDS))
        return cls(f"{vendor} answered HTTP {response.status_code}",
                   retry_at=time.time() + wait if wait is not None else None)

    def describe(self, what: str) -> str:
        """``<reason>[, retry in <span>], <what> from <age>`` — one line a CLI prints verbatim."""
        parts = [self.reason]
        if self.vendor and self.retry_at is not None and self.retry_at > time.time():
            parts.append(f"retry in {describe_span(self.retry_at - time.time() + 1)}")
        parts.append(f"{what} from {describe_age(age_of(self.cached_at))}" if self.cached_at
                     else f"no {what} cached")
        return ", ".join(parts)


def _retry_after(value: str | None) -> float | None:
    """Seconds a ``Retry-After`` header asks for — delta-seconds or an HTTP-date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


class _Built(BaseModel):
    """A value :meth:`TTLCache.derived` built, and the file version and fetch it was built from."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    version: tuple[int, int, int, int, int] | None
    fetched_at: float
    fresh: bool
    value: Any


@dataclass(frozen=True)
class TTLCache:
    """One cache file. Owns where it lives, how it is read, and how it is written."""

    filename: str
    ttl_seconds: float = TTL_SECONDS
    #: Describes this machine only (a local daemon's answers): another machine's copy would be wrong here.
    machine_local: bool = False
    #: How long a failure that names no retry time holds off the automatic reads (`refetch`).
    failure_backoff_seconds: float = FAILURE_BACKOFF_SECONDS
    _built: dict[Callable, _Built] = field(default_factory=dict, init=False, repr=False, compare=False)

    @property
    def path(self) -> Path:
        return Path.home() / ".galaius" / "out" / self.filename

    def read(self) -> dict | None:
        """The stored payload, or None if it is missing or unreadable — never raises."""
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    @property
    def fetched_at(self) -> float:
        """When the cached payload was fetched; 0 when there is none."""
        return self._fetched_at(self.read())

    @staticmethod
    def _fetched_at(raw: dict | None) -> float:
        try:
            return float((raw or {}).get("fetched_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    def is_fresh(self, raw: dict | None) -> bool:
        """Whether a stored payload's fetch is inside this cache's TTL; never fetched: not fresh."""
        return self.fresh_since(self._fetched_at(raw))

    def fresh_since(self, fetched_at: float) -> bool:
        """Whether a fetch at ``fetched_at`` is still inside this cache's TTL; 0 (never): not fresh."""
        return age_of(fetched_at) <= self.ttl_seconds

    def version(self) -> tuple[int, int, int, int, int] | None:
        """The file as last written: device, inode, mtime ns, ctime ns, size; None when there is no file.

        Any process replacing this file must replace it by rename (as :meth:`write` does), so a
        rewrite gets a new inode even inside one tick of the file clock."""
        try:
            info = self.path.stat()
        except OSError:
            return None
        return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size

    def derived(self, build: Callable[[dict | None], T]) -> tuple[bool, T]:
        """Whether the stored copy is fresh, and ``build(self.read())``, built again only when the
        file changes or ages past the TTL.

        One ranking pass reads a cache once per model and criterion term, thousands of times, and
        re-reading plus re-validating the file each time was most of the pass. Held per ``build``:
        pass a module-level function (a fresh lambda per call never hits). The value is shared by
        every caller until the file's :meth:`version` or freshness moves: never mutate it.
        """
        version = self.version()  # before the read: a file replaced in between is built again next call
        held = self._built.get(build)
        if held is not None and held.version == version and held.fresh == self.fresh_since(held.fetched_at):
            return held.fresh, held.value
        raw = self.read()
        held = self._built[build] = _Built(version=version, fetched_at=self._fetched_at(raw), fresh=self.is_fresh(raw),
                                           value=build(raw))
        return held.fresh, held.value

    @property
    def retry_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.retry")

    def deferral(self, *, asked: bool = False) -> RefreshFailed | None:
        """The failure every caller waits out while its window is open: a vendor's for everyone, our
        own back-off only for an automatic read (``asked=False``); an asked-for refresh tries again."""
        try:
            raw = json.loads(self.retry_path.read_text())
            failure = RefreshFailed(str(raw["reason"]), retry_at=float(raw["retry_at"]), vendor=bool(raw.get("vendor", True)))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if failure.retry_at is None or failure.retry_at <= time.time():
            return None
        return failure if failure.vendor or not asked else None

    def due_at(self) -> float:
        """When a refresher should next fetch: the vendor's retry time, else TTL expiry."""
        deferral = self.deferral()
        return deferral.retry_at if deferral is not None else self.fetched_at + self.ttl_seconds

    def refetch(self, vendor: str, fetch: Callable[[], T], *, asked: bool = False) -> T:
        """Run ``fetch`` unless a wait recorded beside the cache is still open (`deferral`).

        Every failure comes out as :class:`RefreshFailed`: an HTTP status keeps its code, a
        transport or parse error keeps its type, and a ``retry_at`` is persisted beside the
        cache so the next process — another editor window, `galaius refresh` — waits too: the
        vendor's when it named one, else :attr:`failure_backoff_seconds` for the automatic reads.
        ``asked``: a person asked for this refresh, so only a vendor's own wait holds it off.
        """
        failure = self.deferral(asked=asked)
        if failure is None:
            try:
                result = fetch()
            except RefreshFailed as error:
                failure = error
            except httpx.HTTPStatusError as error:
                failure = RefreshFailed.from_response(vendor, error.response)
            except httpx.TransportError as error:
                failure = RefreshFailed(f"{vendor} unreachable ({type(error).__name__}: {error})")
            except Exception as error:  # a changed schema must say so, never pass for a refresh
                failure = RefreshFailed(f"{vendor} answer unreadable ({type(error).__name__}: {error})")
            else:
                with suppress(OSError):
                    self.retry_path.unlink(missing_ok=True)
                return result
            if failure.retry_at is None:
                failure.retry_at, failure.vendor = time.time() + self.failure_backoff_seconds, False
            self._write_json(self.retry_path, {"retry_at": failure.retry_at, "reason": failure.reason, "vendor": failure.vendor})
        failure.cached_at = self.fetched_at
        raise failure

    def write(self, payload: dict) -> None:
        """Replace the file ATOMICALLY; a failure to cache never fails the call.

        Several galaius servers run at once (one per editor window) and each refreshes on
        startup, so this path has N writers. A truncating write caught mid-flight leaves a half
        file, and both readers treat a parse error as "no data" — the panel would silently empty
        itself. Writing a temp beside it and renaming makes every reader see one version or the
        other, never a partial one. The pid suffix keeps concurrent writers off each other's temp.
        """
        self._write_json(self.path, payload)

    @staticmethod
    def _write_json(target: Path, payload: dict) -> None:
        temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(payload))
            os.replace(temp, target)  # atomic within a filesystem
        except OSError:
            try:
                temp.unlink()
            except OSError:
                pass  # nothing left to do; a stray temp must not raise into the caller


def age_of(fetched_at: float) -> float:
    """Seconds since a fetch, or infinity when it never happened."""
    return max(0.0, time.time() - fetched_at) if fetched_at else float("inf")
