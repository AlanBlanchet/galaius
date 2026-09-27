"""Coding benchmarks that exist only on a publisher's pages, carried as curated published tables.

Artificial Analysis serves its Coding Agent Index v1.5 and its Terminal-Bench 4.0 run on its site,
never in its API — a coding role ranking on the API's legacy coding field never sees the newest
models. The tables must bind their as-published row names to registered models through
``Model.match_published`` and rank like any other ``aa.*`` metric.
"""

import pytest

from interact.benchmarks.published import PublishedEntry, PublishedTable
from interact.criteria import Criteria, Variables
from interact.models import Benchmark, Model

from tests.support.models import catalog_of, model

NEWEST = ("claude-opus-5-5", "claude-fable-5-1", "gpt-6-astra")


@pytest.fixture
def newest():
    with catalog_of(*(model(i, provider="p", input_cost=1.0, output_cost=2.0) for i in NEWEST)):
        yield


@pytest.mark.parametrize(("benchmark_id", "expected"), [
    ("coding_agent_index", set(NEWEST)),
    ("terminalbench_4", {"claude-opus-5-5", "gpt-6-astra"}),
])
def test_rows_bind_through_match_published(newest, benchmark_id, expected):
    table = Benchmark.by_id(benchmark_id).published
    assert table is not None and table.freshness == "current"
    for entry in table.entries:
        bound = Model.match_published(entry.model_name)
        # The stored id is the binding match_published makes — never a hand-typed second truth.
        if entry.model_id in NEWEST:
            assert bound is not None and bound.id == entry.model_id, entry.model_name
        assert entry.source_url and entry.harness and entry.version and entry.evidence
        assert entry.status in ("eligible", "unmapped")
    assert {e.model_id for e in table.entries} >= expected


@pytest.mark.parametrize(
    ("variable", "model_id", "score"),
    [
        ("aa.coding_agent_index", "claude-opus-5-5", 66.0),
        ("aa.coding_agent_index", "claude-fable-5-1", 62.0),
        ("aa.coding_agent_index", "gpt-6-astra", 62.0),
        ("aa.terminalbench_4", "claude-opus-5-5", 0.596),
        ("aa.terminalbench_4", "gpt-6-astra", 0.596),
    ],
)
def test_newest_models_are_scored(newest, variable, model_id, score):
    var = Variables.by_name(variable)
    assert var is not None and var.rankable
    assert var.read(Model.by_id(model_id)) == pytest.approx(score)


def test_bare_coding_criterion_ranks_the_leader(newest):
    criteria = Criteria.parse("aa.coding_agent_index and price.in >= 0 and price.out >= 0")
    best = criteria.choose(available_only=False)
    assert best is not None and best.id == "claude-opus-5-5"


def test_a_table_never_mixes_versions():
    with pytest.raises(ValueError, match="mixes benchmark versions"):
        PublishedTable(source_url="x", retrieved="2026-09-25", entries=[
            PublishedEntry(model_name="a", score=62, version="v1.5"),
            PublishedEntry(model_name="b", score=70, version="v1.4"),
        ])


def test_explain_names_the_ranked_score(newest):
    why = Criteria.parse("aa.coding_agent_index").explain(available_only=False)
    assert "claude-opus-5-5 (aa.coding_agent_index 66)" in why
