"""Choosing a model by what it SCORES, and saying who measured it.

"maybe instead of 'intelligence' and things like that we could do a 'aa.intelligence'? aa stands
for artificial analysis... we should turn all the benchmarks and possible comparisons we can make
into variables that are compared, such that if tomorrow a better model shows and respects the
criterias, then it would work!"

A pinned model id is a claim frozen at the moment somebody typed it. A CRITERION is the claim
itself, re-resolved every spawn — and each variable is NAMESPACED BY ITS SOURCE, because a bare
`intelligence` hides who measured it and two leaderboards rarely agree.
"""

import json
from datetime import date, timedelta

import pytest

from galaius.models import Benchmark, Model, ModelCapability
from galaius.criteria import Criteria, CriteriaError, Variables
from galaius.agents.protocol import ConversationRoute, ModelSelection

from tests.support.models import catalog_of


def eyes(**spec) -> Model:
    """A VLM that clears `cap.vlm`; everything else about it is the test's to say."""
    return Model(capabilities={ModelCapability.VLM}, intelligence_score=30.0, **spec)


@pytest.fixture
def registry():
    """Three models with known scores, prices and capabilities.

    They carry no provider key, so every selection passes `available_only=False` — availability
    is its own concern, and mixing it in would make these pass or fail on the machine's env.
    """
    with catalog_of(
        Model(provider="p", id="cheap-eyes", capabilities={ModelCapability.VLM},
              input_cost_per_million=0.5, output_cost_per_million=1.0, intelligence_score=10.0),
        Model(provider="p", id="sharp-eyes", capabilities={ModelCapability.VLM},
              input_cost_per_million=8.0, output_cost_per_million=24.0, intelligence_score=40.0),
        Model(provider="p", id="blind-but-cheap", capabilities=set(),
              input_cost_per_million=0.1, output_cost_per_million=0.2, intelligence_score=20.0),
    ):
        yield


def test_every_variable_says_who_measured_it(registry):
    """The naming rule, stated as a test: no variable without a source namespace."""
    names = {v.name for v in Variables.all()}
    assert "aa.intelligence" in names, "the owner's own example must work"
    assert "price.in" in names and "price.out" in names
    assert "cap.vlm" in names
    # Benchmarks are namespaced by WHERE the number came from, never lumped together.
    assert "gui.screenspot" in names, "a grounding leaderboard is not Artificial Analysis"
    # This checkout publishes MMMU-Pro where the other publishes MMMU — the assertion is that a
    # benchmark carries its SOURCE's namespace, not that any particular table is registered.
    assert any(n.startswith("aa.") and n != "aa.intelligence" for n in names), (
        f"no Artificial Analysis benchmark is registered: {sorted(names)}"
    )
    assert all("." in n for n in names), f"un-namespaced variables: {[n for n in names if '.' not in n]}"


def test_a_new_benchmark_becomes_a_variable_with_no_code_change(registry):
    """"turn all the benchmarks... into variables" — the registry IS the source of truth, so a
    leaderboard added to the data tomorrow is usable in a criterion the same day."""
    added = Benchmark(id="brand_new", name="Brand New", description="d",
                      source="Artificial Analysis", namespace="aa", category="image")
    Benchmark._register(added)
    try:
        assert "aa.brand_new" in {v.name for v in Variables.all()}
        Criteria.parse("aa.brand_new > 0.5")  # parses, because the variable exists
    finally:
        # Remove exactly what was added — resetting the registry would wipe the shipped
        # benchmarks for every test after this one (they are registered at import time and
        # never come back).
        Benchmark.registry().remove(added)


def test_criteria_read_as_english(registry):
    c = Criteria.parse("gui.screenspot > 0.8")
    assert c.terms[0].field == "gui.screenspot" and c.terms[0].op == ">"
    assert Criteria.parse("price.in < 5").terms[0].field == "price.in"
    assert len(Criteria.parse("aa.intelligence >= 30 and price.in < 10").terms) == 2


def test_an_unnamespaced_or_unknown_variable_is_refused_where_it_is_written(registry):
    """A typo must fail LOUDLY at parse time, never silently match nothing at 3am. The bare
    name is refused too: it is exactly the ambiguity the namespace exists to remove."""
    for junk in ["intelligence >= 30", "mmlu-pro-max > 0.8", "aa.nope > 1",
                 "price.in", "price.in <", "> 5", ""]:
        with pytest.raises(CriteriaError):
            Criteria.parse(junk)
    # and the message names what they typed plus what exists
    with pytest.raises(CriteriaError) as e:
        Criteria.parse("intelligence >= 30")
    assert "intelligence" in str(e.value) and "aa.intelligence" in str(e.value)


