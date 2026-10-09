"""Live leaderboard tables for the GUI / vision / video / audio benchmarks.

Distinct from :mod:`galaius.benchmark_source`, which serves the overall intelligence ranking
available through the current Artificial Analysis API adapter. That adapter does not expose
per-benchmark MMMU Pro, even though Artificial Analysis publishes MMMU Pro evaluation results on
its public web pages. Runtime benchmark tables therefore come from the exact registered adapters
in :mod:`galaius.benchmarks.upstream` (OpenVLM and friends).

Those upstreams already existed and were only ever run by hand, writing into the PACKAGED data
file — a build step, so an installed copy could never refresh. The panel therefore served a
hand-written offline fallback forever, showing a months-old model as the best at MMMU with its
retrieved date buried in a tooltip. Fetching at runtime into the user's own cache is what makes
the displayed number able to change.
"""

import time

from galaius.benchmarks.published import PublishedTable
from galaius.models import Model
from galaius.ttl_cache import TTL_SECONDS, RefreshFailed, TTLCache

#: Leaderboards move slowly, but the cost of being wrong here is showing a stale model as best,
#: so this shares the catalog's TTL rather than inventing a longer one.
CACHE = TTLCache("benchmark_tables.json", TTL_SECONDS)

_REJECTED_SOURCES = {
    ("mmmu_pro", "https://mmmu-benchmark.github.io/"),
    ("video_mme", "https://video-mme.github.io/"),
}


def cache_path():
    return CACHE.path


def load_tables(*, refresh: bool = False) -> dict[str, PublishedTable]:
    """The freshest per-benchmark tables available, keyed by benchmark id; ``{}`` when none.

    A panel read never raises: one that cannot reach a leaderboard must still render, falling back
    to the packaged snapshot, which carries its own retrieved date. ``refresh=True`` raises
    :class:`RefreshFailed` instead of passing the old tables off as refreshed.
    """
    current, cached = CACHE.derived(_parse)
    if not refresh and current:
        return dict(cached)
    try:
        usable = CACHE.refetch("the benchmark leaderboards", _fetch, asked=refresh)
    except RefreshFailed:
        if refresh:
            raise
        return dict(cached)
    CACHE.write({
        "schema_version": 1,
        "fetched_at": time.time(),
        "tables": {bid: table.model_dump(mode="json") for bid, table in usable.items()},
    })
    return usable


def _fetch() -> dict[str, PublishedTable]:
    # Circular at module level: galaius.config → agents → criteria → this module → upstream →
    # galaius.config.
    from galaius.benchmarks import upstream

    # An upstream that answers with NO entries carries no information — observed live, where the
    # OpenVLM sources returned 200 and zero rows for MMMU and Video-MME. Letting that through
    # would replace a real (if old) packaged snapshot with an empty panel: stale data traded for
    # no data, which is the same mistake pointing the other way.
    usable = {}
    for bid, table in upstream.fetch_all().items():
        if not table.entries:
            continue
        entries = []
        for entry in table.entries:
            model = Model.match_published(entry.model_name)
            entries.append(entry.model_copy(update={
                "model_id": model.id if model else None,
                "status": entry.status if model else "unmapped",
            }))
        usable[bid] = table.model_copy(update={"freshness": "current", "entries": entries})
    if not usable:
        raise RefreshFailed("every benchmark leaderboard answered empty or failed")
    return usable


def _parse(raw: dict | None) -> dict[str, PublishedTable]:
    if not raw or raw.get("schema_version") != 1:
        return {}
    tables = (raw or {}).get("tables") or {}
    freshness = "current" if CACHE.is_fresh(raw) else "stale"
    out = {}
    for bid, payload in tables.items():
        try:
            if any("status" not in entry for entry in payload.get("entries", [])):
                continue
            table = PublishedTable.model_validate(payload)
            if (bid, table.source_url) in _REJECTED_SOURCES:
                continue
            out[bid] = table.model_copy(
                update={"freshness": freshness}
            )
        except Exception:
            continue  # one unreadable table must not blank the rest
    return out
