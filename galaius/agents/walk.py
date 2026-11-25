"""The model walk: the ONE place that says which (model, login) a conversation's next turn runs on (owner 2026-11-25 « every agent
should have a dynamic table to refer to as to what model should be used, and if top models aren't available, we select the ones
under it until we find one »; 2026-11-20 « The harness should never dissalow if i have any form of usage »).

The table is the role's ranked list (`run.rank_candidates`), top row first; a unit is one row on one login. The walk takes the first
unit nothing shuts (`quota.shut`: a lost sign-in, a usage window, a pool, an overloaded API, whatever a refusal remembered), and
nothing waits while one can run. Start, resume, a refusal in the middle of work, an overload and a lost login all end here; a
step never goes by a second order. Nothing open: the unit that reopens first is returned as asking, and the vendor decides: a
request is never refused for what this PC remembers. A step to another provider is a successor carrying the conversation
(`fallback.hand_on`), to another login the session carried (`registry.carry`), to another model a new turn on the same session.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Literal, NamedTuple
from uuid import UUID

from galaius_core import ShutEntry

from galaius.agents.limits import quota
from galaius.agents.providers import PROVIDERS

if TYPE_CHECKING:
    from galaius.agents import registry as reg
    from galaius.agents.policy import Policy
    from galaius.agents.providers import AgentProvider

log = logging.getLogger(__name__)

#: What a step is for the conversation: the same unit it was on, another model of its login's program, another login of it (the
#: session carried), another program (a successor).
How = Literal["same", "model", "login", "successor"]


class Unit(NamedTuple):
    provider: str
    model: str
    login: UUID | None


class Passed(NamedTuple):
    """A unit above the one taken, and what shut it."""

    unit: Unit
    shut: ShutEntry


class Step(NamedTuple):
    """Where the next turn goes. `shut` None: `unit` can run. Else nothing can: `unit` reopens first and is asked all the same."""

    unit: Unit
    shut: ShutEntry | None
    passed: tuple[Passed, ...]

    @property
    def open(self) -> bool:
        return self.shut is None

    def how(self, run: "reg.AgentRun") -> How:
        if self.unit.provider != run.provider:
            return "successor"
        if self.unit.login != run.subscription_id:
            return "login"
        return "model" if self.unit.model != run.model else "same"


def _reopens(entry: ShutEntry) -> float:
    return math.inf if entry.until is None else entry.until


def walk(units: Iterable[Unit], *, now: float | None = None) -> Step | None:
    """The first of `units` (the table's order, each row's logins in order) nothing shuts; none open: the one reopening first
    (the table's order breaks a tie, a lost login reopens last: only a sign-in ends it). None when there is no unit at all."""
    passed: list[Passed] = []
    for unit in units:
        if (entry := quota.shut(unit.provider, unit.model, subscription=unit.login, now=now)) is None:
            return Step(unit, None, tuple(passed))
        passed.append(Passed(unit, entry))
    if not passed:
        return None
    first = min(passed, key=lambda one: _reopens(one.shut))
    return Step(first.unit, first.shut, tuple(passed))


def units_of(table: Sequence["reg.LaunchCandidate"], logins: Mapping[str, Sequence[UUID | None]]) -> list[Unit]:
    """The table's rows, each on every login its program may run on, in the table's order."""
    return [Unit(row.provider, row.model, login) for row in table for login in logins.get(row.provider, ())]


def logins_of(run: "reg.AgentRun", provider: str) -> list[UUID | None]:
    """The logins of this PC `provider`'s next turn of `run` may run on, the run's own first. A member's lent run runs on its one;
    a CLI no login runs (a key program, one with no login folder) has this PC's own. Subscription logins before this PC's own login
    (a run pinned to a lost subscription never waits while the own login has usage); a subscription at one of his caps is no login
    to walk."""
    from galaius_core import SubscriptionLimits   # noqa: PLC0415

    from galaius.agents.limits.subscriptions import RunSubscription, Subscription, SubscriptionRefused   # noqa: PLC0415
    own = run.subscription_id if provider == run.provider else None
    if run.pending_move is not None or run.kind != "process" or run.foreign or (run.subscription is not None and run.subscription.lent):
        return [own]
    program = PROVIDERS.get(provider)
    if program is None or not program.auth_home_env:
        return [own]
    others: list[UUID | None] = []
    for subscription in Subscription.all():
        if subscription.provider != provider or subscription.id == own:
            continue
        try:
            RunSubscription(subscription_id=subscription.id, limits=subscription.limits() or SubscriptionLimits()).check()
        except SubscriptionRefused:
            continue
        others.append(subscription.id)
    return [own, *others, None] if own is not None else [None, *others]


def _programs(run: "reg.AgentRun", policy: "Policy", *, across: bool) -> list[str]:
    """The programs the run's table may name: its own, then (`across`) every other one a conversation can move to: installed, able
    to resume, switched on in his policy, taking no key (a key spends cash: it is drawn off until he says), none for a member's
    lent run, a run of no process, one the owner holds a move for."""
    names = [run.provider]
    if not across or run.pending_move is not None or run.kind != "process" or run.foreign or not run.agent:
        return names
    if run.subscription is not None and run.subscription.lent:
        return names
    for name, program in PROVIDERS.items():
        if name != run.provider and program.can_resume and program.available() and policy.provider_active(name) \
                and not (getattr(program, "program", None) is not None and program.program.keyed):
            names.append(name)
    return names


def of_run(run: "reg.AgentRun", policy: "Policy | None", environment: Mapping[str, str], *, across: bool = True,
           context_tokens: int | None = None, now: float | None = None, own: "AgentProvider | None" = None) -> Step | None:
    """Where the next turn of stopped `run` goes: the walk over its role's table (`policy`'s rule for it). `across` False: its
    own program and the login it is pinned to alone, the model that turn is asked for (the move to another login or program is
    already made, `agent_queue.dispatch`). `own`: the program object `run` is on, when the caller holds it (a test double or a CLI not in the
    registry); else the registry's. A conversation of no role (his own, begun in an editor) has the table of its one model: only
    its logins are walked. A rule that cannot be read raises, as `rank` does."""
    from galaius.agents import run as run_module   # noqa: PLC0415 — circular: run imports the registry

    criterion = policy.criterion_for(run.agent) if policy is not None and run.agent else None
    if not criterion or policy is None:
        return walk((Unit(run.provider, run.model or "", login) for login in (logins_of(run, run.provider) if across else [run.subscription_id])), now=now)
    names = _programs(run, policy, across=across)
    table = run_module.rank_candidates(criterion, dict(environment), providers=[own if name == run.provider and own is not None else PROVIDERS[name] for name in names],
                                       weights=policy.weights_for(run.agent), role=run.agent)
    # A row two programs can run shares one rank (`run.rank`): the conversation's own program takes it, its session stays where it is.
    table = tuple(sorted(table, key=lambda row: (row.rank, row.provider != run.provider)))
    if context_tokens:
        window = lambda row: run_module._context_of(row.model, table)   # noqa: E731
        fits = [row for row in table if (size := window(row)) is None or size >= context_tokens]
        # A model whose window is below the conversation's size goes behind every model that fits (2026-10-23: a 1M-token conversation
        # put on a 200k model that could not compact it); when none fits the largest window is asked, and it compacts.
        table = (*fits, *sorted((row for row in table if row not in fits), key=lambda row: -(window(row) or 0)))
    logins = {name: logins_of(run, name) if across else [run.subscription_id] for name in names}
    return walk(units_of(table, logins), now=now)


def step_of(run: "reg.AgentRun", *, now: float | None = None) -> Step | None:
    """The step of `run` (`of_run` over the policy as it reads now): open, or asking when nothing is. None when its rule cannot be read
    now: nothing is claimed open, the turn is asked as before."""
    from galaius.account.caller import Caller   # noqa: PLC0415
    from galaius.agents import run as run_module   # noqa: PLC0415

    try:
        policy = run_module.load_policy().for_launch(run.agent, reference=run.agent_ref)[0] if run.agent else None
        return of_run(run, policy, dict(Caller.environ()), now=now)
    except Exception as error:   # noqa: BLE001 — a rule that cannot be read is no reason to move a conversation
        log.info("run %s: its model table could not be read for a step: %s", run.run_id[:8], error)
        return None


def open_for(run: "reg.AgentRun", *, now: float | None = None) -> Step | None:
    """`step_of(run)` when a unit can take its next turn, None when nothing can or its table cannot be read."""
    step = step_of(run, now=now)
    return step if step is not None and step.open else None


def nothing_can_run(run: "reg.AgentRun") -> bool:
    """Whether every unit of `run`'s table is shut now (a table that cannot be read is no proof: False): its turn is never asked of one."""
    step = step_of(run)
    return step is not None and not step.open
