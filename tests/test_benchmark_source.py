"""Benchmark scores must be CURRENT, and must say when they are not.

The panel shipped a snapshot baked into the package. It looked authoritative and quietly aged —
a model listed there was months out of date while the UI presented it as fact. Same defect class
the model catalog already fixed for prices: serving old data offline is fine, serving it as
today's truth is the bug.

Licensing shapes the design: Artificial Analysis's free tier is "internal use only, no
redistribution". So the scores are fetched at RUNTIME with the USER'S OWN key and cached under
their home directory — never vendored into the repo, which would be redistribution.
"""

import json
import re
import time

import pytest

from galaius import benchmark_source as bs
from galaius.data import PackageData
from galaius.criteria import Criteria, CriteriaError
from galaius.benchmarks.upstream import GroundingLeaderboardJS, UpstreamSource, fetch_all
from galaius.benchmarks.published import PublishedEntry, PublishedTable
import galaius.benchmark_tables as benchmark_tables
import galaius.benchmark_tables as bt
import galaius.live_sources as live_sources
from galaius.models import Benchmark, Model, ModelCapability


@pytest.fixture(autouse=True)
def _no_upstream_key(monkeypatch):
    """Fetch must fail from the environment, never because a real key is inherited."""
    monkeypatch.delenv("ARTIFICIAL_ANALYSIS_API_KEY", raising=False)


_PAYLOAD = {
    "data": [
        {
            "name": "Claude Sonnet 5",
            "model_creator": {"name": "Anthropic"},
            "evaluations": {"artificial_analysis_intelligence_index": 61.2},
        },
        {
            "name": "GPT-5.4",
            "model_creator": {"name": "OpenAI"},
            "evaluations": {"artificial_analysis_intelligence_index": 55.0},
        },
        {"name": "No scores", "model_creator": {"name": "X"}, "evaluations": {}},
    ]
}


def test_scores_are_parsed_with_their_creator():
    scores = bs._from_artificial_analysis(_PAYLOAD)
    assert len(scores) == 2, "a model with no evaluations carries no score and is dropped"
    top = scores[0]
    assert top.name == "Claude Sonnet 5" and top.creator == "Anthropic"
    assert top.intelligence == pytest.approx(61.2)


def test_scores_come_back_ranked():
    scores = bs._from_artificial_analysis(_PAYLOAD)
    assert [s.name for s in scores] == ["Claude Sonnet 5", "GPT-5.4"]


def test_verified_aa_fields_become_registry_named_metrics_and_nulls_stay_missing():
    scores = bs._from_artificial_analysis({
        "data": [
            {
                "name": "Coding model",
                "model_creator": {"name": "Provider"},
                "evaluations": {
                    "artificial_analysis_coding_index": 88.0,
                    "scicode": None,
                    "ifbench": 0.72,
                },
            },
            {
                "name": "No coding score",
                "model_creator": {"name": "Provider"},
                "evaluations": {"artificial_analysis_coding_index": None},
            },
        ]
    })
    assert len(scores) == 1
    assert scores[0].metrics == {"coding_index": 88.0, "ifbench": 0.72}
    assert "aa.coding_index" in {benchmark.variable for benchmark in Benchmark.registry()}
    assert "aa.frontier_code" not in {benchmark.variable for benchmark in Benchmark.registry()}


def test_without_a_key_it_says_so_rather_than_pretending():
    board = bs.load_scores()
    assert board.source == "unavailable"
    assert board.is_live is False
    assert "ARTIFICIAL_ANALYSIS_API_KEY" in board.describe()


def test_a_fresh_cache_is_used_and_reported_live(monkeypatch):
    bs.cache_path().parent.mkdir(parents=True, exist_ok=True)
    bs.cache_path().write_text(json.dumps({
        "source": "artificial_analysis",
        "fetched_at": time.time(),
        "scores": [{"name": "Claude Sonnet 5", "creator": "Anthropic", "intelligence": 61.2}],
    }))
    board = bs.load_scores()
    assert board.is_live is True and board.scores[0].name == "Claude Sonnet 5"


