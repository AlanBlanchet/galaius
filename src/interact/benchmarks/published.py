"""Published benchmark leaderboard snapshots.

Hand-curated fallback tables + a cache loader so the registry has data
even without network access on first run. The canonical source of truth
is the upstream JSON produced by ``interact-fetch-upstream``; the
literals below are used only when the cache file is absent.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from interact.data import PackageData

_log = logging.getLogger(__name__)


class PublishedEntry(BaseModel):
    """A single published benchmark score for a model.

    ``model_name`` is the as-published string (may differ from any registered
    ``Model.id``); fuzzy matching to the registry is done by callers.
    """

    model_config = ConfigDict(protected_namespaces=())

    model_name: str
    score: float


class PublishedTable(BaseModel):
    """A leaderboard snapshot from an external source."""

    source_url: str
    retrieved: str  # ISO date the table was copied
    lib_recommendation: str | None = None
    entries: list[PublishedEntry] = Field(default_factory=list)

    @staticmethod
    def default_cache_path() -> Path:
        """Path to the bundled ``published_scores.json`` — the source of truth."""
        return PackageData.path(PackageData.PUBLISHED)

    @classmethod
    def load(
        cls, benchmark_id: str, cache_path: Path | None = None
    ) -> "PublishedTable | None":
        """Return the published table for ``benchmark_id``.

        Order: cache file → frozen fallback → ``None``. Cache schema is
        ``{"<benchmark_id>": <PublishedTable JSON>}``.
        """
        path = cache_path or cls.default_cache_path()
        try:
            if path.exists():
                data = json.loads(path.read_text())
                entry = data.get(benchmark_id)
                if entry:
                    return cls.model_validate(entry)
        except (OSError, ValueError) as e:
            _log.warning("Failed to read published cache %s: %s", path, e)
        return _FALLBACKS.get(benchmark_id)

    @staticmethod
    def _fuzzy_norm(s: str) -> str:
        """Lowercase + drop non-alphanumerics."""
        return "".join(c for c in s.lower() if c.isalnum())

    @classmethod
    def _fuzzy_match_registered(cls, name: str):
        """Match a published model name against ``Model.registry()``.

        Lowercase + strip non-alphanumerics, then substring either direction.
        Both sides must have length >= 4 to avoid trivial collisions like ``o1``.
        Returns the first matching ``Model`` or ``None``.
        """
        from interact.models import Model  # noqa: PLC0415 — circular import

        needle = cls._fuzzy_norm(name)
        if len(needle) < 4:
            return None
        for m in Model.registry():
            bare = cls._fuzzy_norm(m.id.split("/", 1)[-1])
            if len(bare) < 4:
                continue
            if needle in bare or bare in needle:
                return m
        return None

    @classmethod
    def score_for_model(
        cls, model_id: str, benchmark_ids: tuple[str, ...] = ("screenspot_pro", "screenspot")
    ) -> float | None:
        """Best published score across ``benchmark_ids`` whose entry name fuzzy-matches ``model_id``.

        Does NOT require the model to be in ``Model.registry()`` — direct
        name-vs-entry fuzzy match. Returns ``None`` if no entry matches.
        """
        needle = cls._fuzzy_norm(model_id.split("/", 1)[-1])
        if len(needle) < 4:
            return None
        best: float | None = None
        for bid in benchmark_ids:
            table = cls.load(bid)
            if table is None:
                continue
            for entry in table.entries:
                bare = cls._fuzzy_norm(entry.model_name)
                if len(bare) < 4:
                    continue
                if needle in bare or bare in needle:
                    if best is None or entry.score > best:
                        best = entry.score
        return best

    @classmethod
    def best(
        cls, benchmark_id: str, *, available_only: bool = True
    ) -> tuple[str, float] | None:
        """Top published score whose model name fuzzy-matches a registered ``Model.id``.

        When ``available_only`` is True the candidate must also pass
        ``Model.is_available()`` (API key present). Pure lookup; never runs an eval.
        Returns ``(model_id, score)`` or ``None``.
        """
        table = cls.load(benchmark_id)
        if table is None or not table.entries:
            return None
        best: tuple[str, float] | None = None
        for entry in table.entries:
            m = cls._fuzzy_match_registered(entry.model_name)
            if m is None:
                continue
            if available_only and not m.is_available():
                continue
            if best is None or entry.score > best[1]:
                best = (m.id, entry.score)
        return best


# TODO(upstream): cache should be refreshed via `interact-fetch-upstream`;
# this literal is the offline fallback. Source:
# https://github.com/TIGER-AI-Lab/ScreenSpot-Pro retrieved 2026-05-21.
_SCREENSPOT_PRO_FALLBACK = PublishedTable(
    source_url="https://github.com/TIGER-AI-Lab/ScreenSpot-Pro",
    retrieved="2026-05-21",
    lib_recommendation="UI-TARS-1.5-7B",
    entries=[
        PublishedEntry(model_name="UI-TARS-1.5-7B", score=0.616),
        PublishedEntry(model_name="Qwen2.5-VL-72B", score=0.533),
        PublishedEntry(model_name="Qwen2.5-VL-32B", score=0.480),
        PublishedEntry(model_name="Qwen2.5-VL-7B", score=0.268),
        PublishedEntry(model_name="OS-Atlas-7B", score=0.189),
        PublishedEntry(model_name="GPT-5-minimal", score=0.185),
        PublishedEntry(model_name="Claude-3-7-Sonnet", score=0.171),
        PublishedEntry(model_name="GPT-4o", score=0.008),
    ],
)


# TODO(researcher): ScreenSpot-V2 has no canonical machine-readable leaderboard
# (SeeClick README only carries the v1 table). Empty fallback until populated.
_SCREENSPOT_V2_FALLBACK = PublishedTable(
    source_url="https://github.com/njucckevin/SeeClick",
    retrieved="2026-05-21",
    lib_recommendation="",
    entries=[],
)


# Small "never leave the user with nothing" snapshots for the image/video benchmarks, until
# `interact-fetch-upstream` populates the live OpenVLM tables. Approximate top entries from
# public leaderboards (frontier numbers diverge by eval protocol); provenance + retrieved date
# below. These are the offline fallback, NOT the source of truth — the fetch overwrites them.
_MMMU_FALLBACK = PublishedTable(
    source_url="https://mmmu-benchmark.github.io/",
    retrieved="2026-06-07",
    lib_recommendation="GPT-5.4",
    entries=[
        PublishedEntry(model_name="GPT-5.4", score=0.94),
        PublishedEntry(model_name="Claude Opus 4.7", score=0.927),
        PublishedEntry(model_name="Gemini 3.1 Pro", score=0.84),
        PublishedEntry(model_name="Qwen3.5", score=0.77),
    ],
)
_VIDEO_MME_FALLBACK = PublishedTable(
    source_url="https://video-mme.github.io/",
    retrieved="2026-06-07",
    lib_recommendation="Kimi K2.5",
    entries=[
        PublishedEntry(model_name="Kimi K2.5", score=0.874),
        PublishedEntry(model_name="Gemini 2.5 Pro", score=0.848),
        PublishedEntry(model_name="Qwen3.6 Plus", score=0.842),
    ],
)


# MMAU (audio understanding) — single-pass test-mini accuracy from the MMAU paper/leaderboard,
# web-verified 2026-06-24. Single-pass numbers only (agentic chain-of-thought results like
# Step-Audio-R1 are not comparable and excluded). Offline fallback until a live source is wired.
_MMAU_FALLBACK = PublishedTable(
    source_url="https://sakshi113.github.io/mmau_homepage/",
    retrieved="2026-06-24",
    lib_recommendation="Gemini 1.5 Pro",
    entries=[
        PublishedEntry(model_name="Gemini 1.5 Pro", score=0.6615),
        PublishedEntry(model_name="Qwen2.5-Omni-7B", score=0.656),
        PublishedEntry(model_name="GPT-4o-audio", score=0.625),
        PublishedEntry(model_name="Qwen2-Audio-7B-Instruct", score=0.554),
    ],
)


_FALLBACKS: dict[str, PublishedTable] = {
    "screenspot_pro": _SCREENSPOT_PRO_FALLBACK,
    "screenspot": _SCREENSPOT_V2_FALLBACK,
    "mmmu": _MMMU_FALLBACK,
    "video_mme": _VIDEO_MME_FALLBACK,
    "mmau": _MMAU_FALLBACK,
}

