"""Live leaderboard tables for the GUI / vision / video / audio benchmarks.

Distinct from :mod:`interact.benchmark_source`, which serves the overall intelligence ranking
available through the current Artificial Analysis API adapter. That adapter does not expose
per-benchmark MMMU Pro, even though Artificial Analysis publishes MMMU Pro evaluation results on
its public web pages. Runtime benchmark tables therefore come from the exact registered adapters
in :mod:`interact.benchmarks.upstream` (OpenVLM and friends).

Those upstreams already existed and were only ever run by hand, writing into the PACKAGED data
file — a build step, so an installed copy could never refresh. The panel therefore served a
hand-written offline fallback forever, showing a months-old model as the best at MMMU with its
retrieved date buried in a tooltip. Fetching at runtime into the user's own cache is what makes
the displayed number able to change.
"""

import time

from interact.benchmarks.published import PublishedTable
from interact.models import Model
from interact.ttl_cache import TTL_SECONDS, TTLCache, age_of

#: Leaderboards move slowly, but the cost of being wrong here is showing a stale model as best,
#: so this shares the catalog's TTL rather than inventing a longer one.
_CACHE = TTLCache("benchmark_tables.json", TTL_SECONDS)

_REJECTED_SOURCES = {
    ("mmmu_pro", "https://mmmu-benchmark.github.io/"),
    ("video_mme", "https://video-mme.github.io/"),
}


def cache_path():
    return _CACHE.path


def load_tables(*, refresh: bool = False) -> dict[str, PublishedTable]:
    """The freshest per-benchmark tables available, keyed by benchmark id; ``{}`` when none.

    Never raises: a panel that cannot reach a leaderboard must still render, falling back to the
    packaged snapshot, which carries its own retrieved date.
    """
    cached = _CACHE.read()
    if not refresh and cached and age_of(float(cached.get("fetched_at", 0))) <= TTL_SECONDS:
        return _parse(cached)

    from interact.benchmarks.upstream import fetch_all

    try:
        fetched = fetch_all()
    except Exception:
        return _parse(cached) if cached else {}
    # An upstream that answers with NO entries carries no information — observed live, where the
    # OpenVLM sources returned 200 and zero rows for MMMU and Video-MME. Letting that through
    # would replace a real (if old) packaged snapshot with an empty panel: stale data traded for
    # no data, which is the same mistake pointing the other way.
    usable = {}
    for bid, table in fetched.items():
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
        return _parse(cached) if cached else {}
    _CACHE.write({
        "schema_version": 1,
        "fetched_at": time.time(),
        "tables": {bid: table.model_dump(mode="json") for bid, table in usable.items()},
    })
    return usable


def _parse(raw: dict | None) -> dict[str, PublishedTable]:
    if not raw or raw.get("schema_version") != 1:
        return {}
    tables = (raw or {}).get("tables") or {}
    freshness = (
        "current"
        if raw and age_of(float(raw.get("fetched_at", 0))) <= TTL_SECONDS
        else "stale"
    )
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
