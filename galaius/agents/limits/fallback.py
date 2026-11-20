"""The last rung of « never stop his request while any login of any provider has usage » (owner 2026-11-20): a conversation whose
provider has nothing left to answer it — every model of it shut on every login of his (`quota.blocked_until`), or the login
paused — continues as a SUCCESSOR run on another provider (`galaius agents spawn --continues`: the ranked walk across providers,
`relations.succeed`), carrying the message that was waiting and where the conversation can be read. The rungs before it
are the model (`run.resolve_continuable_model`) and his other login of the same provider (`registry.swap_login`); this one
is taken only when neither exists, and never moves a session across providers (a session is its CLI's own format).
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from galaius.account.caller import RUN_VARIABLES
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
    AND another provider is installed with its own login open. Never for a run the owner holds a move for (`pending_move`), a member's
    lent run, a CLI no login runs, or a run of no process."""
    models = tuple(dict.fromkeys(model for model in models if model))
    if not models or run.pending_move is not None or run.kind != "process" or run.foreign or not run.agent:
        return False
    if run.subscription is not None and run.subscription.lent:
        return False
    logins = {run.subscription_id, *(one.id for one in Subscription.all() if one.provider == run.provider)}
    if not all(_shut(run.provider, login, models) for login in logins):
        return False
    return any(other.name != run.provider and other.can_resume and other.available() and quota.login_pause(other.name) is None
               for other in PROVIDERS.values())


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


class HandOff(BaseModel):
    """One `galaius agents spawn --continues` of stopped run `run`'s role, started on the provider the ranked walk picks, with `task`."""

    model_config = ConfigDict(frozen=True)

    run: str
    role: str
    name: str
    parent: str | None
    cwd: str
    task: str

    def argv(self) -> list[str]:
        parent = ("--parent-run-id", self.parent) if self.parent else ()
        return reg.participant_argv("galaius", "agents", "spawn", "--agent", self.role, "--name", self.name, *parent, "--cwd", self.cwd,
                                    "--continues", self.run, f"--task={self.task}")   # one argument: a task beginning with a dash stays a task

    def make(self) -> str | None:
        """The run that took it over (the spawn's last line), None when the spawn refused (logged). Never run under the predecessor's
        record lock: the spawn takes it itself (`relations.succeed`)."""
        environment = {**os.environ, **dict.fromkeys(RUN_VARIABLES, self.parent or self.run)}
        done = subprocess.run(self.argv(), cwd=self.cwd if os.path.isdir(self.cwd) else ".", capture_output=True, text=True, timeout=SPAWN_SECONDS,
                              env=reg.participant_environment(environment), check=False)
        said = (done.stdout or "").strip().splitlines()
        if done.returncode != 0 or not said or reg.get_run(said[-1]) is None:
            log.warning("could not hand run %s on to another provider: %s", self.run[:8], (done.stderr or done.stdout).strip()[-400:])
            return None
        return said[-1]

    @classmethod
    def of(cls, run: reg.AgentRun, message: str) -> "HandOff":
        """What carries run `run`'s conversation to its successor: the message waiting for it, and where its record is read."""
        task = (f"{message}\n\n[Galaius: you continue run {run.run_id[:8]} ({run.name}), which ran on {run.provider} and could not take this "
                f"message: every model of its provider is at its usage limit on every login. Its conversation: `galaius agents events {run.run_id}`.]")
        return cls(run=run.run_id, role=run.agent or "", name=run.name, parent=run.parent_run_id, cwd=run.cwd or ".", task=task)