def test_it_selects_the_cheapest_model_that_clears_the_bar(registry):
    assert Criteria.parse("aa.intelligence >= 30").choose(available_only=False).id == "sharp-eyes"
    assert Criteria.parse("aa.intelligence >= 5").choose(available_only=False).id == "blind-but-cheap"
    assert Criteria.parse("price.in < 1").choose(available_only=False).id == "blind-but-cheap"


def test_bare_intelligence_excludes_snapshot_scores_outside_the_current_board(monkeypatch):
    from galaius import model_catalog

    monkeypatch.setattr(model_catalog, "live_scores", lambda: {"current": 40.0})
    with catalog_of(
        Model(provider="p", id="snapshot", capabilities=set(), intelligence_score=99.0),
        Model(provider="p", id="current", capabilities=set(), intelligence_score=40.0),
    ):
        assert Criteria.parse("aa.intelligence").choose(available_only=False).id == "current"


def test_bare_benchmark_obeys_lower_is_better_direction():
    bench = Benchmark(id="fixture-error", name="Error", description="Lower error wins",
                      namespace="fx", higher_is_better=False)
    Benchmark._register(bench)
    bench._measured.update({"low": 0.0, "high": 0.9})
    try:
        with catalog_of(Model(provider="p", id="low", capabilities=set()),
                        Model(provider="p", id="high", capabilities=set())):
            criterion = Criteria.parse("fx.fixture-error")
            assert criterion.choose(available_only=False).id == "low"
            assert criterion.choose(available_only=False, weights="fx.fixture-error=1").id == "low"
            assert "best measured match is low" in criterion.explain(available_only=False)
    finally:
        Benchmark.registry().remove(bench)


def test_a_bare_benchmark_ranks_measured_models_after_filters(registry):
    bench = Benchmark.by_id("coding_index")
    assert bench is not None
    models = (
        Model(provider="p", id="cheap-low", capabilities={ModelCapability.LLM},
              input_cost_per_million=0.1, output_cost_per_million=0.1),
        Model(provider="p", id="expensive-high", capabilities={ModelCapability.LLM},
              input_cost_per_million=5.0, output_cost_per_million=5.0),
    )
    bench._measured.update({"cheap-low": 20.0, "expensive-high": 90.0})
    try:
        criterion = Criteria.parse("cap.llm and aa.coding_index")
        assert criterion.choose(available_only=False, candidates=list(models)).id == "expensive-high"
    finally:
        bench._measured.clear()


def test_a_bare_benchmark_excludes_unmeasured_and_provider_ineligible_models(registry):
    bench = Benchmark.by_id("coding_index")
    assert bench is not None
    models = (
        Model(provider="allowed", id="allowed-low", capabilities={ModelCapability.LLM},
              input_cost_per_million=1.0),
        Model(provider="blocked", id="blocked-high", capabilities={ModelCapability.LLM},
              input_cost_per_million=0.1),
        Model(provider="allowed", id="missing", capabilities={ModelCapability.LLM},
              input_cost_per_million=0.01),
    )
    bench._measured.update({"allowed-low": 20.0, "blocked-high": 99.0})
    try:
        criterion = Criteria.parse("aa.coding_index")
        chosen = criterion.choose(
            available_only=False,
            runnable=lambda model: model.provider == "allowed",
            candidates=list(models),
        )
        assert chosen is not None and chosen.id == "allowed-low"
        assert [model.id for model in criterion.qualifying(
            available_only=False, candidates=list(models)
        )] == ["blocked-high", "allowed-low"]
    finally:
        bench._measured.clear()


def test_a_benchmark_threshold_can_be_combined_with_descending_ranking(registry):
    bench = Benchmark.by_id("coding_index")
    assert bench is not None
    models = (
        Model(provider="p", id="cheap-pass", capabilities={ModelCapability.LLM},
              input_cost_per_million=0.1, output_cost_per_million=0.1),
        Model(provider="p", id="expensive-best", capabilities={ModelCapability.LLM},
              input_cost_per_million=8.0, output_cost_per_million=8.0),
        Model(provider="p", id="cheap-fail", capabilities={ModelCapability.LLM},
              input_cost_per_million=0.01, output_cost_per_million=0.01),
    )
    bench._measured.update({"cheap-pass": 60.0, "expensive-best": 90.0, "cheap-fail": 40.0})
    try:
        criterion = Criteria.parse("cap.llm and aa.coding_index >= 50 and aa.coding_index")
        assert criterion.choose(available_only=False, candidates=list(models)).id == "expensive-best"
    finally:
        bench._measured.clear()


