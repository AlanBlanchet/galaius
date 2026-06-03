"""Fetch published benchmark scores from canonical upstream sources.

Sources (verified live):
  - GUI-Agent/grounding-leaderboard JSON results (drives the JS leaderboards):
      https://raw.githubusercontent.com/GUI-Agent/grounding-leaderboard/main/leaderboard.js                (ScreenSpot-Pro UI)
      https://raw.githubusercontent.com/GUI-Agent/grounding-leaderboard/main/results/screenspot_pro.json   (ScreenSpot-Pro data)
      https://raw.githubusercontent.com/GUI-Agent/grounding-leaderboard/main/leaderboard_screenspot.js     (ScreenSpot v2 UI)
      https://raw.githubusercontent.com/GUI-Agent/grounding-leaderboard/main/results/screenspot_v2.json    (ScreenSpot v2 data)
  - SeeClick README markdown (ScreenSpot v1 table):
      https://raw.githubusercontent.com/njucckevin/SeeClick/main/readme.md

.env is for tests/CLI only — production env vars come from the host.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import ClassVar, Self

import httpx
from pydantic import BaseModel, ConfigDict

from interact.benchmarks.published import PublishedEntry, PublishedTable
from interact.data import PackageData
from interact.dotenv_loader import load_dotenv_for_cli
from interact.models import RegistryMixin

_log = logging.getLogger(__name__)


CACHE_PATH = PackageData.path(PackageData.PUBLISHED)


def load_cache(path: Path = CACHE_PATH) -> dict[str, PublishedTable]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        _log.warning("Cannot read cache %s: %s", path, e)
        return {}
    return {k: PublishedTable.model_validate(v) for k, v in raw.items()}


def save_cache(tables: dict[str, PublishedTable], path: Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: json.loads(v.model_dump_json()) for k, v in tables.items()}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


class UpstreamSource(RegistryMixin, BaseModel):
    """Pydantic-described upstream leaderboard fetcher.

    Subclasses override :meth:`fetch` and register instances in the
    class-level registry via :meth:`_register` (provided by
    :class:`RegistryMixin`).
    """

    model_config = ConfigDict(frozen=False)

    id: str
    name: str
    url: str
    benchmark_id: str

    @classmethod
    def for_benchmark(cls, bid: str) -> list[Self]:
        return [s for s in cls._registry if s.benchmark_id == bid]  # type: ignore[return-value]

    def fetch(self) -> PublishedTable:  # pragma: no cover - abstract
        raise NotImplementedError

    # --- helpers shared by subclasses ---

    def _get(self, timeout: float = 15.0) -> str:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(self.url)
            resp.raise_for_status()
            return resp.text


class GroundingLeaderboardJS(UpstreamSource):
    """Fetches the GUI-Agent grounding-leaderboard JSON result file.

    The official leaderboard JS loads ``results/<bench>.json`` at runtime;
    we hit the JSON directly. Score path:

    - ScreenSpot-Pro: ``data[name]['results']['overall']['avg']``
    - ScreenSpot v2:  ``data[name]['results']['overall_avg']``
    """

    score_path: tuple[str, ...] = ("results", "overall", "avg")
    retrieved: str = ""

    def parse(self, text: str) -> PublishedTable:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("expected top-level JSON object")
        entries: list[PublishedEntry] = []
        for name, payload in data.items():
            if not isinstance(payload, dict):
                continue
            cur: object = payload
            for key in self.score_path:
                if not isinstance(cur, dict) or key not in cur:
                    cur = None
                    break
                cur = cur[key]
            if isinstance(cur, (int, float)):
                entries.append(PublishedEntry(model_name=str(name), score=float(cur)))
        entries.sort(key=lambda e: e.score, reverse=True)
        return PublishedTable(
            source_url=self.url,
            retrieved=self.retrieved or _today(),
            lib_recommendation=entries[0].model_name if entries else None,
            entries=entries,
        )

    def fetch(self) -> PublishedTable:
        return self.parse(self._get())


class SeeClickReadme(UpstreamSource):
    """Parse the ScreenSpot v1 results table out of the SeeClick README."""

    retrieved: str = ""

    _ROW_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"^\|\s*([^|]+?)\s*\|((?:\s*\d+\.?\d*\s*\|){2,})"
    )

    def parse(self, text: str) -> PublishedTable:
        entries: list[PublishedEntry] = []
        in_table = False
        for line in text.splitlines():
            stripped = line.strip()
            if "ScreenSpot" in stripped and "|" in stripped:
                in_table = True
                continue
            if in_table and not stripped.startswith("|"):
                if entries:
                    break
                continue
            m = self._ROW_RE.match(stripped)
            if not m:
                continue
            name = m.group(1).strip()
            cells = [c.strip() for c in m.group(2).split("|") if c.strip()]
            nums = [float(c) for c in cells if _is_number(c)]
            if not nums or not name or name.lower() in {"method", "model"}:
                continue
            # Use last numeric column (typically "Avg" / overall).
            entries.append(PublishedEntry(model_name=name, score=nums[-1] / 100.0))
        entries.sort(key=lambda e: e.score, reverse=True)
        return PublishedTable(
            source_url=self.url,
            retrieved=self.retrieved or _today(),
            lib_recommendation=entries[0].model_name if entries else None,
            entries=entries,
        )

    def fetch(self) -> PublishedTable:
        return self.parse(self._get())


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _today() -> str:
    return date.today().isoformat()


# --- Registry ---------------------------------------------------------------

UpstreamSource._register(
    GroundingLeaderboardJS(
        id="screenspot_pro_gui_agent",
        name="ScreenSpot-Pro (GUI-Agent leaderboard)",
        url="https://raw.githubusercontent.com/GUI-Agent/grounding-leaderboard/main/results/screenspot_pro.json",
        benchmark_id="screenspot_pro",
        score_path=("results", "overall", "avg"),
    )
)
UpstreamSource._register(
    GroundingLeaderboardJS(
        id="screenspot_v2_gui_agent",
        name="ScreenSpot v2 (GUI-Agent leaderboard)",
        url="https://raw.githubusercontent.com/GUI-Agent/grounding-leaderboard/main/results/screenspot_v2.json",
        benchmark_id="screenspot",
        score_path=("results", "overall_avg"),
    )
)
UpstreamSource._register(
    SeeClickReadme(
        id="screenspot_seeclick",
        name="ScreenSpot v1 (SeeClick README)",
        url="https://raw.githubusercontent.com/njucckevin/SeeClick/main/readme.md",
        benchmark_id="screenspot",
    )
)


def fetch_all(benchmark_ids: list[str] | None = None) -> dict[str, PublishedTable]:
    """Fetch the highest-priority upstream source per benchmark.

    First successful fetch per ``benchmark_id`` wins; failures are logged
    and the next source is tried.
    """
    if benchmark_ids is None:
        benchmark_ids = sorted({s.benchmark_id for s in UpstreamSource.registry()})

    results: dict[str, PublishedTable] = {}
    for bid in benchmark_ids:
        sources = UpstreamSource.for_benchmark(bid)
        if not sources:
            _log.warning("no upstream sources for benchmark %r", bid)
            continue
        for src in sources:
            try:
                table = src.fetch()
            except (httpx.HTTPError, ValueError) as e:
                _log.warning("fetch %s failed: %s", src.id, e)
                continue
            results[bid] = table
            print(
                f"  {bid} ← {src.id}: {len(table.entries)} entries",
                file=sys.stderr,
            )
            break
        else:
            print(f"  {bid}: ALL upstream sources failed", file=sys.stderr)
    return results


def _main() -> None:
    load_dotenv_for_cli()
    parser = argparse.ArgumentParser(prog="interact-fetch-upstream")
    parser.add_argument(
        "--benchmarks",
        default=None,
        help="Comma-separated benchmark ids (default: all registered).",
    )
    parser.add_argument("--output", type=Path, default=CACHE_PATH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    bids = (
        [b.strip() for b in args.benchmarks.split(",") if b.strip()]
        if args.benchmarks
        else None
    )
    tables = fetch_all(bids)
    # Merge with existing cache so a single benchmark refresh doesn't wipe peers.
    merged = load_cache(args.output)
    merged.update(tables)
    save_cache(merged, args.output)
    print(f"Wrote {len(tables)} benchmarks → {args.output}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    _main()