def test_a_stale_cache_is_still_served_but_never_called_live(monkeypatch):
    # Offline with old data is useful; offline with old data presented as current is the defect.
    bs.cache_path().parent.mkdir(parents=True, exist_ok=True)
    bs.cache_path().write_text(json.dumps({
        "source": "artificial_analysis",
        "fetched_at": time.time() - (bs.TTL_SECONDS * 3),
        "scores": [{"name": "Old", "creator": "X", "intelligence": 1.0}],
    }))
    board = bs.load_scores()
    assert board.scores and board.is_live is False
    assert "ago" in board.describe()


_ROLE_MODEL_JSON = json.dumps({
    "providers": {
        "anthropic": {
            "envKeys": ["ANTHROPIC_API_KEY"],
            "models": {"claude-4-sonnet-20250514": {
                "input_cost_per_million": 3.0, "output_cost_per_million": 15.0,
            }},
        },
    },
})


@pytest.fixture
def bundled_catalog_after():
    """For a test that replaces the process-wide model registry: the next test gets the bundled
    catalog back (left empty, a later criterion in the same process selected nothing)."""
    yield
    Model.load_registry()


@pytest.mark.parametrize(
    "metric_id",
    [b.id for b in Benchmark.registry() if b.namespace == "aa" and b.source_field],
)
@pytest.mark.usefixtures("bundled_catalog_after")
def test_a_stale_board_still_scores_every_source_mapped_aa_metric(monkeypatch, metric_id):
    """`is_live` used to gate hydration itself, so every source-mapped Artificial Analysis
    metric (coding_index, terminalbench_hard, ...) read as unscored whenever the on-disk board
    aged past TTL_SECONDS — most of the time between refreshes. `aa.intelligence` never had this
    problem (model_catalog.live_scores ranks on the board regardless of age); these metrics must
    not either: a real, if aged, measurement counts."""
    stale = bs.Board(
        scores=[bs.Score(name="Claude 4 Sonnet 20250514", creator="Anthropic", intelligence=50.0,
                          metrics={metric_id: 42.0})],
        source="artificial_analysis", fetched_at=time.time() - bs.TTL_SECONDS * 3,
    )
    assert stale.is_live is False
    monkeypatch.setattr(bs, "load_scores", lambda **_: stale)
    try:
        Model.load_registry(_ROLE_MODEL_JSON)
        model = Model.by_id("claude-4-sonnet-20250514")
        assert model is not None
        benchmark = Benchmark.by_id(metric_id)
        assert benchmark.score_for(model) == pytest.approx(42.0)
        assert Criteria.parse(f"aa.{metric_id} >= 1").choose(available_only=False) == model
    finally:
        Benchmark.by_id(metric_id)._measured.clear()


def test_the_cache_lives_under_home_not_in_the_repo():
    # AA's free tier forbids redistribution, so scores must never be vendored into the package.
    path = str(bs.cache_path())
    assert "/dev/galaius/src" not in path
    assert path.endswith("benchmark_scores.json")


def test_a_corrupt_cache_degrades_to_unavailable():
    bs.cache_path().parent.mkdir(parents=True, exist_ok=True)
    bs.cache_path().write_text("{ not json")
    assert bs.load_scores().source == "unavailable"


def test_artificial_analysis_visual_provenance_names_only_its_actual_metric() -> None:
    benchmarks = json.loads(PackageData.read(PackageData.BENCHMARKS) or "{}")["benchmarks"]
    artificial_analysis = [
        benchmark for benchmark in benchmarks
        if benchmark.get("source") == "Artificial Analysis public evaluation"
    ]
    assert {benchmark["id"] for benchmark in artificial_analysis} == {"mmmu_pro"}
    assert artificial_analysis[0]["name"] == "MMMU Pro"
    assert artificial_analysis[0]["url"] == "https://artificialanalysis.ai/evaluations/mmmu-pro"
    assert artificial_analysis[0]["methodology_url"] == (
        "https://artificialanalysis.ai/methodology/intelligence-benchmarking#mmmu-pro"
    )
    assert artificial_analysis[0]["score_range"] == [0.0, 1.0]
    assert artificial_analysis[0]["higher_is_better"] is True


