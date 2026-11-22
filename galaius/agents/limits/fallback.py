"""The last rung of « never stop his request while any login of any provider has usage » (owner 2026-11-20): a conversation whose
provider has nothing left to answer it — every model of it shut on every login of his (`quota.blocked_until`), or the login
paused — continues as a SUCCESSOR run on another provider (`SpawnLine.hand_on`: the ranked walk across providers,
`relations.succeed`), carrying the message that was waiting and where the conversation can be read. The rungs before it
are the model (`run.resolve_continuable_model`) and his other login of the same provider (`registry.swap_login`); this one
is taken only when neither exists, and never moves a session across providers (a session is its CLI's own format).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from galaius.account.caller import RUN_VARIABLES, Caller
from galaius.agents import registry as reg
from galaius.agents.limits import quota
from galaius.agents.limits.subscriptions import Subscription
from galaius.agents.providers import PROVIDERS

log = logging.getLogger(__name__)

#: How long the spawn of a successor may take to be handed over (`galaius agents spawn` returns once its run is in line).
SPAWN_SECONDS = 120


def _shut(provider: str, login, models: Iterable[str]) -> bool:
    """`login` takes no turn of any of `models`: paused whole, or each model passed over on it."""
    return quota.login_pause(provider, subscription=login) is not None or all(
        quota.blocked_until(provider, model, subscription=login) is not None for model in models)


def exhausted(run: reg.AgentRun, models: Iterable[str]) -> bool:
    """Whether stopped `run`'s own provider can answer nothing now: every one of `models` (what its rule lists, its own included)
    is passed over, or the login paused, on every login of that provider on this PC (the one it is pinned to and each subscription),
    AND another provider is installed, switched on in his policy and with its login open. Never for a run the owner holds a move for (`pending_move`), a member's
    lent run, a CLI no login runs, or a run of no process."""
    models = tuple(dict.fromkeys(model for model in models if model))
    if not models or run.pending_move is not None or run.kind != "process" or run.foreign or not run.agent:
        return False
    if run.subscription is not None and run.subscription.lent:
        return False
    logins = {run.subscription_id, *(one.id for one in Subscription.all() if one.provider == run.provider)}
    if not all(_shut(run.provider, login, models) for login in logins):
        return False
    return any(other.name != run.provider and other.can_resume and other.available() and quota.login_pause(other.name) is None and _active(other.name)
               for other in PROVIDERS.values())


def _active(provider: str) -> bool:
    """The owner's policy has `provider` switched on: a CLI he turned off is never the one his conversation moves to (the ranked walk
    skips it too, `Policy.provider_active`). False when the policy cannot be read now."""
    from galaius.agents.run import load_policy   # noqa: PLC0415 — circular: run imports this package

    try:
        return load_policy().provider_active(provider)
    except Exception as error:   # noqa: BLE001 — an unreadable policy moves nothing
        log.info("the provider policy could not be read for a hand-off: %s", error)
        return False


def candidates(run: reg.AgentRun) -> tuple[str, ...]:
    """The models of `run`'s own provider its rule lists now, its own first; empty when the rule cannot be read now (the service
    is down, no criterion): nothing is then called exhausted, the turn is resumed as before."""
    from galaius.account.caller import Caller   # noqa: PLC0415 — `run` imports the registry, which imports this package
    from galaius.agents import run as run_module   # noqa: PLC0415 — circular: run imports limits

    try:
        policy, _ = run_module.load_policy().for_launch(run.agent, reference=run.agent_ref)
        criterion = policy.criterion_for(run.agent)
        listed = run_module.rank_candidates(criterion, dict(Caller.environ()), providers=[PROVIDERS[run.provider]],
                                            weights=policy.weights_for(run.agent), role=run.agent) if criterion else ()
    except Exception as error:   # noqa: BLE001 — a rule that cannot be read is no reason to move a conversation
        log.info("run %s: its model rule could not be read for a hand-off: %s", run.run_id[:8], error)
        return ()
    return tuple(dict.fromkeys(model for model in (run.model, *(entry.model for entry in listed)) if model))


def moves(run: reg.AgentRun) -> bool:
    """Whether stopped `run`'s next turn goes to another provider (`exhausted` over what its rule lists): asked first of the one
    cheap fact that settles most runs, its own login passing its own model over, before its rule is read."""
    if quota.login_pause(run.provider, subscription=run.subscription_id) is None and (
            run.model is None or quota.blocked_until(run.provider, run.model, subscription=run.subscription_id) is None):
        return False
    return exhausted(run, candidates(run))


def _successor(run: reg.AgentRun, task: str, relaunches: reg.Relaunch | None) -> str | None:
    """A successor of `run` started as `SpawnLine.hand_on` starts it, `task` its brief; its run id, None when it could not be
    started (logged). The run it asks as is the stopped run's, never whichever run's environment this process was started from."""
    from galaius.agents.spawn_line import SpawnLine   # noqa: PLC0415 — circular: spawn_line imports run, which imports this package
    from galaius.agents.run import run_agent   # noqa: PLC0415 — circular, as above

    environment = {name: value for name, value in os.environ.items() if name not in RUN_VARIABLES}
    caller = Caller.current().model_copy(update={"environment": environment})

    def start() -> str:
        with caller.bound():
            return asyncio.run(SpawnLine.here().hand_on(run, task, functools.partial(run_agent, environment=environment), relaunches=relaunches))

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:   # a thread of its own: its caller may run inside an event loop
            return pool.submit(start).result()
    except Exception as error:   # noqa: BLE001 — a successor that cannot start leaves the turn to its caller
        log.warning("could not hand run %s on to a successor: %s", run.run_id[:8], error)
        return None


def hand_on(run: reg.AgentRun, message: str) -> str | None:
    """Stopped `run`'s conversation continued by a successor on another provider (`SpawnLine.hand_on`: the ranked walk across
    providers, its own passed over by its notes), `message` its task with where the conversation can be read. The successor's run
    id, None when it could not be started (logged). Never called under `run`'s record lock: `relations.succeed` takes it."""
    return _successor(run, f"{message}\n\n[Galaius: you continue run {run.run_id[:8]} ({run.name}), which ran on {run.provider} and could not take this "
                           f"message: every model of its provider is at its usage limit on every login. Its conversation: `galaius agents events {run.run_id}`.]", None)


