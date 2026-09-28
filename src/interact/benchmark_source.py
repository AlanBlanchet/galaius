"""Live benchmark scores from Artificial Analysis, fetched with the user's own key.

The package ships a curated `benchmarks.json` describing WHICH benchmarks matter for GUI
grounding. What it cannot ship is the SCORES: they age immediately, and a stale leaderboard
presented as current is worse than none — a model listed there once was months out of date while
the panel showed it as fact.

Two constraints shape this:

* **Licensing.** Artificial Analysis's free tier is "internal use only, no redistribution", so
  scores are fetched at RUNTIME with the USER'S OWN key and cached under their home directory.
  Vendoring them into the repo would be redistribution; bundling one shared key would both breach
  the terms and leak a credential in an open-source package.
* **Honesty.** The board carries its SOURCE and its AGE, and `is_live` goes false once stale —
  same invariant :mod:`interact.model_catalog` holds for prices. Serving old data offline is
  fine; serving it as today's truth is the bug. `is_live` gates what a UI may call current —
  never whether a real, if aged, score counts toward a criterion: a fresh miss is worse than a
  fifteen-hour-old hit, same reasoning `model_catalog.live_scores` already applies to price.
* **Tiering.** Free-key responses only ever carry three indices (intelligence, coding, agentic);
  every other registered metric (SciCode, IFBench, Terminal-Bench, long-context, omniscience) is
  Pro-only on AA's own wire shape. `_fetch` tries the Pro endpoint first and falls back on
  401/403, so a Pro key lights up every metric with nothing to configure; a free key still gets
  the three it's entitled to, never a guessed or empty stand-in for the rest.
"""

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from interact.models import Benchmark
from interact.ttl_cache import TTL_SECONDS, RefreshFailed, TTLCache, age_of, describe_age

#: Pro tier returns every registered metric (`terminalbench_hard`, `scicode`, `aa_lcr`,
#: `ifbench`, the omniscience trio, ...); Free tier's wire shape only ever carries
#: `artificial_analysis_{intelligence,coding,agentic}_index` — confirmed against AA's own docs
#: 2026-09-25 (`~/.github/research/artificial-analysis-api-evaluation-keys.md`). A free key
#: 401/403s on the Pro path, never silently degrading its answer, so trying Pro first costs a
#: free-tier caller nothing but that one denied call and lets a Pro key light up every metric
#: with no config to flip.
_ENDPOINT_PRO = "https://artificialanalysis.ai/api/v2/language/models"
_ENDPOINT_FREE = "https://artificialanalysis.ai/api/v2/language/models/free"
_KEY_ENV = "ARTIFICIAL_ANALYSIS_API_KEY"
_VENDOR = "Artificial Analysis"


@dataclass
class Score:
    name: str
    creator: str
    #: Artificial Analysis's composite intelligence index. Their number, their methodology —
    #: reported as theirs rather than restated as an interact judgement.
    intelligence: float | None = None
    #: Values keyed by Benchmark.id. The source field → benchmark mapping comes from the
    #: benchmark registry, so an unregistered or unverified API field cannot become a criterion.
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass
class Board:
    scores: list[Score] = field(default_factory=list)
    source: str = "unavailable"
    fetched_at: float = 0.0

    @property
    def age_seconds(self) -> float:
        return age_of(self.fetched_at)

    @property
    def is_live(self) -> bool:
        return self.source == "artificial_analysis" and self.age_seconds <= TTL_SECONDS

    def describe(self) -> str:
        """One line the UI can show verbatim — never a bare number with no provenance."""
        if self.source == "unavailable":
            return (
                f"no benchmark scores — set {_KEY_ENV} to fetch them from Artificial Analysis "
                "(free tier, your own key; scores are not redistributable so interact cannot "
                "ship them)"
            )
        return f"Artificial Analysis · {describe_age(self.age_seconds)}"

    def metric_scores(self, benchmark_id: str) -> dict[str, float]:
        """Return one registered metric's current source rows, without nulls."""
        return {
            score.name: score.metrics[benchmark_id]
            for score in self.scores
            if benchmark_id in score.metrics
        }


