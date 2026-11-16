"""Choosing a model by what it SCORES, and saying who measured it.

    ~/.galaius/agents.json:  {"agents": {"visual-critic": "cap.vlm and gui.screenspot > 0.85"}}

A pinned model id is a claim frozen at typing time. Can't notice a better model shipping, a price
cut, or — the case that cost this project weeks — that the tier was never good enough for its job.
A CRITERION is that claim written down instead: "whatever currently clears this bar", re-resolved
every time it's asked. A bare benchmark metric ranks the measured survivors; a price-only rule
still chooses the cheapest survivor.

Every variable is NAMESPACED BY ITS SOURCE — a bare ``intelligence`` hides who measured it, and
two leaderboards rarely agree:

    aa.intelligence        Artificial Analysis' capability score
    aa.mmmu_pro            Artificial Analysis' MMMU Pro visual metric
    oc.mmbench             the OpenCompass MMBench leaderboard
    gui.screenspot         the GUI-Agent grounding leaderboard
    oc.video_mme           the OpenCompass video leaderboard
    price.in / price.out   $ per million tokens, from the provider catalog
    cap.vlm                a capability, demanded by name

A bar may be written as a POSITION in the field rather than a raw number — `aa.intelligence > 90%`
is "better than 90% of everything that source measured". A typed number freezes on its day (`> 40`
meant "the very top" in 2025, "the middle" now); a percentile says what was meant and re-reads the
board every time, so the criterion ages the way the field does.

The set is DERIVED, never a hardcoded list: a benchmark added to the registry tomorrow is usable
in a criterion the same day, a model shipping tomorrow that clears the bar is simply used.
"""

from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ClassVar, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from galaius.catalog import source as benchmark_source
from galaius.catalog import tables as benchmark_tables
from galaius.catalog import models as model_catalog
from galaius.inference import ollama
from galaius.models.registry import Benchmark, Model, TokenMix
from galaius.models.capability import ModelCapability
from galaius.catalog.published import PublishedEntry, PublishedTable
from galaius.catalog.ttl_cache import TTLCache


class CriteriaError(ValueError):
    """A criterion that cannot be evaluated — raised where it is WRITTEN, never at use."""


@dataclass(frozen=True)
class Variable:
    """One comparable fact about a model, and where the number comes from."""

    name: str
    describe: str
    read: Callable[[Model], float | None]
    #: WHO measured it — data, never a literal beside the sentence, else a second board supplying
    #: the same field would still be announced as the first one's. Empty for a fact nobody
    #: publishes (a capability flag).
    source: str = ""
    #: True for a yes/no (a capability), which takes no operator.
    flag: bool = False
    #: Numeric source measures can be written bare to request descending ranking. Prices remain
    #: comparisons because "price.in" is a cost constraint, not a quality benchmark.
    rankable: bool = False
    higher_is_better: bool = True
    #: Distribution a PERCENTILE bar reads against — the source's OWN published population, when
    #: it has one. Namespace IS source, so source owns this: reading `90%` off the local catalog
    #: instead answers "the 90th percentile of what I happen to hold" — mixes live numbers with a
    #: shipped snapshot's, stricter or looser than it reads.
    population: Callable[[], list[float]] | None = None
    #: Whether this source measured a given model. A percentile is a claim ABOUT a population, so
    #: a model the source never measured has no place in it — carrying a snapshot score into a
    #: board percentile once put fifteen models ABOVE the board's own maximum.
    measured: Callable[[Model], bool] | None = None
    #: The range the source scores on, (worst, best) — what "a point" of this measure is when two
    #: models are compared (`ValueRule.edge`). None: no declared scale.
    score_range: tuple[float, float] | None = None


