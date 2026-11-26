"""Which model an agent resolves to: criteria, variables, policy."""

import json
import os
import sys
from typing import cast
from galaius.catalog import models as model_catalog
from galaius.agents import providers as agent_providers
from galaius.agents.policy import ParadigmProjection, Policy, PolicyError, policy_path
from galaius.agents.providers import (
    PROVIDERS,
)
from galaius.agents.run import ModelUnavailable, is_criterion, Ranking, rank, resolve_model
from galaius.catalog.criteria import Criteria, CriteriaError, Variables
from galaius.catalog.models import bare_model_name as _bare_model_name
from galaius.models.registry import Model, _ordinal
from galaius.cli.commands.agents.app import agents_app


@agents_app.command(name="variables")
def agents_variables() -> None:
    """Every comparison a model criterion can make, and who measured each one.

    Discovery surface for writing a criterion: can't write ``aa.mmmu > 0.7`` if nothing says it
    exists. Each variable is namespaced by its SOURCE — a bare ``intelligence`` hides who measured
    it, and two leaderboards rarely agree.
    """

    rows = Variables.all()
    width = max(len(v.name) for v in rows)
    for v in sorted(rows, key=lambda v: v.name):
        kind = "yes/no  " if v.flag else "measure "
        print(f"  {v.name:<{width}}  {kind} {v.describe}")
    print("\nWrite one as  cap.vlm and gui.screenspot > 0.85 and price.in < 10  — cheapest that"
          " clears every term is used, and re-resolved at every spawn.")


def _per_vendor(ranking: Ranking, policy: Policy) -> dict[str, str | None]:
    """What each switched-on vendor CLI would ACTUALLY run: its first candidate in the one ranked
    list a spawn walks — a CLI given alone gets that same list, filtered (`rank`)."""
    answers: dict[str, str | None] = {}
    # Read THROUGH the module: which CLIs exist is a fact about the caller's environment — a name
    # bound at import time would freeze whatever the process started with.
    for name in agent_providers.PROVIDERS:
        if not policy.provider_active(name):
            continue
        first = next((c for c in ranking.candidates if c.provider == name), None)
        answers[name] = None if first is None else resolve_model(
            first.model, dict(os.environ), provider=agent_providers.provider_for(name))[1]
    return answers


@agents_app.command(name="criterion")
def agents_criterion(criterion: str, json_out: bool = False, role: str | None = None) -> None:
    """Resolve what a criterion runs as: the first of the ONE ranked list a spawn walks.

    "We shouldn't write a model, but resolve a model from the constraints." A pinned id freezes
    when typed; a criterion is that claim written down instead, re-read every time it's asked.

    The answer is `rank`'s — the call `agents policy` and every spawn make: the models a switched-on CLI can
    run, the provider terms (`provider ~ claude`) applied, ordered by the cost relation of `--role` (none: the
    default relation), a model the vendor refused out of it. A rule this galaius cannot rank says so and names
    no model: silently answering with some other model looks like it worked.
    """
    answer: dict[str, object] = {"criterion": criterion, "model": None, "role": role}

    def refuse(why: str) -> None:
        if json_out:
            print(json.dumps({**answer, "why": why}))
            return
        print(f"nothing resolves {criterion!r}: {why}")
        raise SystemExit(1)

    try:
        parsed = Criteria.parse(criterion)
    except CriteriaError as err:
        if json_out:
            print(json.dumps({**answer, "why": str(err)}))
            return
        print(f"that criterion cannot be evaluated: {err}")
        raise SystemExit(1)
    if not is_criterion(criterion):
        # A spawn reads this text as a model id, so no ranking is made of it (`is_criterion`'s shape test, the spawn's own).
        refuse("a spawn reads this as a model id, not a criterion: add a bar or a measure (`aa.intelligence and provider ~ claude`)")
        return
    policy = Policy.load()
    try:
        ranking = rank(criterion, dict(os.environ), providers=list(agent_providers.PROVIDERS.values()),
                       weights=policy.weights_for(role) if role else "", role=role, policy=policy)
    except ModelUnavailable as err:
        refuse(str(err))
        return
    first = ranking.candidates[0]
    row = Model.named(first.catalog_id) if first.catalog_id else Model.by_id(first.model)
    valued = next((item for item in ranking.valued if item.model.catalog_id == first.catalog_id), None)
    answer.update({
        "model": first.model,
        "provider": first.provider,
        "score": valued.score if valued is not None else (row.intelligence_score if row is not None else None),
        "competence": row.competence() if row is not None else None,
        "ranked": [candidate.model_dump() for candidate in ranking.candidates[:3]],
        "valued": [{"model": item.model.id, "score": item.score, "cost": round(item.cost, 4), "value": round(item.value, 2)}
                   for item in ranking.valued[:3]],
        # What each switched-on CLI would run: the answer a spawn will actually get.
        "providers": _per_vendor(ranking, policy),
        "why": f"{len(ranking.candidates)} candidate(s) clear {parsed}; first of the list a spawn walks is {first.provider}/{first.model}",
    })
    if json_out:
        print(json.dumps(answer))
        return
    print(f"{parsed}  →  {first.provider}/{first.model}")
    if row is not None:
        print(f"  {row.competence()}")
    for item in ranking.valued[:3]:
        print(f"  {item}")
    print(f"  {answer['why']}")