def test_explicit_weights_still_normalize_source_metrics_before_scoring(registry):
    coding = Benchmark.by_id("coding_index")
    ifbench = Benchmark.by_id("ifbench")
    assert coding is not None and ifbench is not None
    models = [
        Model(provider="p", id="coding-winner", capabilities={ModelCapability.LLM}),
        Model(provider="p", id="ifbench-winner", capabilities={ModelCapability.LLM}),
    ]
    coding._measured.update({"coding-winner": 90.0, "ifbench-winner": 20.0})
    ifbench._measured.update({"coding-winner": 0.2, "ifbench-winner": 0.9})
    try:
        criterion = Criteria.parse("cap.llm")
        chosen = criterion.choose(
            available_only=False,
            weights="aa.coding_index=1, aa.ifbench=1",
            candidates=models,
        )
        assert chosen is not None and chosen.id == "coding-winner"
    finally:
        coding._measured.clear()
        ifbench._measured.clear()


def test_route_resolver_uses_the_shared_metric_chooser(registry):
    bench = Benchmark.by_id("coding_index")
    assert bench is not None
    models = [
        Model(provider="p", id="route-cheap", capabilities={ModelCapability.LLM},
              input_cost_per_million=0.1),
        Model(provider="p", id="route-best", capabilities={ModelCapability.LLM},
              input_cost_per_million=5.0),
    ]
    bench._measured.update({"route-cheap": 20.0, "route-best": 80.0})
    route = ConversationRoute(
        id="p:api", provider="p", connection="api", label="p API", availability="available",
        charge_path="metered_api", cost_certainty="known", billing_note="test",
        models=models, cataloged_at=0.0,
    )
    try:
        criterion = Criteria.parse("cap.llm and aa.coding_index")
        expected = criterion.choose(available_only=False, candidates=models)
        assert route.resolve(ModelSelection(criterion=str(criterion))) == expected
    finally:
        bench._measured.clear()


def test_a_capability_can_be_demanded_by_name(registry):
    """A visual judge that cannot see is not a cheaper visual judge, it is a wrong answer."""
    chosen = Criteria.parse("cap.vlm and price.in < 100").choose(available_only=False)
    assert chosen is not None and chosen.id == "cheap-eyes"


def test_unknown_never_qualifies_and_nothing_matching_refuses_loudly(registry):
    assert Criteria.parse("aa.intelligence > 999").choose(available_only=False) is None
    # No published grounding score is attached to these three: unknown is not good.
    assert Criteria.parse("gui.screenspot > 0.1").choose(available_only=False) is None
    why = Criteria.parse("aa.intelligence > 999 and price.in < 1").explain(available_only=False)
    assert "aa.intelligence" in why and "999" in why


def test_a_criterion_where_a_model_id_goes_resolves_before_the_cli_sees_it(registry):
    from galaius.agents.run import resolve_model

    _, name = resolve_model("claude-sonnet-5", {})
    assert name == "claude-sonnet-5", "an id passes through untouched"
    assert resolve_model("gpt-4.1", {})[1] == "gpt-4.1", "a dotted id is still an id, not a variable"
    _, name = resolve_model("aa.intelligence >= 30", {}, available_only=False)
    assert name == "sharp-eyes", "a criterion must arrive at the CLI as a real model"


def test_a_criterion_that_matches_nothing_refuses_loudly(registry):
    from galaius.agents.run import ModelUnavailable, resolve_model

    with pytest.raises(ModelUnavailable) as e:
        resolve_model("aa.intelligence > 999", {}, available_only=False)
    assert "999" in str(e.value)


def test_the_catalog_loads_itself_in_a_process_that_never_asked_for_it():
    """The CLI spawn path never imported `galaius.runtime`, so the catalog was EMPTY there and
    every criterion died with "no model is configured at all" — while these tests passed, because
    conftest loads the catalog for them. A fresh interpreter is the only faithful stand-in for
    the CLI process: nothing here may pre-load anything."""
    import subprocess
    import sys

    code = (
        "from galaius.agents.run import resolve_model; "
        "print(resolve_model('aa.intelligence >= 30', {}, available_only=False)[1])"
    )
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-600:]
    assert run.stdout.strip(), "a criterion must resolve in a process that never loaded the catalog"