class _Scalar(BaseModel):
    """A `Variable` read straight off the model record rather than off a benchmark board."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    attr: str
    describe: str
    source: str
    population: Callable[[], list[float]] | None = None
    score_range: tuple[float, float] | None = None
    #: A quality measure, rankable when written bare — never a price, which is a cost constraint.
    rankable: bool = False


class Variables:
    """Every comparison galaius can make, derived from what is registered right now."""

    #: Scalars off the model record. Namespaced by SOURCE: capability score is Artificial
    #: Analysis', prices are the provider catalog's — saying so is the whole point.
    _SCALARS: dict[str, _Scalar] = {
        # Artificial Analysis publishes its board to disk — that board, not the local catalog, is
        # what "the 90th percentile" means for this number. Scored out of 100, like its sibling
        # indexes in the benchmark registry (`aa.coding_index`, `aa.coding_agent_index`).
        "aa.intelligence": _Scalar(attr="intelligence_score", describe="Artificial Analysis capability score",
                                   source="Artificial Analysis", population=lambda: _board_scores(),
                                   score_range=(0.0, 100.0), rankable=True),
        # A price has no published leaderboard: its population is what galaius can reach — also
        # the honest answer to "cheaper than most of what I could actually run".
        "price.in": _Scalar(attr="input_cost_per_million", describe="input cost, $ per million tokens",
                            source="the provider catalog"),
        "price.out": _Scalar(attr="output_cost_per_million", describe="output cost, $ per million tokens",
                             source="the provider catalog"),
    }

    @classmethod
    def all(cls) -> list[Variable]:
        out: list[Variable] = []
        for name, scalar in cls._SCALARS.items():
            out.append(Variable(
                name, scalar.describe, _reader(scalar.attr), source=scalar.source, rankable=scalar.rankable,
                population=scalar.population,
                measured=_board_measured if scalar.population is not None else None,
                score_range=scalar.score_range,
            ))
        for bench in Benchmark.registry():
            out.append(Variable(
                bench.variable, f"{bench.name} — {bench.source or 'published'}",
                _bench_reader(bench), source=bench.source or "published",
                rankable=True,
                higher_is_better=bench.higher_is_better is not False,
                population=_bench_population(bench),
                measured=_bench_measured(bench),
                score_range=bench.score_range if bench.score_range and bench.score_range[0] < bench.score_range[1] else None,
            ))
        for cap in ModelCapability:
            out.append(Variable(
                f"cap.{cap.value}", f"the model can do {cap.value}",
                _cap_reader(cap), flag=True,
            ))
        return out

    @classmethod
    def by_name(cls, name: str) -> Variable | None:
        return next((v for v in cls.all() if v.name == name), None)

    @classmethod
    def names(cls) -> list[str]:
        return sorted(v.name for v in cls.all())


def _reader(attr: str) -> Callable[[Model], float | None]:
    return lambda model: getattr(model, attr, None)


def _bench_entries(bench: Benchmark) -> list[PublishedEntry]:
    """Rows this benchmark's board publishes and still stands behind: the live table when at
    least as recent as the bundled snapshot, else the snapshot.

    ONE resolution of "which board, and which of its rows". A model's score and the population
    its percentile reads against must come off the SAME table, or `90%` is the 90th percentile of
    a board nobody on screen was ever scored on.
    """
    published = _bench_table(bench)
    if published is None:
        return []
    return [entry for entry in published.entries if entry.qualifies(published.freshness)]


def _bench_table(bench: Benchmark) -> PublishedTable | None:
    live = benchmark_tables.load_tables().get(bench.id)
    return (
        live
        if live is not None
        and (bench.published is None or live.retrieved >= bench.published.retrieved)
        else bench.published
    )


_BOUND_ROWS: dict[str, tuple[PublishedTable, dict[str, PublishedEntry]]] = {}


def _bench_row(bench: Benchmark, model: Model) -> PublishedEntry | None:
    """The published row naming this model. Bound by its as-published name (`Model.published_key`):
    the stored `model_id` is a hand-kept second spelling (`claude-opus-5-5` while the catalog
    registers `claude-opus-5.5`), never the key. Every reseller copy of a model shares its row."""
    table = _bench_table(bench)
    if table is None:
        return None
    held = _BOUND_ROWS.get(bench.id)
    if held is None or held[0] is not table:   # a live table landing after import replaces the bound one
        held = _BOUND_ROWS[bench.id] = (table, {Model.published_key(entry.model_name): entry for entry in _bench_entries(bench)})
    return held[1].get(model.published_id_key)


def _bench_reader(bench: Benchmark) -> Callable[[Model], float | None]:
    def read(model: Model) -> float | None:
        measured = bench.score_for(model)
        if measured is not None:
            return measured
        entry = _bench_row(bench, model)
        return entry.normalized_score if entry is not None else None

    return read


def _board_scores() -> list[float]:
    """What Artificial Analysis currently publishes, as numbers."""
    return list(model_catalog.live_scores().values())


def _board_measured(model: Model) -> bool:
    """Whether Artificial Analysis has a row for this model — not merely whether galaius holds a
    number for it, which may be the shipped snapshot's."""
    from galaius.catalog.models import bare_model_name, live_scores

    return bare_model_name(model.id) in live_scores()


def _bench_measured(bench: Benchmark) -> Callable[[Model], bool] | None:
    """Whether this benchmark's source or published table lists the model."""
    read = _bench_reader(bench)
    return lambda model: read(model) is not None


