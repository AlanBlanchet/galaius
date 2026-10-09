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

from galaius import benchmark_source, benchmark_tables, model_catalog, ollama
from galaius.models import Benchmark, Model, ModelCapability, PublishedEntry, TokenMix
from galaius.ttl_cache import TTLCache


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
    live = benchmark_tables.load_tables().get(bench.id)
    published = (
        live
        if live is not None
        and (bench.published is None or live.retrieved >= bench.published.retrieved)
        else bench.published
    )
    if published is None:
        return []
    return [entry for entry in published.entries if entry.qualifies(published.freshness)]


def _bench_reader(bench: Benchmark) -> Callable[[Model], float | None]:
    def read(model: Model) -> float | None:
        measured = bench.score_for(model)
        if measured is not None:
            return measured
        for entry in _bench_entries(bench):
            scored = Model.by_id(entry.model_id) if entry.model_id else None
            if scored is not None and scored.id == model.id:
                return entry.normalized_score
        return None

    return read


def _board_scores() -> list[float]:
    """What Artificial Analysis currently publishes, as numbers."""
    return list(model_catalog.live_scores().values())


def _board_measured(model: Model) -> bool:
    """Whether Artificial Analysis has a row for this model — not merely whether galaius holds a
    number for it, which may be the shipped snapshot's."""
    from galaius.model_catalog import bare_model_name, live_scores

    return bare_model_name(model.id) in live_scores()


def _bench_measured(bench: Benchmark) -> Callable[[Model], bool] | None:
    """Whether this benchmark's source or published table lists the model."""
    from galaius.model_catalog import bare_model_name

    def listed(model: Model) -> bool:
        if bench.score_for(model) is not None:
            return True
        key = bare_model_name(model.id)
        return any(bare_model_name(entry.model_id or entry.model_name) == key
                   for entry in _bench_entries(bench))

    return listed


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
    #: A bare numeric metric is a ranking request. It still requires a measured value, but does
    #: not create a zero-valued threshold.
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


#: Why a model left a ranking: another one is at least as good for no more money, or nearly as
#: good for a fraction of the price.
DropRule = Literal["dominated", "small_edge"]


class Dropped(BaseModel):
    """A model a ranking left out, and the model that made it pointless — with the numbers."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    model: Model
    by: Model
    rule: DropRule
    score: tuple[float, ...]
    by_score: tuple[float, ...]
    cost: float
    by_cost: float
    #: The rule's own bounds, so the sentence states the bar that applied.
    ceiling: float
    edge: float

    def __str__(self) -> str:
        score = "/".join(f"{value:g}" for value in self.score)
        by_score = "/".join(f"{value:g}" for value in self.by_score)
        if self.rule == "dominated":
            return (f"{self.model.id}: dominated by {self.by.id} — score {by_score} ≥ {score}, "
                    f"price ${self.by_cost:.2f} ≤ ${self.cost:.2f} per M tokens")
        return (f"{self.model.id}: {self.by.id} scores {by_score}, less than {self.edge:g} points "
                f"below {score}, for ${self.by_cost:.2f} vs ${self.cost:.2f} per M tokens "
                f"(over {self.ceiling:g}x)")


class ValueRule(BaseModel):
    """Intelligence against cost: which models a ranking may never run, not even as a fallback.

    Two drops, both relative to another model in the SAME pool — so a model leaves the list
    because something runnable there is a better deal, never because of its name:

    - dominated: another model scores at least as high on every measure AND costs no more
      (strictly better on one side; an exact twin is kept as a second route);
    - small edge: another model costs ``ceiling`` times less and scores less than ``edge`` points
      below — a big price for a small gain. Points are out of 100 of the measure's range, so this
      drop needs a measure with a known scale.

    Price is one number per model: catalog rates blended by the workload's `TokenMix`. A model
    with no known price is never dropped and never drops another — an unknown price proves
    nothing. Models are walked cheapest first and only KEPT models drop others, so the outcome
    does not depend on catalog order.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    ceiling: float = Field(default=1.5, ge=1)
    edge: float = Field(default=2.0, ge=0)

    def prune(
        self, models: Sequence[Model], score: Callable[[Model], tuple[float, ...] | None],
        mix: TokenMix, *, small_edge: bool = True,
    ) -> tuple[list[Model], list[Dropped]]:
        """``models`` minus every dropped one, order kept, and why each left."""
        valued = [(index, model, score(model), model.blended_cost(mix)) for index, model in enumerate(models)]
        walk = sorted(
            ((index, model, value, cost) for index, model, value, cost in valued if value is not None and cost is not None),
            key=lambda item: (item[3], tuple(-v for v in item[2]), item[0]),
        )
        keepers: list[tuple[Model, tuple[float, ...], float]] = []
        dropped: dict[int, Dropped] = {}
        for index, model, value, cost in walk:
            for by, by_value, by_cost in keepers:
                rule = self._beats(by_value, by_cost, value, cost, small_edge)
                if rule is not None:
                    dropped[index] = Dropped(
                        model=model, by=by, rule=rule, score=value, by_score=by_value,
                        cost=cost, by_cost=by_cost, ceiling=self.ceiling, edge=self.edge,
                    )
                    break
            else:
                keepers.append((model, value, cost))
        return ([model for index, model in enumerate(models) if index not in dropped],
                [dropped[index] for index in sorted(dropped)])

    def _beats(
        self, by_value: tuple[float, ...], by_cost: float, value: tuple[float, ...], cost: float,
        small_edge: bool,
    ) -> DropRule | None:
        pairs = list(zip(by_value, value, strict=True))
        if by_cost <= cost and all(b >= v for b, v in pairs) and (by_cost < cost or by_value != value):
            return "dominated"
        if small_edge and by_cost * self.ceiling < cost and all(v - b < self.edge for b, v in pairs):
            return "small_edge"
        return None