def test_a_criterion_only_picks_what_the_vendor_cli_can_run(monkeypatch):
    """`@eyes` resolved to a Gemini id and was handed to the claude binary — structurally
    unrunnable. The pool a vendor CLI chooses from is what THAT CLI can be pointed at: its own
    vendor's models through its own login, or a model galaius can route it to (ollama, when its
    key or daemon is there) — never a cheaper model from a provider it cannot reach."""
    from galaius.agents.providers import provider_for
    from galaius.agents.run import ModelUnavailable, resolve_model

    claude, codex = provider_for("claude"), provider_for("codex")
    with catalog_of(
        eyes(provider="gemini", id="cheap-eyes", input_cost_per_million=0.1, output_cost_per_million=0.2),
        eyes(provider="ollama", id="local-eyes", input_cost_per_million=0.2, output_cost_per_million=0.4),
        eyes(provider="anthropic", id="claude-eyes", input_cost_per_million=3.0, output_cost_per_million=15.0),
    ):
        monkeypatch.setattr(Model, "is_available", lambda self: False)  # no key, no daemon anywhere
        routed, chosen = resolve_model("cap.vlm", {}, provider=claude)
        assert (routed, chosen) == ({}, "claude-eyes"), "Claude runs through the CLI's own login — no key"

        monkeypatch.setattr(Model, "is_available", lambda self: self.provider == "ollama")
        routed, chosen = resolve_model("cap.vlm", {}, provider=claude)
        assert chosen == "local-eyes" and routed.get("ANTHROPIC_BASE_URL"), "a routed model arrives WITH its endpoint"

        with pytest.raises(ModelUnavailable) as e:
            resolve_model("cap.vlm", {}, provider=codex)
        assert "codex" in str(e.value), "say WHICH CLI has nothing to run"


def test_a_model_states_its_competence_with_the_source_and_the_field_it_ranks_in():
    """"Models should also spell out their intelligence score, such that we can compare the most
    competents and trust one agent more than another in some situations (isn't absolute)."

    So the score is never a bare number: it carries WHO measured it and WHERE it sits among the
    models that carry the same measure — a comparison, explicitly not a verdict. A model nobody
    scored says so rather than reading as a zero.
    """
    from galaius.models import Model

    with catalog_of(
        Model(id="big", provider="anthropic", capabilities=set(), intelligence_score=60.0),
        Model(id="mid", provider="anthropic", capabilities=set(), intelligence_score=37.1),
        Model(id="small", provider="anthropic", capabilities=set(), intelligence_score=20.0),
        Model(id="unmeasured", provider="anthropic", capabilities=set()),
    ):
        assert Model.by_id("mid").competence() == "aa.intelligence 37.1 — 2nd of 3 scored (Artificial Analysis)"
        assert Model.by_id("big").competence() == "aa.intelligence 60.0 — 1st of 3 scored (Artificial Analysis)"
        assert Model.by_id("unmeasured").competence() == "aa.intelligence — not scored"


def test_a_bar_can_be_the_boards_own_shape_so_it_never_goes_stale(registry):
    """`90%` is a bar written as a POSITION on the board instead of a number somebody typed.

    A typed number is itself a claim frozen on its day: `aa.intelligence > 40` meant "the very
    top" in 2025 and means "the middle" as soon as the board moves — so a criterion built out of
    them decays exactly the way a pinned model id does, only more quietly. A percentile says the
    thing that was actually meant, "the top tenth of everything measured", and re-reads the board
    every time it is asked.
    """
    top = Criteria.parse("aa.intelligence >= 90%")
    assert str(top) == "aa.intelligence >= 90%", "it keeps what was written, not today's number"
    assert top.choose(available_only=False).id == "sharp-eyes"

    # The board moves — a stronger model ships — and the SAME criterion follows it. This is the
    # whole difference from a number: nothing had to be re-typed for the bar to still mean "top".
    with catalog_of(
        Model(provider="p", id="sharp-eyes", capabilities=set(), intelligence_score=40.0,
              input_cost_per_million=8.0),
        Model(provider="p", id="newcomer", capabilities=set(), intelligence_score=90.0,
              input_cost_per_million=100.0),
    ):
        assert top.choose(available_only=False).id == "newcomer"

    with pytest.raises(CriteriaError, match="between 0 and 100"):
        Criteria.parse("aa.intelligence > 150%")


