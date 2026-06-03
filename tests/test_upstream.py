"""Tests for src/interact/benchmarks/upstream.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from interact.benchmarks.published import PublishedTable
from interact.benchmarks.upstream import (
    GroundingLeaderboardJS,
    SeeClickReadme,
    UpstreamSource,
    fetch_all,
    load_cache,
    save_cache,
)


_SAMPLE_SCREENSPOT_PRO_JSON = json.dumps(
    {
        "Model-A": {
            "link": "x",
            "description": "y",
            "results": {"overall": {"avg": 0.72, "icon": 0.5, "text": 0.9}},
        },
        "Model-B": {
            "link": "x",
            "description": "y",
            "results": {"overall": {"avg": 0.30}},
        },
        "Broken": {"results": {"overall": {}}},
    }
)


_SAMPLE_V2_JSON = json.dumps(
    {
        "Holo-7B": {"results": {"overall_avg": 0.91}},
        "Holo-4B": {"results": {"overall_avg": 0.85}},
    }
)


_SAMPLE_SEECLICK_MD = """\
## ScreenSpot Results

| Method | Mobile-Text | Mobile-Icon | Desktop-Text | Web-Text | Avg |
|---|---|---|---|---|---|
| GPT-4V | 22.6 | 24.5 | 20.2 | 9.2 | 16.2 |
| SeeClick | 78.0 | 52.0 | 72.2 | 55.7 | 64.5 |

Some other text.
"""


def test_grounding_leaderboard_js_parse_pro():
    src = GroundingLeaderboardJS(
        id="t",
        name="t",
        url="https://example.invalid/x.json",
        benchmark_id="screenspot_pro",
        score_path=("results", "overall", "avg"),
    )
    table = src.parse(_SAMPLE_SCREENSPOT_PRO_JSON)
    names = [e.model_name for e in table.entries]
    assert names == ["Model-A", "Model-B"]  # broken entry dropped, sorted desc
    assert table.entries[0].score == pytest.approx(0.72)
    assert table.lib_recommendation == "Model-A"


def test_grounding_leaderboard_js_parse_v2():
    src = GroundingLeaderboardJS(
        id="t",
        name="t",
        url="https://example.invalid/x.json",
        benchmark_id="screenspot",
        score_path=("results", "overall_avg"),
    )
    table = src.parse(_SAMPLE_V2_JSON)
    assert [e.model_name for e in table.entries] == ["Holo-7B", "Holo-4B"]


def test_seeclick_readme_parse():
    src = SeeClickReadme(id="t", name="t", url="https://x", benchmark_id="screenspot")
    table = src.parse(_SAMPLE_SEECLICK_MD)
    assert len(table.entries) == 2
    # last column is Avg / 100
    assert table.entries[0].model_name == "SeeClick"
    assert table.entries[0].score == pytest.approx(0.645)


def test_fetch_all_mocks_httpx():
    """fetch_all calls each registered source via its mocked Client."""

    def _fake_get(self, url, **kw):  # noqa: ARG001
        if "screenspot_pro.json" in url:
            body = _SAMPLE_SCREENSPOT_PRO_JSON
        elif "screenspot_v2.json" in url:
            body = _SAMPLE_V2_JSON
        else:
            body = _SAMPLE_SEECLICK_MD
        resp = MagicMock()
        resp.text = body
        resp.raise_for_status = lambda: None
        return resp

    with patch.object(httpx.Client, "get", _fake_get):
        out = fetch_all(["screenspot_pro", "screenspot"])
    assert set(out) == {"screenspot_pro", "screenspot"}
    assert len(out["screenspot_pro"].entries) == 2


def test_fetch_all_failure_logs_and_skips(caplog):
    def _fail(self, url, **kw):  # noqa: ARG001
        raise httpx.ConnectError("nope")

    with patch.object(httpx.Client, "get", _fail):
        out = fetch_all(["screenspot_pro"])
    assert out == {}


def test_load_published_falls_back_when_cache_missing(tmp_path: Path):
    missing = tmp_path / "nope.json"
    table = PublishedTable.load("screenspot_pro", cache_path=missing)
    assert table is not None
    assert any("UI-TARS" in e.model_name for e in table.entries)


def test_load_published_reads_cache(tmp_path: Path):
    cache = tmp_path / "scores.json"
    src = GroundingLeaderboardJS(
        id="t",
        name="t",
        url="https://x",
        benchmark_id="screenspot_pro",
        score_path=("results", "overall", "avg"),
    )
    table = src.parse(_SAMPLE_SCREENSPOT_PRO_JSON)
    save_cache({"screenspot_pro": table}, cache)

    roundtrip = load_cache(cache)
    assert "screenspot_pro" in roundtrip
    loaded = PublishedTable.load("screenspot_pro", cache_path=cache)
    assert loaded is not None
    assert loaded.entries[0].model_name == "Model-A"


def test_upstream_registry_for_benchmark():
    assert UpstreamSource.for_benchmark("screenspot_pro")
    assert UpstreamSource.for_benchmark("nonexistent") == []