def _bench_population(bench: Benchmark) -> Callable[[], list[float]]:
    """A benchmark's own published leaderboard — the population its percentiles mean.

    Read LAZILY, never decided when the variable is built: the live table lands on disk after
    import, so answering "this benchmark has no board" once would freeze that answer for the
    process's life. An empty board is not an error here — `_population` falls back.
    """
    def population() -> list[float]:
        measured = [
            score for model in Model.catalog()
            if (score := bench.score_for(model)) is not None
        ]
        if measured:
            return measured
        return [
            entry.normalized_score for entry in _bench_entries(bench)
            if entry.normalized_score is not None
        ]

    return population


def _cap_reader(cap: ModelCapability) -> Callable[[Model], float | None]:
    return lambda model: 1.0 if model.can(cap) else 0.0


def _weighted_value(variable: Variable | None, model: Model) -> float | None:
    if variable is None:
        return None
    score = variable.read(model)
    if score is None:
        return None
    benchmark = next(
        (item for item in Benchmark.registry() if item.variable == variable.name), None
    )
    if benchmark is not None and benchmark.score_range is not None:
        lower, upper = benchmark.score_range
        if not lower <= score <= upper or lower >= upper:
            return None
        score = (score - lower) / (upper - lower)
    if not 0 <= score <= 1:
        return None
    return score if variable.higher_is_better else 1 - score


_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "=": lambda a, b: a == b,
    "==": lambda a, b: a == b,
}

_TERM = re.compile(
    r"^\s*([\w.-]+)\s*(>=|<=|==|=|>|<)\s*(-?\d+(?:\.\d+)?%?)\s*$")
_BARE = re.compile(r"^\s*([\w.-]+)\s*$")


def _did_you_mean(name: str) -> str:
    """Namespaced variables whose tail matches what they typed — so a bare `intelligence` is
    answered with `aa.intelligence`, not the whole catalogue."""
    renamed = {"aa.mmmu": "aa.mmmu_pro", "aa.mmbench": "oc.mmbench"}
    if replacement := renamed.get(name.lower()):
        return f" This metric was corrected; use {replacement!r} and review its meaning."
    tail = name.rsplit(".", 1)[-1].lower()
    near = [n for n in Variables.names() if n.rsplit(".", 1)[-1].lower() == tail]
    return f" Did you mean: {', '.join(near)}?" if near else ""


def _population(field: str) -> tuple[list[float], bool]:
    """The distribution a percentile bar is read against: the variable's OWN source, ascending.

    A NAMESPACE IS A SOURCE, so the source owns its own distribution: `aa.intelligence` is
    Artificial Analysis' board, `gui.screenspot` is the grounding leaderboard, each on disk. The
    local catalog is a MIXTURE — live numbers where a board speaks, the shipped snapshot's where
    it doesn't — and percentiling over that mixture once moved the 75% bar from 22.3 to 31.5,
    every percentile criterion quietly stricter than it read. Same "one number, two sources" bug
    that once had two tabs of one window disagreeing which model leads.

    A variable with no published population (a price) falls back to what galaius can reach —
    also the honest reading of "cheaper than most of what I could actually run", and so does a
    board simply not on this machine.
    """
    var = Variables.by_name(field)
    if var is None:
        return [], False
    if var.population is not None:
        published = var.population()
        if published:
            return sorted(published), True
    return sorted(
        score for model in Model.catalog() if (score := var.read(model)) is not None
    ), False