def test_a_percentile_bar_reads_the_whole_board_never_what_you_hold_a_key_for(registry, monkeypatch):
    """"Top tenth" must not quietly become "best of the three I can reach".

    Availability decides who is a CANDIDATE; it must not decide where the BAR is, or a criterion
    flatters whatever keys happen to be present — the same mistake as ranking a model against its
    credential scope instead of against everyone measured.
    """
    monkeypatch.setenv("FIXTURE_KEY", "set")
    Model._provider_keys["p"] = ["FIXTURE_KEY"]
    with catalog_of(
        Model(provider="p", id="reachable-and-weak", capabilities=set(), intelligence_score=10.0,
              input_cost_per_million=0.5),
        Model(provider="nokey", id="unreachable-and-strong", capabilities=set(),
              intelligence_score=99.0, input_cost_per_million=1.0),
    ):
        Model._provider_keys["p"] = ["FIXTURE_KEY"]
        top = Criteria.parse("aa.intelligence >= 90%")
        assert top.choose() is None, "the weak model is reachable, but it is not the board's top"
        bar, = top.against_the_board()
        assert bar.value == 99.0, "the bar came off the whole board, not the reachable subset"
        # And the message says where the bar came from, or "nobody clears 99" reads as arbitrary.
        assert "90% of 2 scored" in top.explain() and "99" in top.explain()


def test_the_pool_holds_what_the_BOARD_ranks_not_only_what_the_snapshot_shipped():
    """A criterion can only ever pick a model the catalog knows EXISTS.

    The bundled `models.json` is a snapshot; the board on disk ranks 450 models and the snapshot
    covered 86 of them, so the entire top of the leaderboard the panel displays was invisible to
    the thing that chooses. "Resolve a model from the constraints" cannot mean anything while the
    constraint is evaluated against a fifth of the field — a criterion would confidently answer
    with the best model of 2025 and look like it worked.

    litellm already ships prices for these and is already a dependency: same live-merge shape as
    the Ollama pass, which exists for the same reason — a bundled catalog structurally cannot know.
    """
    with catalog_of(
        Model(provider="p", id="old-timer", capabilities=set(), intelligence_score=10.0,
              input_cost_per_million=1.0),
    ):
        added = Model.merge_ranked(
            scores={"old-timer": 10.0, "newcomer-5": 90.0, "unpriced-5": 80.0},
            priced={
                # The same model under three spellings — the regional ones cost more, and the
                # bare vendor id is the one worth registering.
                "au.vendor.newcomer-5": {"litellm_provider": "vendor",
                                         "input_cost_per_token": 2.2e-6},
                "vendor.newcomer-5": {"litellm_provider": "vendor", "input_cost_per_token": 2.1e-6},
                "newcomer-5": {"litellm_provider": "vendor", "input_cost_per_token": 2e-6,
                               "output_cost_per_token": 1e-5},
                # Already in the catalog: never registered twice under another spelling.
                "vendor.old-timer": {"litellm_provider": "vendor", "input_cost_per_token": 1e-6},
                # Ranked but unpriced: it is still a candidate. The missing price must sort it
                # after priced rows, never erase it from a quality-ranked role.
                "unpriced-5": {"litellm_provider": "vendor"},
            },
        )
        assert added == 2, "both genuinely new ranked models, including the unpriced one"
        by_id = {m.id: m for m in Model.catalog()}
        assert "newcomer-5" in by_id, "the canonical spelling, not the regional one"
        assert "unpriced-5" in by_id, "a missing price must not hide a newly ranked model"
        assert "au.vendor.newcomer-5" not in by_id and "vendor.newcomer-5" not in by_id
        assert by_id["newcomer-5"].input_cost_per_million == 2.0
        assert by_id["newcomer-5"].intelligence_score == 90.0, "carrying what the board measures"
        assert by_id["unpriced-5"].input_cost_per_million is None
        # And the criterion now reaches it — the whole point.
        assert Criteria.parse("aa.intelligence > 50").choose(available_only=False).id == "newcomer-5"