@agents_app.command(name="models")
def agents_models(
    limit: int = 0, json_out: bool = False, names: list[str] | None = None,
) -> None:
    """The models the registry has a capability score for, best first, ONE ROW PER MODEL.

    "Models should also spell out their intelligence score, such that we can compare the most
    competents and trust one agent more than another in some situations (isn't absolute)" — a
    choice is only a comparison if candidates are distinct, so a model's provider aliases
    (`azure/gpt-5.5`, `azure_ai/gpt-5.5-2026-04-23`, `vertex_ai/…`) collapse into one row carrying
    the plainest id: forty rows of four facts compared nothing. Rank is over distinct models. One
    measure among several — a lower-scored model is often the better fit for a narrow job.
    """
    # RANKED ON ONE VINTAGE. Registry keeps every baked score — selection/criteria need whatever
    # is known about a model — but a ranking PRINTED beside the panel's Benchmarks tab must be the
    # board that tab shows, or the two disagree in public about which model leads. Read THROUGH
    # the module, never bound by name: a board a test points elsewhere must move this command too.
    live = model_catalog.live_scores()

    class _Row:
        __slots__ = ("id", "intelligence_score")

        def __init__(self, id: str, intelligence_score: float | None) -> None:
            self.id, self.intelligence_score = id, intelligence_score

    # Every model this machine can REACH, scored by the board DIRECTLY. Reading the score off the
    # registry instead inverted the ask: registry holds none of the board's top models, so the
    # most competent ones printed "nothing scored carries this name" and every number on screen
    # approximated a superseded model. Ids come from registry AND browse catalogue — a model can
    # be routable through either.
    reachable: dict[str, _Row] = {}
    for model_id in [m.id for m in Model.catalog()] + [
        m.id for m in (model_catalog.load_catalog().models if live else [])
    ]:
        if model_id in reachable:
            continue
        measured = live.get(_bare_model_name(model_id)) if live else None
        if measured is None and live:
            continue
        reachable[model_id] = _Row(model_id, measured)
    if not live:  # no board on disk: fall back to whatever the registry knows
        reachable = {m.id: _Row(m.id, m.intelligence_score) for m in Model.catalog()
                     if m.intelligence_score is not None}
    scored = sorted(
        reachable.values(),
        key=lambda m: (-(m.intelligence_score or 0.0), len(m.id), m.id),
    )
    # One entry per distinct model: same score AND same bare name (id minus provider prefixes,
    # regions, date suffixes) is the same model resold under another name.
    distinct: dict[tuple[float, str], dict] = {}
    for m in scored:
        key = (m.intelligence_score or 0.0, _bare_model_name(m.id))
        entry = distinct.get(key)
        if entry is None:
            distinct[key] = {"id": m.id, "score": m.intelligence_score, "aliases": 0, "covers": []}
        else:
            entry["aliases"] += 1
            # Every absorbed id named: a SECOND list (browse catalogue) can drop what's already
            # ranked above it, instead of offering `openai/gpt-5.5` as "not scored" right under
            # `gpt-5.5` at "60.2 · 1st" — the picker contradicting itself.
            entry["covers"].append(m.id)
    rows = list(distinct.values())
    # Rank over the BOARD, never what this machine happens to reach. Denominator used to be
    # "models you can route to", so one model once read 10th from one directory, 13th from
    # another with different credentials in scope — a local-availability measure wearing an
    # intelligence label. Which models are LISTED still depends on what you can reach; the PLACE
    # doesn't. Within that, rank by COMPETITION: equal scores share one rank, say "joint" —
    # enumerating would manufacture the difference the measure denies.
    population = sorted(live.values(), reverse=True) or [r["score"] for r in rows]
    for row in rows:
        ahead = sum(1 for value in population if value > row["score"])
        tied = sum(1 for value in population if value == row["score"])
        joint = "joint " if tied > 1 else ""
        row["competence"] = (
            f"aa.intelligence {row['score']:.1f} · {joint}{_ordinal(ahead + 1)} of {len(population)}"
        )
    ranked_total = len(rows)  # before any --limit: the population the RANK is over
    listed = rows[:limit] if limit else rows
    # Rows a picker puts NEAREST THE TOP are aliases, not ids: Claude Code's `sonnet`/`opus` tiers,
    # whatever the agent runs on today. Unscored, they made the one comparison anyone makes — keep
    # the incumbent, or switch — impossible, so each resolves to the best-scored model it stands
    # for, carrying THAT model's score. An alias nothing scores stays absent.
    resolved = []
    for alias in names or []:
        want = _bare_model_name(alias)
        # EXACT first. A containment match is how a family word (`sonnet`, `opus`) finds the model
        # wearing it, but once let `claude-fable-5` take `claude-fable-5.1`'s number — a different
        # model four places higher — with nothing marking the swap, purely because one name
        # prefixes the other.
        hit = next((r for r in rows if _bare_model_name(r["id"]) == want), None)
        if hit is not None:
            resolved.append({**hit, "asked": alias})
            continue
        hit = next((r for r in rows if want in _bare_model_name(r["id"])), None)
        if hit is not None:
            # A bare family word asks for no particular version, so nothing was substituted. A
            # name carrying its OWN version did — an approximation that must say so.
            versioned = any(ch.isdigit() for ch in want)
            resolved.append({**hit, "asked": alias, **({"approximate": True} if versioned else {})})
            continue
        # Registry is OLDER than the fleet: an agent runs `claude-sonnet-5` while ranking still
        # knows `claude-sonnet-4-6`, so an exact match scores nothing and the one comparison
        # anyone makes has no left-hand side. Fall back to the closest ranked RELATIVE — row
        # sharing the most leading name-parts — flagged approximate so no surface passes it off as
        # the model itself. Sharing fewer than two parts is not a relative.
        parts = want.split("-")
        best, best_shared = None, 1
        for row in rows:  # rows are ranked, so the first of a tie is the best-scored
            other = _bare_model_name(row["id"]).split("-")
            shared = 0
            for mine, theirs in zip(parts, other):
                if mine != theirs:
                    break
                shared += 1
            if shared > best_shared:
                best, best_shared = row, shared
        if best is not None:
            resolved.append({**best, "asked": alias, "approximate": True})
    rows = listed + resolved
    if json_out:
        print(json.dumps(rows))
        return
    # Naming the SCOPE: the panel's Benchmarks tab shows the whole board, and one tab once called
    # a model first while another had it lower behind a different leader, with nothing saying this
    # one ranks only what this machine can route to. Header names BOTH populations — the
    # difference is the point: board ranks everyone, this list shows what you can actually run.
    print(
        f"aa.intelligence is one measure, not a verdict — {ranked_total} of the "
        f"{len(population)} models on the Artificial Analysis board are routable from here, and "
        "the place each holds is its place on that whole board.\n"
    )
    for row in rows:
        also = f"  (+{row['aliases']} provider aliases)" if row["aliases"] else ""
        stands_for = f"  [{row['asked']}]" if row.get("asked") else ""
        print(f"{row['score']:5.1f}  {row['id']}{also}{stands_for}")