def test_aa_api_limit_is_not_misstated_as_publication_absence() -> None:
    docs = " ".join((live_sources.__doc__ or "", benchmark_tables.__doc__ or ""))
    assert "AA does not publish" not in docs
    assert "AA publishes text and reasoning benchmarks only" not in docs
    assert re.search(
        r"current Artificial Analysis API adapter does not expose\s+per-benchmark MMMU Pro",
        docs,
    )
    benchmark = Benchmark.by_id("mmmu_pro")
    assert benchmark is not None
    assert benchmark.namespace == "aa"
    assert benchmark.variable == "aa.mmmu_pro"
    assert benchmark.source == "Artificial Analysis public evaluation"
    assert benchmark.source_auth == ""


def test_generated_benchmark_snapshot_preserves_typed_publication_fields() -> None:
    benchmarks = json.loads(PackageData.read(PackageData.BENCHMARKS) or "{}")["benchmarks"]
    screen = next(benchmark for benchmark in benchmarks if benchmark["id"] == "screenspot_pro")
    table = screen["published"]
    assert {"source_url", "retrieved", "freshness"} <= table.keys()
    assert {"model_id", "status", "normalized_score"} <= table["entries"][0].keys()


def test_generated_recommendation_requires_an_authorized_current_entry() -> None:
    benchmarks = json.loads(PackageData.read(PackageData.BENCHMARKS) or "{}")["benchmarks"]
    for benchmark in benchmarks:
        table = benchmark["published"]
        recommendation = table and table.get("lib_recommendation")
        if not recommendation:
            continue
        matching = [entry for entry in table["entries"] if entry["model_name"] == recommendation]
        assert table["freshness"] == "current"
        assert matching and matching[0]["status"] == "eligible"


def test_generated_refresh_capability_is_derived_from_exact_upstream_ids() -> None:
    benchmarks = {
        row["id"]: row
        for row in json.loads(PackageData.read(PackageData.BENCHMARKS) or "{}")["benchmarks"]
    }
    registered = {source.benchmark_id for source in UpstreamSource.registry()}
    for benchmark_id, benchmark in benchmarks.items():
        assert benchmark["refresh_supported"] is (benchmark_id in registered)
    assert benchmarks["mmmu_pro"]["refresh_supported"] is False
    assert benchmarks["mmmu_pro"]["source_auth"] == ""
    assert benchmarks["mmmu_pro"]["requires_auth"] is False


def test_runtime_refresh_uses_exact_registered_benchmark_id(monkeypatch) -> None:
    source = next(source for source in UpstreamSource.registry() if source.benchmark_id == "mmmu")
    table = PublishedTable(
        source_url=source.url, retrieved="2026-09-06",
        entries=[PublishedEntry(model_name="exact", score=0.8, status="eligible")],
    )
    monkeypatch.setattr(GroundingLeaderboardJS, "fetch", lambda self: table)
    monkeypatch.setattr(UpstreamSource, "_registry", [source])
    assert set(fetch_all(["mmmu", "mmmu_pro"])) == {"mmmu"}
    assert set(benchmark_tables.load_tables(refresh=True)) == {"mmmu"}


@pytest.mark.parametrize(
    ("legacy", "replacement"),
    [("aa.mmmu > 0.8", "aa.mmmu_pro"), ("aa.mmbench > 0.8", "oc.mmbench")],
)
def test_semantically_corrected_benchmark_names_fail_with_migration_guidance(
    legacy: str, replacement: str,
) -> None:
    with pytest.raises(CriteriaError, match=replacement.replace(".", r"\.")):
        Criteria.parse(legacy)


# ────────────── Leaderboard tables (formerly test_benchmark_tables.py) ─────────────────────────
#
# The upstreams existed but were only ever run by hand, writing into the PACKAGED data file — so
# an installed copy could never refresh, and the panel served a hand-written fallback forever.