def test_nonnegative_price_guards_do_not_hide_a_scored_unpriced_model(registry, monkeypatch):
    """Role defaults use nonnegative price terms as presence guards, not a requirement that a
    new provider has already published both prices. Explicit upper bounds still reject unknown
    prices; only the zero lower-bound guard is open-ended."""
    monkeypatch.setattr(
        "galaius.criteria.model_catalog.live_scores",
        lambda: {"priced": 10.0, "new-and-unpriced": 90.0},
    )
    with catalog_of(
        Model(provider="p", id="priced", capabilities=set(), intelligence_score=10.0,
              input_cost_per_million=1.0, output_cost_per_million=1.0),
        Model(provider="p", id="new-and-unpriced", capabilities=set(), intelligence_score=90.0),
    ):
        criterion = Criteria.parse("aa.intelligence and price.in >= 0 and price.out >= 0")
        assert criterion.choose(available_only=False).id == "new-and-unpriced"
        assert Criteria.parse("price.in < 1").choose(
            available_only=False, candidates=[Model(provider="p", id="unknown", capabilities=set())]
        ) is None


def test_pricing_the_ranked_models_is_cached_so_no_command_pays_for_litellm(monkeypatch, tmp_path):
    """`galaius.models` is on the import path of EVERY CLI command, and importing litellm costs
    ~2.5 s. Pricing the board's models against it once a day is the difference between a live
    catalog and a tool that got slower for everyone who never asked."""
    import sys

    from galaius import model_catalog as mcat

    board = tmp_path / "board.json"
    board.write_text(json.dumps({"scores": [
        {"name": "Newcomer 5", "intelligence": 90.0},
        {"name": "New Unpriced 5", "intelligence": 80.0},
    ]}))
    monkeypatch.setattr(mcat, "leaderboard_path", lambda: board)
    monkeypatch.setattr(type(mcat.RANKED_CACHE), "path",
                        property(lambda _s: tmp_path / "ranked.json"))

    class Priced:
        model_cost = {
            "newcomer-5": {"litellm_provider": "vendor", "input_cost_per_token": 2e-6,
                           "mode": "chat"},
            "new-unpriced-5": {"litellm_provider": "vendor", "mode": "chat"},
            # Ranked and priced, but an agent cannot be pointed at it. 86 embedding models, 25
            # image generators and 9 rerankers were all eligible candidates for a criterion.
            "newcomer-5-embed": {"litellm_provider": "vendor", "input_cost_per_token": 1e-9,
                                 "mode": "embedding"},
        }

    monkeypatch.setitem(sys.modules, "litellm", Priced)
    first = mcat.ranked_extras()
    assert "newcomer-5" in first, "priced from litellm the first time"
    assert "new-unpriced-5" in first, "a live-ranked conversational model need not have a price yet"
    assert "newcomer-5-embed" not in first, "an embedding model is not something an agent can run"

    # Now litellm is not merely slow — it is GONE. A cached answer must still be there, or the
    # cache is decoration and every command pays the import.
    monkeypatch.setitem(sys.modules, "litellm", None)
    assert "newcomer-5" in mcat.ranked_extras(), "and read back without touching it again"


def test_a_percentile_is_read_against_the_variable_s_OWN_source(monkeypatch, tmp_path):
    """`90%` of `aa.intelligence` means the 90th percentile of Artificial Analysis' board.

    The local catalog holds a MIXTURE: live board numbers where the board speaks, and the shipped
    snapshot's numbers where it does not. Taking the percentile over that mixture moved 75% from
    22.3 to 31.5 — every percentile criterion quietly meaning something stricter than it says,
    which is the same "two sources, one number" bug that put two tabs of one window into open
    disagreement about which model leads.
    """
    from galaius import model_catalog as mcat

    board = tmp_path / "board.json"
    board.write_text(json.dumps({"scores": [
        {"name": "One", "intelligence": 10.0}, {"name": "Two", "intelligence": 20.0},
        {"name": "Three", "intelligence": 30.0}, {"name": "Four", "intelligence": 40.0},
    ]}))
    monkeypatch.setattr(mcat, "leaderboard_path", lambda: board)
    monkeypatch.setattr(type(mcat.RANKED_CACHE), "path", property(lambda _s: tmp_path / "r.json"))
    with catalog_of(
        # Two models the board never ranked, both scored high by the snapshot. They must not drag
        # the bar upward: they are not what Artificial Analysis measured.
        Model(provider="p", id="snapshot-only-a", capabilities=set(), intelligence_score=99.0,
              input_cost_per_million=1.0),
        Model(provider="p", id="snapshot-only-b", capabilities=set(), intelligence_score=98.0,
              input_cost_per_million=1.0),
    ):
        bar, = Criteria.parse("aa.intelligence >= 75%").against_the_board()
        assert bar.value == 30.0, "the board's own 75th percentile, not the local mixture's"
        assert "of 4 scored" in bar.asked, "and it says whose distribution that was"