#: Never inside the package — these scores are not redistributable (see the licensing note).
CACHE = TTLCache("benchmark_scores.json", TTL_SECONDS)


def cache_path() -> Path:
    return CACHE.path


def _num(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _from_artificial_analysis(payload: dict) -> list[Score]:
    """Their rows → our scores, ranked. A model with no evaluations carries no score and is
    dropped rather than listed at zero, which would rank it below every measured model."""
    rows = payload.get("data") if isinstance(payload, dict) else payload
    out: list[Score] = []
    source_fields = {
        benchmark.source_field: benchmark.id
        for benchmark in Benchmark.registry()
        if benchmark.source_field
    }
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        evals = row.get("evaluations") or {}
        values = {
            benchmark_id: value
            for source_field, benchmark_id in source_fields.items()
            if (value := _num(evals.get(source_field))) is not None
        }
        intelligence = _num(evals.get("artificial_analysis_intelligence_index"))
        if intelligence is not None:
            values.setdefault("intelligence", intelligence)
        if not values:
            continue
        creator = (row.get("model_creator") or {}).get("name") or "unknown"
        out.append(Score(
            name=str(row.get("name") or "?"), creator=str(creator),
            intelligence=intelligence, metrics={k: v for k, v in values.items() if k != "intelligence"},
        ))
    out.sort(key=lambda s: (s.intelligence is not None, s.intelligence or float("-inf")), reverse=True)
    return out


def _read_cache() -> Board | None:
    raw = CACHE.read()
    if raw is None:
        return None
    try:
        scores = [
            Score(
                name=s["name"], creator=s.get("creator", "unknown"),
                intelligence=float(s["intelligence"]) if s.get("intelligence") is not None else None,
                metrics={name: float(value) for name, value in (s.get("metrics") or {}).items()},
            )
            for s in raw.get("scores", [])
        ]
    except (ValueError, KeyError, TypeError):
        return None
    if not scores:
        return None
    return Board(scores=scores, source=str(raw.get("source", "artificial_analysis")),
                 fetched_at=float(raw.get("fetched_at", 0)))


def _write_cache(board: Board) -> None:
    CACHE.write({
        "source": board.source,
        "fetched_at": board.fetched_at,
        "scores": [{"name": s.name, "creator": s.creator, "intelligence": s.intelligence,
                    "metrics": s.metrics}
                   for s in board.scores],
    })


def _fetch() -> Board:
    key = os.environ.get(_KEY_ENV, "").strip()
    if not key:
        raise RefreshFailed(f"no {_KEY_ENV} set")
    for endpoint in (_ENDPOINT_PRO, _ENDPOINT_FREE):
        response = httpx.get(endpoint, headers={"x-api-key": key}, timeout=20)
        if response.status_code in (401, 403):
            continue  # this key isn't entitled to Pro — the free tier still answers
        response.raise_for_status()
        break
    else:
        raise RefreshFailed.from_response(_VENDOR, response)
    scores = _from_artificial_analysis(response.json())
    if not scores:
        raise RefreshFailed(f"{_VENDOR} returned nothing usable")
    return Board(scores=scores, source="artificial_analysis", fetched_at=time.time())


def load_scores(*, refresh: bool = False) -> Board:
    """The current leaderboard: fresh cache, else a fetch, else stale cache, else unavailable.

    A stale board is still RETURNED — old numbers beat no numbers when offline — but `is_live`
    is false and :meth:`Board.describe` says how old it is, so the UI can never present it as
    today's truth. ``refresh=True`` is the one exception: a refresh that did not happen raises
    :class:`RefreshFailed` saying why, instead of handing back the old board as if it were new.
    """
    cached = _read_cache()
    if not refresh and cached is not None and cached.age_seconds <= TTL_SECONDS:
        return cached
    try:
        fetched = CACHE.refetch(_VENDOR, _fetch)
    except RefreshFailed:
        if refresh:
            raise
        return cached if cached is not None else Board()
    _write_cache(fetched)
    return fetched