def _table(model="NewModel-9B", score=0.99):
    return PublishedTable(source_url="https://example.test", retrieved="2026-08-18",
                          freshness="current",
                          entries=[PublishedEntry(model_name=model, score=score, status="eligible")])


def _empty():
    return PublishedTable(source_url="https://example.test", retrieved="2026-08-18", entries=[])


def test_a_fetch_is_cached_so_the_panel_does_not_hit_the_network_every_render(monkeypatch):
    calls = []
    monkeypatch.setattr("galaius.benchmarks.upstream.fetch_all",
                        lambda *a, **k: calls.append(1) or {"mmmu": _table()})
    assert bt.load_tables()["mmmu"].entries[0].model_name == "NewModel-9B"
    bt.load_tables()
    assert len(calls) == 1, "a fresh cache must not refetch"


def test_refresh_bypasses_the_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr("galaius.benchmarks.upstream.fetch_all",
                        lambda *a, **k: calls.append(1) or {"mmmu": _table()})
    bt.load_tables()
    bt.load_tables(refresh=True)
    assert len(calls) == 2


def test_an_unreachable_upstream_serves_the_stale_cache_rather_than_nothing(monkeypatch):
    monkeypatch.setattr("galaius.benchmarks.upstream.fetch_all", lambda *a, **k: {"mmmu": _table()})
    bt.load_tables()
    raw = bt.CACHE.read()
    raw["fetched_at"] = time.time() - (bt.TTL_SECONDS * 10)
    bt.CACHE.write(raw)

    def _boom(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr("galaius.benchmarks.upstream.fetch_all", _boom)
    stale = bt.load_tables()["mmmu"]
    assert stale.entries[0].model_name == "NewModel-9B"
    assert stale.freshness == "stale"


def _leaderboards_unreachable(*a, **k):
    import httpx

    raise httpx.ConnectError("no route", request=httpx.Request("GET", "https://example.test"))


@pytest.mark.parametrize("fail", [_leaderboards_unreachable, lambda *a, **k: {"mmmu": _empty()}],
                         ids=["unreachable", "every_board_empty"])
def test_a_failed_download_holds_off_the_ranking_reads_for_ten_minutes(monkeypatch, fail):
    """A ranking pass reads stale tables thousands of times; with the leaderboards unreachable each
    read downloaded again (18,606 tries, ~190 s a pass). One try, then the stale tables for 10 min;
    a person's own refresh still tries at once."""
    monkeypatch.setattr("galaius.benchmarks.upstream.fetch_all", lambda *a, **k: {"mmmu": _table()})
    bt.load_tables()
    raw = bt.CACHE.read()
    raw["fetched_at"] = time.time() - bt.TTL_SECONDS * 2
    bt.CACHE.write(raw)
    calls = []
    monkeypatch.setattr("galaius.benchmarks.upstream.fetch_all", lambda *a, **k: calls.append(1) or fail())

    assert all(bt.load_tables()["mmmu"].freshness == "stale" for _ in range(200))
    assert len(calls) == 1
    with pytest.raises(bt.RefreshFailed):
        bt.load_tables(refresh=True)
    assert len(calls) == 2
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 9 * 60)
    bt.load_tables()
    assert len(calls) == 2
    monkeypatch.setattr(time, "time", lambda: now + 10 * 60 + 1)
    bt.load_tables()
    assert len(calls) == 3


