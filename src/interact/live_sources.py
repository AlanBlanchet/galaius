"""Refresh the live external data the front ends read.

Three sources age the same way: the model catalog (prices, from OpenRouter/litellm), the overall
benchmark board (the current Artificial Analysis API adapter), and the per-benchmark leaderboard
tables (OpenVLM and friends). The current Artificial Analysis API adapter does not expose
per-benchmark MMMU Pro; Artificial Analysis also publishes evaluation results on its public web
pages. Each runtime source keeps a TTL cache on a
fixed path under ``~/.interact/out/`` that the CLI dashboard AND the VS Code extension READ
directly.

Both loaders shipped with no caller at all, so nothing ever refreshed them on a schedule: the
cache files existed only because a developer had run a loader by hand, and a fresh install had
none. Refreshing is nobody's job unless it is somebody's job, so it is this module's — wired into
the MCP server, the process alive whenever the user is working, and reachable on demand as
``interact refresh``, which is what the extension calls when its cache has aged out.

Python is the SOLE WRITER of these files. The extension used to fetch and write the catalog
itself with a narrower schema (no output prices), so whichever side wrote last decided whether
prices existed at all.

Adding a third source means one entry in ``SOURCES`` — never a new call site.
"""

import threading
from collections.abc import Callable, Mapping

from interact.benchmark_source import load_scores
from interact.benchmark_tables import load_tables
from interact.model_catalog import load_catalog

#: name → refresh it. Each loader does fresh-cache → fetch → stale-cache internally; `refresh=True`
#: is what makes the call an actual REFRESH rather than a cache read, which matters because this
#: runs inside a server that may live for days.
SOURCES: Mapping[str, Callable[[], object]] = {
    "model catalog": lambda: load_catalog(refresh=True),
    "benchmark scores": lambda: load_scores(refresh=True),
    "benchmark tables": lambda: load_tables(refresh=True),
}


def refresh_all(sources: Mapping[str, Callable[[], object]] | None = None) -> list[str]:
    """Refresh every source; returns the names that succeeded, sorted.

    Best-effort per source: one that cannot reach its API keeps serving its stale cache, which is
    the honest fallback both loaders already implement, and never blocks the others.
    """
    done = []
    for name, refresh in (SOURCES if sources is None else sources).items():
        try:
            refresh()
        except Exception:
            continue  # offline / rate-limited / no key → the stale cache stands
        done.append(name)
    return sorted(done)


def refresh_in_background() -> threading.Thread:
    """Refresh off the caller's thread — this runs at MCP server startup, where two HTTP round
    trips must not delay the first tool call. Daemon, so it never holds up shutdown."""
    thread = threading.Thread(target=refresh_all, name="interact-live-sources", daemon=True)
    thread.start()
    return thread
