"""Refresh the live external data the front ends read.

Three sources age the same way: the model catalog (prices, from OpenRouter/litellm), the overall
benchmark board (the current Artificial Analysis API adapter), and the per-benchmark leaderboard
tables (OpenVLM and friends). The current Artificial Analysis API adapter does not expose
per-benchmark MMMU Pro; Artificial Analysis also publishes evaluation results on its public web
pages. Each runtime source keeps a TTL cache on a
fixed path under ``~/.galaius/out/`` that the CLI dashboard AND the VS Code extension READ
directly.

Both loaders shipped with no caller at all, so nothing ever refreshed them on a schedule: the
cache files existed only because a developer had run a loader by hand, and a fresh install had
none. Refreshing is nobody's job unless it is somebody's job, so it is this module's — wired into
the MCP server, the process alive whenever the user is working, and reachable on demand as
``galaius refresh``, which is what the extension calls when its cache has aged out.

The server's refresher fetches a source only when it is DUE — its cache past the TTL, or the
vendor's ``Retry-After`` window over — never on every startup: one server runs per editor window,
and forcing every source at each start spent Artificial Analysis's 100-calls-a-day free tier by
mid-afternoon. A source that fails is retried after its ``Retry-After`` or
:data:`FAILURE_RETRY_SECONDS`, not after the full TTL.

Python is the SOLE WRITER of these files. The extension used to fetch and write the catalog
itself with a narrower schema (no output prices), so whichever side wrote last decided whether
prices existed at all.

Adding a source means one entry in ``SOURCES`` — never a new call site.
"""

import logging
import threading
import time
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict

from galaius import benchmark_source, benchmark_tables, model_catalog
from galaius.ttl_cache import RefreshFailed, TTLCache

_log = logging.getLogger(__name__)

#: How soon a source that failed WITHOUT a vendor-given retry time is tried again.
FAILURE_RETRY_SECONDS = 15 * 60
#: Floor on the refresher's sleep, so a clock oddity can never make it spin.
_MIN_WAIT_SECONDS = 60


class LiveSource(BaseModel):
    """One live external source: how to refresh it and the cache that says when it is due."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    #: What the copy standing in is called in a NOT-refreshed line ("board from 8h ago").
    noun: str
    #: The source's loader; called with ``refresh=True``, raises :class:`RefreshFailed`.
    load: Callable[..., object]
    cache: TTLCache

    def refresh(self) -> str | None:
        """Refresh now; None when it happened, else the one-line reason it did not."""
        try:
            self.load(refresh=True)
        except RefreshFailed as failure:
            return failure.describe(self.noun)
        except Exception as error:  # a loader bug is still a refresh that did not happen
            return f"{type(error).__name__}: {error}"
        return None


SOURCES: Sequence[LiveSource] = (
    LiveSource(name="model catalog", noun="catalog",
               load=model_catalog.load_catalog, cache=model_catalog.CACHE),
    LiveSource(name="benchmark scores", noun="board",
               load=benchmark_source.load_scores, cache=benchmark_source.CACHE),
    LiveSource(name="benchmark tables", noun="tables",
               load=benchmark_tables.load_tables, cache=benchmark_tables.CACHE),
)


def refresh_all(sources: Sequence[LiveSource] | None = None) -> dict[str, str | None]:
    """Refresh every source now; name → None when refreshed, else why not.

    Best-effort per source: one that cannot reach its API keeps serving its stale cache and never
    blocks the others — but it is REPORTED, never counted as refreshed.
    """
    return {source.name: source.refresh() for source in (SOURCES if sources is None else sources)}


def refresh_when_due(stop: threading.Event, sources: Sequence[LiveSource] | None = None) -> None:
    """Refresh each source whenever it falls due, until ``stop`` is set."""
    sources = SOURCES if sources is None else sources
    retry_at: dict[str, float] = {}

    def due(source: LiveSource) -> float:
        return max(source.cache.due_at(), retry_at.get(source.name, 0.0))

    while not stop.is_set():
        for source in sources:
            if due(source) > time.time():
                continue
            if (why := source.refresh()) is None:
                retry_at.pop(source.name, None)
            else:
                _log.warning("%s NOT refreshed: %s", source.name, why)
                retry_at[source.name] = time.time() + FAILURE_RETRY_SECONDS
        wait = min(due(source) for source in sources) - time.time()
        stop.wait(max(_MIN_WAIT_SECONDS, wait))


def refresh_in_background(stop: threading.Event | None = None) -> threading.Thread:
    """Run :func:`refresh_when_due` off the caller's thread — it starts with the MCP server, where
    an HTTP round trip must not delay the first tool call. Daemon, so it never holds up shutdown."""
    thread = threading.Thread(target=refresh_when_due, args=(stop or threading.Event(),),
                              name="galaius-live-sources", daemon=True)
    thread.start()
    return thread