@dataclass(frozen=True)
class Term:
    """One clause. A capability has no op or value — it is a yes/no."""

    field: str
    op: str = ""
    value: float = 0.0
    #: True when the bar was written as a POSITION in the field (`90%`), not a raw number. Then
    #: `value` is the percentile, and the bar is whatever that position is worth today.
    percentile: bool = False
    #: What was asked for, carried through resolution so a message can say both — "90% of 450
    #: scored" beside the number it came out as; the only way to read either.
    asked: str = ""
    #: Set on a RESOLVED percentile: a model this variable's source never measured is not in the
    #: population the percentile describes, so it can't clear a bar drawn on it.
    source_only: bool = False
    #: A bare numeric metric is a ranking request: it needs a number, never a zero-valued threshold; the source's own
    #: measures rank before numbers it never measured (`Criteria.qualifying`).
    ranking: bool = False

    def __str__(self) -> str:
        if not self.op:
            return self.field
        if self.percentile:
            return f"{self.field} {self.op} {self.value:g}%"
        shown = f"{self.field} {self.op} {self.value:g}"
        return f"{shown} ({self.asked})" if self.asked else shown

    def resolved(self) -> "Term":
        """This term with a board-relative bar turned into the number it's worth TODAY.

        The population is everything the variable's OWN SOURCE measures — never the models a key
        happens to reach ("the top tenth" must not mean "the best of my three"), never the local
        mixture of live and snapshot numbers (see :func:`_population`). Nothing scored leaves the
        term as-is; no model can then clear it, and `explain` says why.
        """
        if not self.percentile:
            return self
        scores, from_source = _population(self.field)
        if not scores:
            return self
        # Nearest-rank: 90% of 10 scores is the 9th, so `>= 90%` keeps a tenth of the field.
        rank = min(len(scores), max(1, math.ceil(self.value / 100 * len(scores))))
        return Term(self.field, self.op, scores[rank - 1],
                    asked=f"{self.value:g}% of {len(scores)} scored", source_only=from_source)

    def score_of(self, model: Model) -> float | None:
        """What this term measures on ``model``, or None when nothing measured it.

        None is NOT zero and never qualifies: "unknown" is what a criterion excludes.
        """
        var = Variables.by_name(self.field)
        return None if var is None else var.read(model)

    def holds(self, model: Model) -> bool:
        if self.percentile:  # asked directly rather than through `Criteria`, which pre-resolves
            bar = self.resolved()
            return False if bar.percentile else bar.holds(model)
        # A percentile bar is drawn on what the source measured: a model it never measured is outside it. A bare ranking
        # metric only needs a number (`qualifying` ranks the source's own measures first): `aa.intelligence and provider ~
        # mistral` dropped every Mistral model, none being on the live board, so the preference never ran.
        if self.source_only:
            var = Variables.by_name(self.field)
            if var is not None and var.measured is not None and not var.measured(model):
                return False
        got = self.score_of(model)
        if got is None:
            # Policy defaults use nonnegative price clauses as presence guards. A newly
            # published model can have a live quality score before its provider publishes both
            # prices; keep it visible, then let `thrift` place it after priced rows. A real price
            # ceiling remains a hard filter.
            if self.field in {"price.in", "price.out"} and self.op == ">=" and self.value <= 0:
                return True
            return False
        if self.ranking:
            return True
        if not self.op:
            return got > 0
        return _OPS[self.op](got, self.value)


#: WHICH CLI runs the request, never WHAT it scores — so it lives beside `Term`, not inside
#: `Variables`: no `Model` carries a "provider" score to read, and its own grammar (`=`/`!=`/`~`)
#: answers a different question than a threshold does. REQUIRE refuses when unmet (honest,
#: brittle); PREFER reorders the qualifying candidates but still falls through to the rest (keeps
#: work moving, never silently drops the ask — the refusal it never raises says so by construction);
#: EXCLUDE removes one provider from the pool. `explain`/refusal text always says which was written.
ProviderMode = Literal["require", "prefer", "exclude"]

_PROVIDER_OPS: dict[str, ProviderMode] = {"=": "require", "==": "require", "!=": "exclude", "~": "prefer"}
_PROVIDER_TERM = re.compile(r"^\s*provider\s*(=|==|!=|~)\s*([a-z][a-z0-9_-]*)\s*$")


@dataclass(frozen=True)
class ProviderConstraint:
    """One `provider <op> name` clause, resolved to REQUIRE / PREFER / EXCLUDE."""

    name: str
    mode: ProviderMode

    def __str__(self) -> str:
        op = {"require": "=", "exclude": "!=", "prefer": "~"}[self.mode]
        return f"provider {op} {self.name}"


def _parse_provider_clause(clause: str) -> ProviderConstraint:
    match = _PROVIDER_TERM.match(clause)
    if match is None:
        raise CriteriaError(
            f"{clause.strip()!r} is not a usable provider constraint — write "
            "'provider = <name>' (REQUIRE), 'provider != <name>' (EXCLUDE), "
            "or 'provider ~ <name>' (PREFER)"
        )
    op, name = match.group(1), match.group(2)
    return ProviderConstraint(name, _PROVIDER_OPS[op])


