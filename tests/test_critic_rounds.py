"""A critic role takes at most `Config.agent_critic_rounds_max` rounds on one maker's work, counted across fresh
instances and hand-offs; an agent asking for one more is refused in one line, the person never is
(`galaius.agents.critic_rounds`). A week to 2026-10-20: 274 visual-critic runs in 71 subjects reviewed again and again."""

import asyncio
import os
import uuid

import pytest

from galaius.agents import agent_queue, messaging, relations
from galaius.agents.critic_rounds import CriticRounds
from galaius.agents import registry as reg
from galaius.agents.spawn_line import Spawn
from tests.support import register_run

NAMES = ("lead", "eng", "eng2", "rev1", "rev2", "p1", "p2", "p3")
IDS = {name: str(uuid.UUID(int=n << 96)) for n, name in enumerate(NAMES, start=1)}
ROLES = {"lead": "main", "eng": "app-engineer", "eng2": "app-engineer", "rev1": "visual-critic", "rev2": "visual-critic",
         "p1": "visual-critic", "p2": "visual-critic", "p3": "visual-critic"}


def _run(name: str, *, parent: str | None = None, **rest) -> reg.AgentRun:
    return register_run(IDS[name], agent=ROLES[name], name=ROLES[name], task=f"{name} task", parent_run_id=IDS[parent] if parent else None,
                        pid=os.getpid(), **rest)


def _spawned(name: str, *, parent: str, for_run: str | None) -> None:
    """Run `name` started as `Spawn.start` starts it, `--for` run `for_run`."""
    async def runner(provider, task, *, run_id, **rest):
        register_run(run_id, agent=ROLES[name], name=ROLES[name], task=task, parent_run_id=IDS[parent], pid=os.getpid(),
                     asked_by=rest["asked_by"], provider_session_id=run_id)

    asyncio.run(Spawn(task=f"{name}: review it", cwd=".", agent=ROLES[name], parent_run_id=IDS[parent],
                      for_run=IDS[for_run] if for_run else None).start(IDS[name], runner))


def _next_turn(name: str) -> None:
    """Run `name` takes its next turn: a message reached it and its turn began (its dispatcher claimed it)."""
    agent_queue.enqueue(IDS[name], message_id=f"turn-{len(agent_queue.items(IDS[name]))}", sender=reg.PERSON_SENDER)
    with reg.record_lock(IDS[name]):
        agent_queue.claim_next_locked(IDS[name])


def _ask(role: str, *, parent: str | None, for_run: str) -> str | None:
    return Spawn(task="review it again", cwd=".", agent=role, parent_run_id=IDS[parent] if parent else None, for_run=IDS[for_run]).naming_refusal()


@pytest.fixture
def three_rounds(monkeypatch) -> None:
    """eng's work reviewed three times: a critic spawned for it, a task eng sent that critic, then — eng having handed its
    work to eng2 — a fresh critic for eng2."""
    monkeypatch.setattr(messaging, "policy_for_continuation", lambda run, provider, environment: (None, None, run.model, None))
    monkeypatch.setattr(messaging, "_dispatch", lambda *arguments, **keywords: None, raising=False)
    _run("lead")
    _run("eng", parent="lead")
    _spawned("rev1", parent="lead", for_run="eng")
    assert messaging.queue_message(IDS["rev1"], "fixed rows 2 and 3: check again", sender=IDS["eng"]).state != "error"
    _run("eng2", parent="lead")
    assert relations.succeed(IDS["eng"], IDS["eng2"])
    _next_turn("lead")
    _spawned("rev2", parent="lead", for_run="eng2")
    _next_turn("lead")   # asked again in a later turn: a new round, never part of the last one's panel


def test_an_agent_asking_a_fourth_critic_round_on_one_makers_work_is_refused_in_one_line(three_rounds):
    refused = _ask("visual-critic", parent="lead", for_run="eng")
    assert refused == (f"visual-critic already took 3 rounds on {IDS['eng'][:8]}'s work (runs {IDS['rev1'][:8]}, {IDS['rev2'][:8]}; cap 3): "
                       "no further visual-critic round is started for it by an agent. Fix what its last verdict lists, or ask the "
                       "person for another round.")
    assert _ask("visual-critic", parent="lead", for_run="eng2") is not None   # the hand-off kept the count
    sent = messaging.queue_message(IDS["rev2"], "and once more", sender=IDS["eng2"])
    assert sent.state == "error" and "visual-critic already took 3 rounds" in sent.text


@pytest.mark.parametrize("role, parent", [
    pytest.param("visual-critic", None, id="the-person-asking"),
    pytest.param("ux-critic", "lead", id="another-critic-role"),
    pytest.param("backend-engineer", "lead", id="no-critic-role"),
])
def test_the_person_and_other_roles_are_never_refused_for_it(three_rounds, role, parent):
    assert _ask(role, parent=parent, for_run="eng") is None


def test_an_agent_clearing_its_run_id_is_still_the_agent_asking(three_rounds, monkeypatch):
    """`GALAIUS_RUN_ID= galaius agents send …` from an agent's process: its process says which run asks."""
    monkeypatch.setattr(messaging, "asking_run", lambda: IDS["lead"])
    sent = messaging.queue_message(IDS["rev1"], "one more look", sender=reg.PERSON_SENDER, for_run=IDS["eng2"])
    assert sent.state == "error" and "visual-critic already took 3 rounds" in sent.text


def test_a_lead_dispatching_critics_with_no_maker_named_spends_no_ones_rounds(monkeypatch):
    """A coordinator sending visual-critic to three slices without `--for` is no maker: nothing counts, nothing refuses."""
    _run("lead")
    _run("eng", parent="lead")
    for index, name in enumerate(("p1", "p2", "p3")):
        _next_turn("lead") if index else None
        _spawned(name, parent="lead", for_run=None)
    assert _ask("visual-critic", parent="lead", for_run="eng") is None and CriticRounds.here().spent("visual-critic", IDS["lead"]) == []


def test_a_blind_panel_asked_in_one_turn_is_one_round():
    _run("lead")
    _run("eng", parent="lead")
    for name in ("p1", "p2", "p3"):   # three blind critics, one dispatch
        _spawned(name, parent="lead", for_run="eng")
    assert len(CriticRounds.here().spent("visual-critic", IDS["eng"])) == 1
