"""A JSON cache file that ages, shared by every live external source.

The model catalog (prices) and the benchmark board (scores) are the same thing twice: fetch from a
vendor, keep the answer in a fixed file under ``~/.interact/out/`` that the CLI and the VS Code
extension read DIRECTLY, and be honest about how old it is. They had that logic written out
separately — same TTL constant, same read, same write, same age arithmetic — so a fix to one
(making the write atomic) would silently have missed the other.

The path is fixed rather than following ``INTERACT_DEBUG_DIR``: two different front ends read it,
so it must not move.

A refresh that did not happen is said, never folded into success: every fetch goes through
:meth:`TTLCache.refetch`, which turns a failure into :class:`RefreshFailed` (why, how old the copy
standing in is, when to try again) and honours a vendor's ``Retry-After`` across every process
sharing the file — Artificial Analysis's free tier is 100 calls a day, and one interact server per
editor window each refreshing on startup spent it, after which `interact refresh` still printed
"Refreshed" for a board half a day old.
"""

import json
import os
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TypeVar

import httpx

T = TypeVar("T")

#: Prices and scores both move on the order of days, so half a day keeps a panel responsive
#: without ever being meaningfully behind. One constant, so the two sources cannot drift apart.
TTL_SECONDS = 12 * 60 * 60
#: How long a 429 that names no ``Retry-After`` holds further calls off.
RATE_LIMIT_BACKOFF_SECONDS = 15 * 60


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

    def __init__(self, reason: str, *, retry_at: float | None = None, cached_at: float = 0.0):
        super().__init__(reason)
        self.reason = reason
        self.retry_at = retry_at
        self.cached_at = cached_at

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
        if self.retry_at is not None and self.retry_at > time.time():
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


@dataclass(frozen=True)
class TTLCache:
    """One cache file. Owns where it lives, how it is read, and how it is written."""

    filename: str
    ttl_seconds: float = TTL_SECONDS

    @property
    def path(self) -> Path:
        return Path.home() / ".interact" / "out" / self.filename

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
        try:
            return float((self.read() or {}).get("fetched_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def retry_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.retry")

    def deferral(self) -> RefreshFailed | None:
        """The failure a vendor told every caller to wait out, while its window is still open."""
        try:
            raw = json.loads(self.retry_path.read_text())
            retry_at, reason = float(raw["retry_at"]), str(raw["reason"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return RefreshFailed(reason, retry_at=retry_at) if retry_at > time.time() else None

    def due_at(self) -> float:
        """When a refresher should next fetch: the vendor's retry time, else TTL expiry."""
        deferral = self.deferral()
        return deferral.retry_at if deferral is not None else self.fetched_at + self.ttl_seconds

    def refetch(self, vendor: str, fetch: Callable[[], T]) -> T:
        """Run ``fetch`` unless the vendor's ``Retry-After`` window is still open.

        Every failure comes out as :class:`RefreshFailed`: an HTTP status keeps its code, a
        transport or parse error keeps its type, and a ``retry_at`` is persisted beside the
        cache so the next process — another editor window, `interact refresh` — waits too.
        """
        failure = self.deferral()
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
            if failure.retry_at is not None:
                self._write_json(self.retry_path, {"retry_at": failure.retry_at, "reason": failure.reason})
        failure.cached_at = self.fetched_at
        raise failure

    def write(self, payload: dict) -> None:
        """Replace the file ATOMICALLY; a failure to cache never fails the call.

        Several interact servers run at once (one per editor window) and each refreshes on
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