def _validate_provider_constraints(constraints: list[ProviderConstraint]) -> None:
    """Refuse a self-contradictory set of provider clauses HERE, at write time — never silently
    picking one over the other later."""
    required = {c.name for c in constraints if c.mode == "require"}
    excluded = {c.name for c in constraints if c.mode == "exclude"}
    preferred = {c.name for c in constraints if c.mode == "prefer"}
    if len(required) > 1:
        raise CriteriaError(
            f"provider REQUIRE names more than one provider ({', '.join(sorted(required))}) — "
            "a run has exactly one provider, write only one"
        )
    if len(preferred) > 1:
        raise CriteriaError(
            f"provider PREFER names more than one provider ({', '.join(sorted(preferred))}) — "
            "write only one preferred provider"
        )
    if required & excluded:
        name = next(iter(required & excluded))
        raise CriteriaError(f"provider {name!r} is both REQUIRE and EXCLUDE — contradiction")
    if required and preferred and required != preferred:
        raise CriteriaError(
            f"provider REQUIRE {next(iter(required))!r} conflicts with PREFER "
            f"{next(iter(preferred))!r} — a required provider makes a different preference moot"
        )
    if preferred & excluded:
        name = next(iter(preferred & excluded))
        raise CriteriaError(f"provider {name!r} is both PREFER and EXCLUDE — contradiction")


class Valued(BaseModel):
    """One model's place under the cost relation (`ValueRule`), with the numbers that set it: its score on the
    ranked measure, its price for the workload, and the relation of the two."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    model: Model
    #: Points out of 100 on the ranked measure (a weighted score's single number, several measures summed).
    score: float
    #: $ per million tokens, catalog rates blended by the workload's `TokenMix`.
    cost: float
    #: `score` less what its price costs in points against the pool's cheapest model (`ValueRule.value`).
    value: float

    def __str__(self) -> str:
        return f"{self.model.id}: score {self.score:g}, ${self.cost:.2f} per M tokens, value {self.value:.1f}"


class ValueRule(BaseModel):
    """Intelligence against cost, as ONE relation every ranking applies — never a list of models left out.

    value = score − per_doubling × log2((price + price_floor) / (cheapest price + price_floor))

    A model must buy `per_doubling` points of the ranked measure for each doubling of its price over the
    cheapest model in the same pool; the highest value runs first. Owner 2026-10-05: « We need some kind of
    formula … Fable should not even come out in the formula, because intelligence vs costs surpasses anyways
    this model »; 2026-11-16: « find the correct relations to make efficient models be chosen, so no hardcoding /
    deactivating models ». A model leaves the top because the relation ranks it lower, never by its name; every
    model stays in the list as a fallback.

    Default 3.5 points per doubling ≈ the earlier bound of 2 points per 1.5× the price (2 / log2 1.5 = 3.42).
    Price is the catalog's API list price blended by the role's measured `TokenMix`: it also stands for a
    subscription's usage share, which the vendors meter in proportion to the same token prices (an assumption,
    no vendor publishes the meter). A model with no known price or score keeps its place after the valued ones.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    per_doubling: float = Field(default=3.5, ge=0)
    #: $ per M tokens added to every price, so a model served on the machine (price 0) has a finite relation.
    price_floor: float = Field(default=0.01, gt=0)

    def value(self, score: float, cost: float, cheapest: float) -> float:
        return score - self.per_doubling * math.log2((cost + self.price_floor) / (cheapest + self.price_floor))

    def order(
        self, models: Sequence[Model], score: Callable[[Model], tuple[float, ...] | None], mix: TokenMix,
    ) -> tuple[list[Model], list[Valued]]:
        """``models`` ordered by value, highest first (ties keep their order); models with no score or price follow, in
        their order. Nothing is dropped. The second list: each valued model's numbers, in the new order."""
        priced = [(model, scored, cost) for model in models
                  if (scored := score(model)) is not None and (cost := model.blended_cost(mix)) is not None]
        if not priced:
            return list(models), []
        cheapest = min(cost for _model, _scored, cost in priced)
        valued = sorted(
            (Valued(model=model, score=sum(scored), cost=cost, value=self.value(sum(scored), cost, cheapest))
             for model, scored, cost in priced),
            key=lambda item: -item.value,
        )
        ids = {id(item.model) for item in valued}
        return [item.model for item in valued] + [model for model in models if id(model) not in ids], valued