@dataclass(frozen=True)
class Criteria:
    """A model requirement, as a sentence somebody can read and change."""

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
                    terms.append(Term(name, ranking=True, source_only=True))
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
                *[variable.read(model) * (-1 if variable.higher_is_better else 1) for variable in variables],
                model.thrift[0], model.thrift[1], model.id,
            ))
            return fit
        fit.sort(key=lambda m: m.thrift)
        return fit

    def _ranking_variables(self) -> list[Variable]:
        variables = [Variables.by_name(term.field) for term in self.terms if term.ranking]
        return [v for v in variables if v is not None] if all(v is not None for v in variables) else []

    def _value_variables(self) -> list[Variable]:
        """The measures a model's worth is read on: the bare metrics it ranks on, else the measured
        ones its bars compare (`aa.intelligence >= 90%` ranks cheapest-first, yet a model clearing it
        that is weaker AND pricier than another is still never worth running). Prices are the
        other axis, never a score."""
        ranking = self._ranking_variables()
        if ranking:
            return ranking
        named = dict.fromkeys(term.field for term in self.terms if term.op)
        variables = [Variables.by_name(name) for name in named]
        return [v for v in variables if v is not None and v.rankable]

    def value_score(self, weights: str = "") -> Callable[[Model], tuple[float, ...] | None]:
        """What "scores at least as high" means for this criterion, per model, in POINTS OUT OF 100
        of each measure's declared range: its weighted score when weights are given, else every
        bare metric it ranks on, higher always better. A measure with no declared range stays on
        its own scale (only `edge_known` tells the two apart). None for a model without that
        number, and for every model when the criterion names no measure at all."""
        parsed = _parse_weights(weights)
        if parsed:
            pairs = [(Variables.by_name(name), weight) for name, weight in parsed.items()]

            def weighted(model: Model) -> tuple[float, ...] | None:
                scores = [(_weighted_value(variable, model), weight) for variable, weight in pairs]
                if any(score is None for score, _weight in scores):
                    return None
                return (100 * sum(score * weight for score, weight in scores if score is not None),)
            return weighted
        variables = self._value_variables()

        def measured(model: Model) -> tuple[float, ...] | None:
            if not variables:
                return None
            points: list[float] = []
            for variable in variables:
                value = variable.read(model)
                if value is None:
                    return None
                if variable.score_range is not None:
                    worst, best = variable.score_range
                    value = 100 * (value - worst) / (best - worst)
                points.append(value if variable.higher_is_better else -value)
            return tuple(points)
        return measured

    def edge_known(self, weights: str = "") -> bool:
        """Whether a POINT of this criterion's score means something: weighted scores and measures
        with a declared range are out of 100; a measure without one has no size for "a small edge"."""
        return bool(_parse_weights(weights)) or all(
            variable.score_range is not None for variable in self._value_variables())

    def prune(
        self, value: ValueRule, models: Sequence[Model], weights: str = "", mix: TokenMix = TokenMix(),
    ) -> tuple[list[Model], list[Dropped]]:
        """`value` applied with this criterion's score, prices blended by `mix`."""
        return value.prune(models, self.value_score(weights), mix, small_edge=self.edge_known(weights))

    def frontier(
        self,
        available_only: bool = True,
        runnable: Callable[[Model], bool] | None = None,
        weights: str = "",
        candidates: list[Model] | None = None,
        value: ValueRule | None = None,
        mix: TokenMix = TokenMix(),
    ) -> tuple[list[Model], list[Dropped]]:
        """The ranking, and the models `value` left out of it — none unless a `ValueRule` is given:
        the rule is a POLICY choice, read once by its owner (`galaius.agents.run.rank`) and handed
        here, never a default some callers apply and others do not."""
        fit = self.qualifying(available_only, runnable, candidates)
        parsed = _parse_weights(weights)
        if parsed:
            score = self.value_score(weights)
            weighted = [(scored[0], model) for model in fit if (scored := score(model)) is not None]
            weighted.sort(key=lambda pair: (-pair[0], pair[1].thrift, pair[1].id))
            fit = [model for _score, model in weighted]
        if value is None:
            return fit, []
        return self.prune(value, fit, weights, mix)

    def ranked(
        self,
        available_only: bool = True,
        runnable: Callable[[Model], bool] | None = None,
        weights: str = "",
        candidates: list[Model] | None = None,
        value: ValueRule | None = None,
        mix: TokenMix = TokenMix(),
    ) -> list[Model]:
        """Eligible candidates in the same order used for execution and selection previews,
        without the models `value` rules out (see `ValueRule`)."""
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