def test_every_variable_reads_its_percentile_against_its_own_leaderboard(registry, monkeypatch):
    """`90%` on a benchmark means the 90th percentile of THAT leaderboard.

    Reading it off the local catalog instead was a hack wearing one variable's name: the same bug
    it fixed for `aa.intelligence` was left in place for `gui.screenspot` and every benchmark added
    after it. A namespace IS a source, so the source owns its own distribution — and it is the
    SAME table the score is read from, or `90%` is the 90th percentile of a board nobody was
    scored on.
    """
    from galaius import benchmark_tables
    from galaius.benchmarks.published import PublishedEntry, PublishedTable

    # No live table: the bundled snapshot IS this fixture's board, so the assertion cannot depend
    # on whatever this machine last fetched.
    monkeypatch.setattr(benchmark_tables, "load_tables", lambda **_: {})
    bench = Benchmark(
        id="fixture-bench", name="Fixture", description="a leaderboard", namespace="fx",
        published=PublishedTable(
            source_url="https://example.invalid", retrieved="2026-01-01", freshness="current",
            entries=[PublishedEntry(model_name=f"m{i}", score=float(i), normalized_score=float(i),
                                    status="eligible") for i in range(1, 11)],
        ),
    )
    Benchmark._register(bench)
    try:
        bar, = Criteria.parse(f"{bench.variable} >= 90%").against_the_board()
        assert bar.value == 9.0, "the leaderboard's own 90th percentile of 1..10"
        assert "of 10 scored" in bar.asked
    finally:
        Benchmark._registry[:] = [b for b in Benchmark.registry() if b.id != "fixture-bench"]


def test_an_unknown_price_is_not_a_cheap_price(registry):
    """"Cheapest that clears the bar" must not mean "the one whose price nobody knows".

    `cost_of` counted a missing price as 0, so all 50 scored models carrying no price at all —
    `chatgpt/gpt-5.4-pro` among them — sorted ahead of every genuinely cheap model and won every
    criterion. A model that is FREE and a model we have no number for are different facts, and
    only one of them is an argument for choosing it.

    A locally served model is the real free case and keeps its place at the front: it costs
    nothing per token because nobody is billing for it.
    """
    with catalog_of(
        Model(provider="p", id="known-cheap", capabilities=set(), intelligence_score=50.0,
              input_cost_per_million=0.5, output_cost_per_million=1.0),
        Model(provider="p", id="price-unknown", capabilities=set(), intelligence_score=50.0),
        Model(provider="local", id="served-free", capabilities=set(), intelligence_score=50.0),
    ):
        # REBIND, never mutate: `catalog_of` restores this attribute by reference, so mutating
        # the dict in place leaks the fake provider into every later test in the session.
        Model._served = {**Model._served, "local": ["served-free"]}
        order = [m.id for m in Criteria.parse("aa.intelligence > 10").qualifying(available_only=False)]
        assert order.index("served-free") < order.index("known-cheap"), (
            "a locally served model genuinely costs nothing per token"
        )
        assert order[-1] == "price-unknown", "an unpriced model is never evidence of thrift"


def test_the_same_price_is_broken_by_the_measure_not_by_registry_order(registry):
    """Two models at one price: take the better one. The sort was stable on cost alone, so the
    winner was whichever happened to be registered first — an arbitrary pick wearing the words
    "cheapest that clears"."""
    with catalog_of(
        Model(provider="p", id="weaker-first", capabilities=set(), intelligence_score=20.0,
              input_cost_per_million=1.0, output_cost_per_million=1.0),
        Model(provider="p", id="stronger-second", capabilities=set(), intelligence_score=40.0,
              input_cost_per_million=1.0, output_cost_per_million=1.0),
    ):
        assert Criteria.parse("aa.intelligence > 10").choose(available_only=False).id == "stronger-second"


def test_a_percentile_bar_says_what_number_it_came_out_as(registry):
    """`aa.intelligence >= 50%` picked a model ranked 81st and the line never said why.

    The bar was 9.5 — the board's median, because the board has a long tail of small models — so
    81st clears it comfortably and the answer was right. It was unreadable: two numbers the reader
    could not reconcile, and no third number anywhere that would have.
    """
    said = Criteria.parse("aa.intelligence >= 50%").explain(available_only=False)
    assert "50%" in said, "it still says what was asked for"
    assert "20" in said, "and what that came out as on today's board"