#: How many models' score, price and value `agents policy` prints under each rule (`Ranking.valued`; --json-out: all).
VALUED_SHOWN = 5


def _switched_off(ranking: Ranking, policy: Policy) -> list[str]:
    """Providers of this ranking that policy switches off. When it names every candidate's, the
    role cannot start at all — said where the rule is shown, never first at the refused spawn."""
    return list(dict.fromkeys(item.provider for item in ranking.candidates if not policy.provider_active(item.provider)))


@agents_app.command(name="policy")
def agents_policy(json_out: bool = False) -> None:
    """Active role policy: server catalog when configured, otherwise local policy.

    Each agent row shows the rule as WRITTEN and what it currently RESOLVES to, so a criterion
    quietly matching nothing is visible here rather than at the spawn that fails.

    `--json-out` hands the panel the same answer — otherwise it can only print the rule, leaving a
    reader unable to tell a RESOLUTION from a model somebody typed, the distinction the mechanism
    exists for.
    """
    policy = Policy.load()
    if json_out:
        # One shape test, one resolver, both sides: panel must not re-derive "is this a criterion"
        # in TypeScript, where it'd drift from the spawn's own answer.
        agents = []
        for agent in policy.agents:
            rule = policy.criterion_for(agent) or ""
            resolves: str | None = rule
            why = None
            ranking = Ranking(candidates=())
            if is_criterion(rule):
                try:
                    ranking = rank(rule, dict(os.environ), providers=list(agent_providers.PROVIDERS.values()), weights=policy.weights_for(agent), role=agent, policy=policy)
                    resolves = ranking.candidates[0].model
                except ModelUnavailable as err:
                    resolves, why = None, str(err).splitlines()[0]
            agents.append({
                "name": agent, "rule": policy.agents[agent], "criterion": is_criterion(rule),
                "resolves": resolves, "why": why,
                "ranked": [candidate.model_dump() for candidate in ranking.candidates[:3]],
                "switched_off": _switched_off(ranking, policy),
                "valued": [{"model": item.model.id, "score": item.score, "cost": round(item.cost, 4), "value": round(item.value, 2)}
                           for item in ranking.valued],
                "providers": _per_vendor(ranking, policy) if is_criterion(rule) else {},
            })
        print(json.dumps({
            "policy": "server catalog" if policy.catalog is not None else str(policy_path()),
            "catalog_cursor": policy.catalog.snapshot.cursor if policy.catalog is not None else None,
            "stale": policy.catalog.stale if policy.catalog is not None else False,
            "ignored": list(policy.ignored),
            "profiles": dict(policy.profiles), "agents": agents,
            "paradigms": {
                agent: [{"paradigm": a.paradigm, "as": a.projection} for a in assignments]
                for agent, assignments in policy.paradigms.items()
            },
        }))
        return
    print(f"policy: {'server catalog ' + policy.catalog.snapshot.cursor if policy.catalog is not None else policy_path()}")
    if policy.ignored:
        print(f"not obeyed here (the workspace's rules decide): {', '.join(policy.ignored)} in {policy_path()}")
    if policy.profiles:
        print("\nprofiles")
        for name, rule in policy.profiles.items():
            print(f"  @{name:<14} {rule}")
    if policy.agents:
        print("\nagents")
        for agent, rule in policy.agents.items():
            resolved = policy.criterion_for(agent) or ""
            shown = rule if rule == resolved else f"{rule}  →  {resolved}"
            note = ""
            ranking = Ranking(candidates=())
            if is_criterion(resolved):
                try:
                    ranking = rank(resolved, dict(os.environ), providers=list(agent_providers.PROVIDERS.values()), weights=policy.weights_for(agent), role=agent, policy=policy)
                    off = _switched_off(ranking, policy)
                    shown_ranked = " → ".join(f"{item.provider}/{item.model}" + (" (off)" if item.provider in off else "")
                                              for item in ranking.candidates[:3])
                    if off and all(item.provider in off for item in ranking.candidates):
                        note = f"  ⇒ NOTHING CAN START: ranked {shown_ranked}; provider {', '.join(off)} switched off"
                    else:
                        note = f"  ranked: {shown_ranked} (availability checked at start)"
                except ModelUnavailable as err:
                    note = "  ⇒ NOTHING (" + str(err).splitlines()[0].rstrip(":") + ")"

            print(f"  {agent:<16} {shown}{note}")
            for item in ranking.valued[:VALUED_SHOWN]:
                print(f"  {'':<16}   {item}")
            if len(ranking.valued) > VALUED_SHOWN:
                print(f"  {'':<16}   + {len(ranking.valued) - VALUED_SHOWN} more further down the list (--json-out lists them)")
    if policy.toolsets:
        print("\ntoolsets")
        for name in policy.toolsets:
            print(f"  @{name:<14} {', '.join(policy.expand_toolset(name))}")
    if policy.agent_tools:
        print("\nagent tools")
        for agent in policy.agent_tools:
            print(f"  {agent:<16} {', '.join(policy.tools_for(agent))}")
    if policy.paradigms:
        print("\nparadigms")
        for agent, assignments in policy.paradigms.items():
            worn = ", ".join(f"{a.paradigm} (as {a.projection})" for a in assignments)
            print(f"  {agent:<16} {worn}")
    print("\nproviders")
    for name in PROVIDERS:
        print(f"  {name:<8} {'on' if policy.provider_active(name) else 'off'}")


@agents_app.command(name="paradigm-assign")
def agents_paradigm_assign(agent: str, paradigm: str, projection: str) -> None:
    """Give AGENT a PARADIGM, projected as "skill" (loaded on demand) or "system_prompt" (always
    present). Writes ~/.galaius/agents.json — the one file the spawn reads.

    "a user doesn't edit a 'system prompt' or a 'skill', but a paradigm, and choses to add it to
    an agent as 'skill' or as 'system prompt'." Same paradigm can be a skill for one agent, baked
    into another's system prompt — choice is per agent, never global to the paradigm.
    """
    if projection not in ("skill", "system_prompt"):
        print(f"projection must be 'skill' or 'system_prompt', not {projection!r}", file=sys.stderr)
        raise SystemExit(2)
    try:
        policy = Policy.load()
        policy.assign_paradigm(agent, paradigm, cast(ParadigmProjection, projection))
    except PolicyError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        raise SystemExit(2) from err
    print(f"{agent} now carries {paradigm!r} as {projection}.")