def test_no_cache_and_no_network_is_empty_not_an_exception(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr("galaius.benchmarks.upstream.fetch_all", _boom)
    assert bt.load_tables() == {}


def test_legacy_mmmu_snapshot_is_never_resurrected_from_cache(monkeypatch):
    legacy = PublishedTable(
        source_url="https://mmmu-benchmark.github.io/",
        retrieved="2026-06-07",
        lib_recommendation="GPT-5.4",
        entries=[PublishedEntry(model_name=name, score=score) for name, score in (
            ("GPT-5.4", 0.94), ("Claude Opus 4.7", 0.927),
            ("Gemini 3.1 Pro", 0.84), ("Qwen3.5", 0.77),
        )],
    )
    bt.CACHE.write({"fetched_at": time.time(), "tables": {
        "mmmu_pro": legacy.model_dump(mode="json"),
    }})

    assert "mmmu_pro" not in bt.load_tables()
    assert PublishedTable.load("mmmu_pro", bt.cache_path()) is None


@pytest.mark.parametrize(
    ("benchmark_id", "source_url", "models"),
    [
        ("video_mme", "https://video-mme.github.io/", ("Kimi K2.5", "Gemini 2.5 Pro", "Qwen3.6 Plus")),
        ("mmmu_pro", "https://mmmu-benchmark.github.io/", ("GPT-5.4", "Claude Opus 4.7", "Gemini 3.1 Pro", "Qwen3.5")),
    ],
)
def test_unreceipted_persisted_fallbacks_are_rejected(
    benchmark_id: str, source_url: str, models: tuple[str, ...],
) -> None:
    bt.CACHE.write({"schema_version": 1, "fetched_at": time.time(), "tables": {
        benchmark_id: {"source_url": source_url, "retrieved": "2026-06-07",
                       "entries": [{"model_name": model, "score": 0.8} for model in models]},
    }})
    assert benchmark_id not in bt.load_tables()


def test_mapped_numeric_entry_is_unverified_until_producer_opts_in(monkeypatch) -> None:
    model = Model(id="vendor/exact", provider="vendor", capabilities={ModelCapability.VLM})
    monkeypatch.setattr(Model, "_registry", [model])
    table = PublishedTable(source_url="https://example.test", retrieved="2026-09-06",
                           freshness="current", entries=[PublishedEntry(
                               model_name="exact", model_id=model.id, score=0.9,
                               normalized_score=0.9,
                           )])
    benchmark = next(b for b in __import__("galaius.models", fromlist=["Benchmark"]).Benchmark.registry()
                     if b.id == "mmmu_pro").model_copy(update={"published": table})
    assert table.entries[0].status == "unverified"
    assert benchmark.recommend(available_only=False) == []
    assert benchmark.lib_recommendation_model() is None


def test_cache_requires_current_envelope_and_explicit_entry_authority() -> None:
    old = {"fetched_at": time.time(), "tables": {"x": _table().model_dump(mode="json")}}
    assert bt._parse(old) == {}


def test_one_corrupt_table_does_not_blank_the_others(monkeypatch):
    monkeypatch.setattr("galaius.benchmarks.upstream.fetch_all",
                        lambda *a, **k: {"mmmu": _table(), "video_mme": _table("Other")})
    bt.load_tables()
    raw = bt.CACHE.read()
    raw["tables"]["mmmu"] = {"nonsense": True}
    bt.CACHE.write(raw)
    tables = bt.load_tables()
    assert "mmmu" not in tables and tables["video_mme"].entries[0].model_name == "Other"


def test_an_empty_upstream_table_is_not_cached_over_a_real_one(monkeypatch):
    """OpenVLM sources answered 200 with ZERO entries for MMMU and Video-MME. Caching that would
    replace a real (if old) snapshot with nothing — trading stale data for no data."""
    monkeypatch.setattr("galaius.benchmarks.upstream.fetch_all",
                        lambda *a, **k: {"mmmu": _table(), "video_mme": _empty()})
    tables = bt.load_tables()
    assert "mmmu" in tables
    assert "video_mme" not in tables, "an empty table carries no information; it must not win"


@pytest.mark.parametrize(
    ("published_name", "registered_id"),
    [("Holo1.5", "azure/o1"), ("GPT-5.4", "azure/gpt5")],
)
@pytest.mark.usefixtures("bundled_catalog_after")
def test_published_identity_never_uses_substring_aliases(
    published_name: str, registered_id: str,
) -> None:
    Model._reset()
    Model._register(Model(
        id=registered_id, provider="azure", capabilities={ModelCapability.VLM},
    ))
    assert PublishedTable._fuzzy_match_registered(published_name) is None
    assert Model.match_published(published_name) is None


def test_live_display_table_is_the_same_score_criteria_executes(monkeypatch) -> None:
    monkeypatch.setattr(Model, "_registry", [])
    model = Model(id="vendor/exact-model", provider="vendor", capabilities={ModelCapability.VLM})
    Model._register(model)
    live = _table("exact-model")
    live.entries[0].score = 0.91
    live.entries[0].normalized_score = 0.91
    live.entries[0].model_id = model.id
    monkeypatch.setattr("galaius.criteria.benchmark_tables.load_tables", lambda: {"mmmu_pro": live})

    assert Criteria.parse("aa.mmmu_pro > 0.9").choose(available_only=False) == model


@pytest.mark.parametrize(
    ("freshness", "status", "model_id", "normalized"),
    [("stale", "eligible", "vendor/exact-model", 0.99),
     ("current", "approximate", "vendor/exact-model", 0.99),
     ("current", "missing", "vendor/exact-model", None),
     ("current", "unmapped", None, 0.99)],
)
def test_non_authoritative_scores_never_qualify(
    monkeypatch, freshness: str, status: str, model_id: str | None, normalized: float | None,
) -> None:
    model = Model(id="vendor/exact-model", provider="vendor", capabilities={ModelCapability.VLM})
    monkeypatch.setattr(Model, "_registry", [model])
    table = PublishedTable(
        source_url="https://example.test", retrieved="2026-09-06", freshness=freshness,
        entries=[PublishedEntry(model_name="exact-model", model_id=model_id, score=0.99,
                                normalized_score=normalized, status=status)],
    )
    monkeypatch.setattr("galaius.criteria.benchmark_tables.load_tables", lambda: {"mmmu_pro": table})
    assert Criteria.parse("aa.mmmu_pro > 0.9").choose(available_only=False) is None


def _stale_board(hours: float) -> None:
    bs.cache_path().parent.mkdir(parents=True, exist_ok=True)
    bs.cache_path().write_text(json.dumps({
        "source": "artificial_analysis",
        "fetched_at": time.time() - hours * 3600,
        "scores": [{"name": "Old", "creator": "X", "intelligence": 1.0}],
    }))


def _answer(status: int, headers: dict | None = None, payload: dict | None = None):
    import httpx

    def get(url, **_):
        return httpx.Response(status, headers=headers or {}, json=payload or {},
                              request=httpx.Request("GET", url))
    return get


def _unreachable(url, **_):
    import httpx

    raise httpx.ConnectError("no route", request=httpx.Request("GET", url))


@pytest.mark.parametrize(("get", "reason", "defers"), [
    (_answer(429, {"retry-after": "3600"}), "rate-limited by Artificial Analysis (HTTP 429), retry in 1h", True),
    (_answer(429), "rate-limited by Artificial Analysis (HTTP 429), retry in 15m", True),
    (_answer(503), "Artificial Analysis answered HTTP 503", False),
    (_unreachable, "Artificial Analysis unreachable (ConnectError: no route)", False),
    (_answer(200, payload={"data": []}), "Artificial Analysis returned nothing usable", False),
])
def test_a_refresh_that_did_not_happen_says_why_and_how_old_the_board_is(monkeypatch, get, reason, defers):
    """`galaius refresh` printed "Refreshed" while AA answered 429 and the board stayed 8 h old."""
    from galaius.ttl_cache import RefreshFailed

    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "k")
    _stale_board(8)
    calls = []
    monkeypatch.setattr(bs.httpx, "get", lambda url, **kw: calls.append(url) or get(url, **kw))
    with pytest.raises(RefreshFailed) as failed:
        bs.load_scores(refresh=True)
    assert failed.value.describe("board") == f"{reason}, board from 8h ago"
    with pytest.raises(RefreshFailed):
        bs.load_scores(refresh=True)
    # Retry-After is honoured: inside the window AA is not asked again, by this or any process.
    assert len(calls) == (1 if defers else 2)
    assert bs.load_scores().scores, "a panel read still gets the aged board, never an exception"


def test_no_key_is_a_named_reason_too(monkeypatch):
    from galaius.ttl_cache import RefreshFailed

    with pytest.raises(RefreshFailed, match="no ARTIFICIAL_ANALYSIS_API_KEY set"):
        bs.load_scores(refresh=True)