def test_the_measure_names_its_source_from_the_registry(registry):
    """The source of a number is data on the variable, not a literal beside the sentence.

    `competence()` held `source = "Artificial Analysis"` inline, so a second board supplying the
    same field would still have been announced as the first one's.
    """
    from galaius.criteria import Variables

    variable = Variables.by_name("aa.intelligence")
    assert variable is not None and variable.source, "the registry owns the source name"
    model = Model(provider="p", id="m", capabilities=set(), intelligence_score=50.0)
    assert variable.source in model.competence()


def test_a_position_in_the_field_is_written_as_a_percentage(registry):
    """"why are we talking in p instead of % in the benchmarks?"

    `90%` was invented jargon. A position in the field is a percentage and reads as one. The two
    notations stay unambiguous because they answer different questions: a BARE number is a raw
    score on the variable's own scale (`gui.screenspot > 0.85`), and a number carrying `%` is a
    position among everything that source measured (`aa.intelligence >= 90%`).
    """
    top = Criteria.parse("aa.intelligence >= 90%")
    assert str(top) == "aa.intelligence >= 90%", "it keeps the notation that was written"
    bar, = top.against_the_board()
    assert bar.value == 40.0, "the 90th percentile of 10/20/40 by nearest rank"
    assert "90%" in bar.asked, "and says the position it came from"

    # A raw number is still a raw number: same variable, wildly different meaning, no ambiguity.
    raw, = Criteria.parse("aa.intelligence >= 90").against_the_board()
    assert raw.value == 90.0 and not raw.percentile

    with pytest.raises(CriteriaError, match="between 0 and 100"):
        Criteria.parse("aa.intelligence >= 150%")
    # The invented spelling is gone rather than kept alive beside its replacement.
    with pytest.raises(CriteriaError):
        Criteria.parse("aa.intelligence >= p90")


class TestProviderConstraint:
    """Provider as a first-class constraint, in the same criteria language.

    "maybe one constraint could be just using a provider?" — REQUIRE (`=`/`==`) is a hard filter
    that refuses when unmet; EXCLUDE (`!=`) removes a provider from the pool; PREFER (`~`) is a
    soft ordering that still falls through to everything else. All three compose with the
    benchmark terms already there: a provider constraint never widens what a hard capability or
    price term already ruled out — it only filters/orders the candidates that already qualify.
    """

    def test_require_is_parsed_as_its_own_constraint_not_a_scoring_term(self):
        criteria = Criteria.parse("aa.intelligence > 10 and provider = codex")
        assert criteria.provider_require() == "codex"
        assert criteria.provider_prefer() is None
        assert criteria.provider_exclude() == frozenset()
        # Never folded into the scored terms — a provider name has no place on a Model's scale.
        assert all(term.field != "provider" for term in criteria.terms)

    def test_double_equals_also_requires(self):
        assert Criteria.parse("provider == codex").provider_require() == "codex"

    def test_exclude_is_parsed(self):
        criteria = Criteria.parse("aa.intelligence > 10 and provider != claude")
        assert criteria.provider_exclude() == frozenset({"claude"})
        assert criteria.provider_require() is None

    def test_prefer_is_parsed(self):
        criteria = Criteria.parse("aa.intelligence > 10 and provider ~ codex")
        assert criteria.provider_prefer() == "codex"
        assert criteria.provider_require() is None

    def test_a_provider_only_criterion_needs_no_scoring_term(self):
        """`provider = codex` alone selects on price only, same as no benchmark term at all —
        it must not be treated as an empty, unusable criterion."""
        criteria = Criteria.parse("provider = codex")
        assert criteria.provider_require() == "codex"
        assert criteria.terms == ()

    def test_malformed_provider_clause_is_refused_at_parse(self):
        with pytest.raises(CriteriaError, match="provider"):
            Criteria.parse("provider codex")

    def test_conflicting_require_and_exclude_same_provider_is_refused(self):
        with pytest.raises(CriteriaError, match="codex"):
            Criteria.parse("provider = codex and provider != codex")

    def test_two_different_required_providers_is_refused(self):
        with pytest.raises(CriteriaError):
            Criteria.parse("provider = codex and provider = claude")

    def test_prefer_conflicting_with_require_is_refused(self):
        with pytest.raises(CriteriaError):
            Criteria.parse("provider = codex and provider ~ claude")