#: The errors that are this computer's own code failing, never a cause waiting changes: a resume that raises one is not tried again.
CODE_FAULTS = (AttributeError, TypeError, NameError, LookupError, AssertionError, ImportError)
#: How much of the ended run's last report its fresh successor is handed.
REPORT_CHARS = 4000


def code_fault(error: BaseException) -> bool:
    """Whether `error` is a bug of this computer's code (`CODE_FAULTS`): a resume it broke is repaired by a fresh run, at once."""
    return isinstance(error, CODE_FAULTS)


def relaunch_fresh(run: reg.AgentRun, message: str, error: BaseException | str) -> str | None:
    """Ended `run` could not be resumed for a code reason (`code_fault`: 2026-11-22 a resume read a field its model has not, and four
    critic reports never reached their makers): a FRESH run of its role, in its place, carries `message` (the one it could not take)
    and `run`'s last report; each records the other (`relaunched_from`, `replaced_by`), what is sent to `run` later reaches it
    (`relations.succeed`). The repair never depends on the path that broke: it starts a run, resumes nothing. Its run id, None when
    it could not be started (logged). Never called under `run`'s record lock."""
    from galaius.agents.catalog import relations   # noqa: PLC0415 — circular, as `_successor`

    why = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else error
    last = "\n\n".join(relations.final_parts(run.run_id))[-REPORT_CHARS:]
    task = (f"{message}\n\n[Galaius: you continue run {run.run_id[:8]} ({run.name}), which could not be resumed ({why[:300]}). "
            f"Its conversation: `galaius agents events {run.run_id}`." + (f" Its last report:\n{last}" if last else "") + "]")
    return _successor(run, task, reg.Relaunch(run_id=run.run_id, provider=run.provider, model=run.model, said=why[:reg.RELAUNCH_SAID_MAX], cause="fault"))