@dataclass(frozen=True)
class Criteria:
    """A model requirement, as a sentence somebody can read and change."""

    #: How a requirement starts, telling it from a model id (`Criteria.written`).
    NAMESPACES: ClassVar[tuple[str, ...]] = ("cap.", "aa.", "gui.", "oc.", "price.")

    #: Every cache file a ranking reads (fixed names under ``~/.galaius/out``; the VS Code extension
    #: reads them too). ``machine_local`` marks the one describing this machine's own daemon.
    INPUTS: ClassVar[tuple[TTLCache, ...]] = (
        benchmark_source.CACHE, benchmark_tables.CACHE, model_catalog.CACHE, model_catalog.RANKED_CACHE,
        ollama.CAPABILITY_CACHE,
    )

    terms: tuple[Term, ...] = field(default_factory=tuple)
    #: Which CLI may run it — parsed out of the SAME text, never scored against a Model (see
    #: `ProviderConstraint`). Applied downstream, over the ranked (model, CLI) candidate list.
    providers: tuple[ProviderConstraint, ...] = field(default_factory=tuple)
    source: str = ""

    @classmethod
    def written(cls, text: str) -> bool:
        """Whether `text` is a requirement (`cap.vlm`, `aa.intelligence >= 40`), not a model id."""
        return text.startswith(cls.NAMESPACES)

    @classmethod
    def input_files(cls) -> list[tuple[Path, TTLCache]]:
        """Every file a ranking reads, each with the cache whose TTL ages it: the input caches, and
        the benchmark board where ``BENCHMARK_SCORES`` points it (:func:`model_catalog.leaderboard_path`)."""
        files = [(cache.path, cache) for cache in cls.INPUTS]
        board = model_catalog.leaderboard_path()
        if board is None or board in {path for path, _ in files}:
            return files
        return [*files, (board, benchmark_source.CACHE)]

    def provider_require(self) -> str | None:
        return next((c.name for c in self.providers if c.mode == "require"), None)

    def provider_prefer(self) -> str | None:
        return next((c.name for c in self.providers if c.mode == "prefer"), None)

    def provider_exclude(self) -> frozenset[str]:
        return frozenset(c.name for c in self.providers if c.mode == "exclude")

    def __str__(self) -> str:
        return self.source or " and ".join(str(t) for t in self.terms)

    @staticmethod
    def comparison_operators():
        return tuple(_OPS)

    @classmethod
    def parse(cls, text: str) -> "Criteria":
        """Read a criterion, refusing anything nobody can evaluate.

        Raises `CriteriaError` naming the clause THEY typed — an unknown or un-namespaced
        variable fails here, at write time, never silently matching nothing later.
        """
        if not text or not text.strip():
            raise CriteriaError("an empty criterion selects nothing — say what you want")
        terms: list[Term] = []
        provider_constraints: list[ProviderConstraint] = []
        for clause in re.split(r"\s+and\s+|,", text):
            if not clause.strip():
                continue
            if clause.strip().split(None, 1)[0] == "provider":
                provider_constraints.append(_parse_provider_clause(clause))
                continue
            if (m := _TERM.match(clause)) is not None:
                name, op, written = m.group(1), m.group(2), m.group(3)
                var = Variables.by_name(name)
                if var is None:
                    raise CriteriaError(
                        f"{name!r} is not a variable galaius knows.{_did_you_mean(name)}"
                    )
                if var.flag:
                    raise CriteriaError(f"{name!r} is a yes/no — write it on its own, not with {op}")
                if written.endswith("%"):
                    # A POSITION in the field, not a score on the variable's own scale — a bare
                    # number is the raw measure (`gui.screenspot > 0.85`), a percentage is a
                    # place among everything that source measured; the two never collide.
                    percentile = float(written[:-1])
                    if not 0 < percentile <= 100:
                        raise CriteriaError(
                            f"{written!r} is a position in the field, so it must be between 0 and "
                            "100 — '90%' is the top tenth"
                        )
                    terms.append(Term(name, op, percentile, percentile=True))
                else:
                    terms.append(Term(name, op, float(written)))
            elif (m := _BARE.match(clause)) is not None:
                name = m.group(1)
                var = Variables.by_name(name)
                if var is None:
                    raise CriteriaError(
                        f"{name!r} is not a capability or metric.{_did_you_mean(name)}"
                    )
                if var.rankable:
                    terms.append(Term(name, ranking=True))
                    continue
                if var.flag:
                    terms.append(Term(name))
                    continue
                raise CriteriaError(
                    f"{name!r} is a measurement — compare it, e.g. '{name} > 0.8'"
                )
            else:
                raise CriteriaError(
                    f"{clause.strip()!r} is not a criterion — write it as 'name > number'"
                )
        if not terms and not provider_constraints:
            raise CriteriaError("an empty criterion selects nothing — say what you want")
        _validate_provider_constraints(provider_constraints)
        return cls(tuple(terms), tuple(provider_constraints), text.strip())

    @staticmethod
    def _pool(
        available_only: bool,
        runnable: Callable[[Model], bool] | None,
        candidates: list[Model] | None = None,
    ) -> list[Model]:
        """Who is in the running. `runnable`, when given, IS the pool: the caller — a vendor CLI —
        knows what it can actually be pointed at, its own login included. Otherwise every model
        whose key is here (or every model, for a dry look at the catalog)."""
        models = Model.catalog() if candidates is None else candidates
        if runnable is not None:
            return [m for m in models if runnable(m)]
        return [m for m in models if not available_only or m.is_available()]

    def against_the_board(self) -> tuple[Term, ...]:
        """Terms with every board-relative bar turned into today's number — read ONCE, so every
        model is judged against the same bar and the board isn't walked per candidate."""
        return tuple(t.resolved() for t in self.terms)

    def clears(self, model: Model) -> bool:
        """Whether ``model`` clears EVERY term."""
        return all(t.holds(model) for t in self.against_the_board())

    def qualifying(
        self,
        available_only: bool = True,
        runnable: Callable[[Model], bool] | None = None,
        candidates: list[Model] | None = None,
    ) -> list[Model]:
        """Every model clearing EVERY term, ranked by a bare metric or otherwise by price.

        A bare numeric metric means "best measured value". Hard comparisons and bare capability
        terms remain filters. With no bare metric, the historical cheapest-clearing behavior
        remains: known price, then cost, then model id.
        """
        bars = self.against_the_board()
        fit = [m for m in self._pool(available_only, runnable, candidates)
               if all(t.holds(m) for t in bars)]
        variables = self._ranking_variables()
        if variables:
            fit.sort(key=lambda model: (
                not self.measured(model),
                *[variable.read(model) * (-1 if variable.higher_is_better else 1) for variable in variables],
                model.thrift[0], model.thrift[1], model.id,
            ))
            return fit
        fit.sort(key=lambda m: m.thrift)
        return fit

    def measured(self, model: Model) -> bool:
        """Whether every measure this criterion ranks on has its source's own number for `model` (the live board for
        `aa.intelligence`), not only one galaius shipped: those rank first (`qualifying`)."""
        return all(variable.measured is None or variable.measured(model) for variable in self._ranking_variables())

    def _ranking_variables(self) -> list[Variable]:
        variables = [Variables.by_name(term.field) for term in self.terms if term.ranking]
        return [v for v in variables if v is not None] if all(v is not None for v in variables) else []

    def value_score(self, weights: str = "") -> Callable[[Model], tuple[float, ...] | None] | None:
        """What the cost relation (`ValueRule`) reads as a model's score, in POINTS OUT OF 100 of each measure's
        declared range: its weighted score when weights are given, else every bare metric it ranks on. None when the
        criterion ranks on no measure (a bar-only rule runs cheapest first: price already orders it) or on one with no
        declared range (a point there has no size to weigh a price against). The callable answers None for a model
        without that number."""
        parsed = _parse_weights(weights)
        if parsed:
            pairs = [(Variables.by_name(name), weight) for name, weight in parsed.items()]

            def weighted(model: Model) -> tuple[float, ...] | None:
                scores = [(_weighted_value(variable, model), weight) for variable, weight in pairs]
                if any(score is None for score, _weight in scores):
                    return None
                return (100 * sum(score * weight for score, weight in scores if score is not None),)
            return weighted
        variables = self._ranking_variables()
        if not variables or any(variable.score_range is None for variable in variables):
            return None

        def measured(model: Model) -> tuple[float, ...] | None:
            points: list[float] = []
            for variable in variables:
                value = variable.read(model)
                if value is None:
                    return None
                worst, best = variable.score_range
                value = 100 * (value - worst) / (best - worst)
                points.append(value if variable.higher_is_better else 100 - value)
            return tuple(points)
        return measured

    def valued(
        self, value: ValueRule, models: Sequence[Model], weights: str = "", mix: TokenMix = TokenMix(),
    ) -> tuple[list[Model], list[Valued]]:
        """`models` ordered by `value` over this criterion's score, prices blended by `mix`; unchanged (and no numbers)
        when the criterion ranks on no measure the relation can read (`value_score`)."""
        score = self.value_score(weights)
        return (list(models), []) if score is None else value.order(models, score, mix)

    def frontier(
        self,
        available_only: bool = True,
        runnable: Callable[[Model], bool] | None = None,
        weights: str = "",
        candidates: list[Model] | None = None,
        value: ValueRule | None = None,
        mix: TokenMix = TokenMix(),
    ) -> tuple[list[Model], list[Valued]]:
        """The ranking, and each model's numbers under `value` — none unless a `ValueRule` is given: the relation is a
        POLICY choice, read once by its owner (`galaius.agents.run.rank`) and handed here, never a default some
        callers apply and others do not. No model is ever left out by it."""
        fit = self.qualifying(available_only, runnable, candidates)
        parsed = _parse_weights(weights)
        if parsed:
            score = self.value_score(weights)
            weighted = [(scored[0], model) for model in fit if score is not None and (scored := score(model)) is not None]
            weighted.sort(key=lambda pair: (-pair[0], pair[1].thrift, pair[1].id))
            fit = [model for _score, model in weighted]
        if value is None:
            return fit, []
        return self.valued(value, fit, weights, mix)

    def ranked(
        self,
        available_only: bool = True,
        runnable: Callable[[Model], bool] | None = None,
        weights: str = "",
        candidates: list[Model] | None = None,
        value: ValueRule | None = None,
        mix: TokenMix = TokenMix(),
    ) -> list[Model]:
        """Eligible candidates in the same order used for execution and selection previews, ordered by `value`
        when given (see `ValueRule`)."""
        return self.frontier(available_only, runnable, weights, candidates, value, mix)[0]

    def choose(
        self,
        available_only: bool = True,
        runnable: Callable[[Model], bool] | None = None,
        weights: str = "",
        candidates: list[Model] | None = None,
        value: ValueRule | None = None,
        mix: TokenMix = TokenMix(),
    ) -> Model | None:
        """The selected model, or None when the requested policy cannot be satisfied."""
        ranked = self.ranked(available_only, runnable, weights, candidates, value, mix)
        return ranked[0] if ranked else None

    @staticmethod
    def validate_weights(weights: str) -> None:
        _parse_weights(weights)

    def explain(
        self,
        available_only: bool = True,
        runnable: Callable[[Model], bool] | None = None,
        candidates: list[Model] | None = None,
    ) -> str:
        """Why nothing qualified — which term excluded everyone, and how close anyone got."""
        pool = self._pool(available_only, runnable, candidates)
        if not pool:
            if runnable is None:
                return "no model is configured at all — add a provider key first"
            return ("nothing in the catalog is runnable through this CLI — it runs its own vendor's "
                    "models through its login; anything else needs a route and that provider's key")
        fit = self.qualifying(available_only, runnable, candidates)
        if fit:
            # The bar AS APPLIED, not as typed: `>= 50%` once picked a model ranked 81st while the
            # line said only "50%" — the two numbers couldn't be reconciled and the right answer
            # looked wrong. A percentile prints as the number it came out as.
            applied = " and ".join(str(t) for t in self.against_the_board())
            selection = "best measured match" if any(term.ranking for term in self.terms) else "cheapest match"
            # A ranking names the number it ranked on — else the only score on screen is
            # whatever the caller prints beside it, usually a different benchmark's.
            ranked_on = ", ".join(
                f"{term.field} {score:g}" for term in self.terms
                if term.ranking and (score := term.score_of(fit[0])) is not None
            )
            lead = f"{fit[0].id} ({ranked_on})" if ranked_on else fit[0].id
            return f"{len(fit)} model(s) clear {applied}; {selection} is {lead}"
        lines = []
        for term in self.against_the_board():
            # A board-relative bar always names itself, even alone: "nobody clears 99" reads only
            # once you see where the 90% bar landed on today's board.
            about = f"{term} — " if len(self.terms) > 1 or term.asked else ""
            kept = [m for m in pool if term.holds(m)]
            if kept:
                lines.append(f"  {about}{len(kept)} of {len(pool)} pass")
                continue
            scored = [(m.id, term.score_of(m)) for m in pool if term.score_of(m) is not None]
            if not scored:
                lines.append(f"  {about}nothing in the catalog is scored on '{term.field}'")
            else:
                near = max(scored, key=lambda pair: pair[1] or 0)
                lines.append(f"  {about}nobody passes; best is {near[0]} at {near[1]:g}")
        return "\n".join(lines)


def _parse_weights(text: str) -> dict[str, float]:
    if not text.strip():
        return {}
    weights: dict[str, float] = {}
    benchmark_variables = {benchmark.variable for benchmark in Benchmark.registry()}
    for clause in text.split(","):
        name, separator, raw = clause.partition("=")
        name = name.strip()
        if not separator or name not in benchmark_variables:
            raise CriteriaError(
                f"{name!r} is not a normalized benchmark; raw price, latency, and index units cannot be weighted"
            )
        try:
            value = float(raw)
        except ValueError as exc:
            raise CriteriaError(f"weight for {name!r} is not a number") from exc
        if not math.isfinite(value) or value < 0:
            raise CriteriaError(f"weight for {name!r} must be finite and non-negative")
        if name in weights:
            raise CriteriaError(f"duplicate weight for {name!r}")
        weights[name] = value
    total = sum(weights.values())
    if total <= 0:
        raise CriteriaError("criteria weights must have a positive total")
    return {name: value / total for name, value in weights.items()}
