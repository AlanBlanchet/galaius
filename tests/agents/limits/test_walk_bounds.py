"""GUARD cells the first living file lacked (tester aa8f5f6b, 2026-11-25): a shut login is asked a BOUNDED number of times, and a refusal
never repeats the same (model, login) turn. Same stand-in, same fixture as `test_walk_living`."""

import pytest

from tests.agents.limits.test_walk_living import (  # noqa: F401 — the fixture is reused by name
    TABLE, _continue, _launch, _out, standin,
)


@pytest.mark.parametrize("cause", ["week", "login_lost"])
async def test_a_login_shut_whole_is_asked_once_per_model_at_most_never_a_storm(standin, cause: str) -> None:
    """Every model of the one login is out: the turn that follows his message asks each model of the table at most once (the table's
    length), then stops and says so. Red on e31c445d: nine turns (a, b, c, c, c, c, c, c, a)."""
    plan, asked, tmp_path = standin
    plan(**_out(cause, 2))
    run = await _launch(tmp_path)
    _continue(run.run_id, "operator")
    _continue(run.run_id, "operator")
    assert len(asked()) - 1 <= 2 * len(TABLE), f"asked {asked()}"


async def test_one_models_refusal_is_never_asked_twice_in_a_row_while_another_model_answers(standin) -> None:
    plan, asked, tmp_path = standin
    plan(**_out("overloaded", 2))
    run = await _launch(tmp_path)
    _continue(run.run_id, "operator")
    _continue(run.run_id, "operator")
    after_work = asked()[1:]
    assert after_work.count("model-a") <= 1, f"the refused model was asked again and again: {asked()}"


# ───── cells added 2026-11-25 (second pass): two logins, a provider going out mid-work, a lost network — on the stand-in subprocess ─────

import asyncio
import sys
from pathlib import Path

from galaius.agents import agent_queue, messaging, registry as reg, run as run_module

from galaius.agents.limits import walk
from galaius.agents.limits import quota
from galaius.agents.limits.subscriptions import RunSubscription, Subscription
from galaius.agents.run import run_agent
from tests.agents.limits.test_walk_living import STANDIN, _Standin
from tests.agents.limits.test_resume_swap import _Claude, _stopped_run
from tests.machine.test_machine_agents import _agents, base  # noqa: F401 — the fixture
from tests.support.agents import install_provider, use_policy


class _TwoLogins(_Claude):
    """A resumable CLI keeping sessions per login folder (`CLAUDE_CONFIG_DIR`) that runs the stand-in vendor program."""

    def resume_command(self, session_id, message, **kwargs):
        self.models.append(kwargs.get("model"))
        self.messages.append(str(message))
        return [sys.executable, str(STANDIN), "--model", kwargs.get("model") or ""]


async def test_two_logins_the_first_shut_whole_the_resume_runs_on_the_second_on_the_stand_in(standin, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """His login a answers every turn with its week window; login b is open. His message resumes the run (pinned to a): the vendor refuses
    there, the turn goes to b at once (the session carried), a bounded number of turns, done."""
    plan, asked, _ = standin
    provider, logins, _ = _stopped_run(monkeypatch, tmp_path)
    monkeypatch.setattr(walk, "PROVIDERS", {"claude": provider})
    monkeypatch.setattr(provider, "resume_command", _TwoLogins.resume_command.__get__(provider))
    monkeypatch.setattr(run_module, "resolve_model", lambda model, env, *, provider, weights="": ({}, model))
    plan(**{f"*@{logins['a'].folder.name}": {"*": "week"}})
    monkeypatch.setenv("STANDIN_STATE", str(tmp_path / "turns2"))
    messaging.deliver_message("r1", "go on", sender="operator")
    await asyncio.to_thread(agent_queue.dispatch, "r1")
    run = reg.get_run("r1")
    turns = (tmp_path / "turns2.log").read_text().splitlines()
    assert run.status == "done" and run.subscription_id == logins["b"].id, f"ended {run.status} on login {run.subscription_id} (b is {logins['b'].id}); turns {turns}"
    assert len(turns) <= 3, f"the shut login was asked again and again: {turns}"


async def test_provider_out_mid_work_on_the_stand_in_the_pc_resumes_the_run_on_the_next_program_with_no_hand(standin, base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The run's program refuses mid-work with the login's whole window; another program is in the table: the PC's own pass (no person)
    continues the conversation there at once, a successor run, a real subprocess."""
    plan, asked, tmp_path = standin
    other = type("_OtherStandin", (_Standin,), {"name": "other"})()
    install_provider(monkeypatch, other)
    monkeypatch.setattr(walk, "PROVIDERS", {"claude": walk.PROVIDERS["claude"], "other": other})
    use_policy(monkeypatch, messaging, run_module, agents={"tester": "queue-model"}, reasoning={"tester": "medium"}, providers={"claude": True, "other": True})
    rows = (("claude", "model-a"), ("claude", "model-b"), ("other", "model-c"))
    monkeypatch.setattr(run_module, "rank_candidates", lambda rule, env, *, providers, weights="", role=None, among=None: tuple(
        reg.LaunchCandidate(provider=provider, model=model, rank=index) for index, (provider, model) in enumerate(rows) if provider in {one.name for one in providers}))
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)
    reg.cut_resume_since(now=0.0)
    plan(all={"1": "week"})
    monkeypatch.setenv("STANDIN_WORK", "1")
    monkeypatch.setenv("STANDIN_DELAY", "1.2")
    handle = await run_agent(walk.PROVIDERS["claude"], "go", agent="tester", cwd=str(tmp_path), quota_window=0.5)
    await handle.wait()
    monkeypatch.delenv("STANDIN_DELAY")
    cut = reg.get_run(handle.run_id)
    assert cut.status == "failed" and cut.model == "model-a"
    resumed = await _agents(base, tmp_path).resume_paused()
    assert resumed == [handle.run_id], f"the PC did not resume the cut run (waits for the week's reset while another program is open): {resumed}"
    import threading
    worker = threading.Thread(target=agent_queue.dispatch, args=(handle.run_id,), daemon=True)   # bounded: a dispatcher that waits instead of handing on never returns
    worker.start()
    await asyncio.to_thread(worker.join, 40)
    assert not worker.is_alive(), f"the dispatcher is still waiting 40 s after the PC's resume instead of handing the conversation on; asked {asked()}"
    successor = reg.get_run(reg.get_run(handle.run_id).successor or "")
    assert successor is not None and successor.provider == "other", f"asked {asked()}"


async def test_a_lost_network_mid_work_is_no_model_shut_and_the_run_comes_back_by_itself_on_the_same_model(standin, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI never reached the API (no vendor answered): nothing is shut, the table does not move; the run resumes by itself after the
    short network backoff, on the model it was on."""
    plan, asked, tmp_path = standin
    reg.cut_resume_since(now=0.0)
    plan(model_a={"1": "network"})
    monkeypatch.setenv("STANDIN_WORK", "1")
    monkeypatch.setenv("STANDIN_DELAY", "1.2")
    handle = await run_agent(None, "go", agent="tester", cwd=str(tmp_path), quota_window=0.5)
    await handle.wait()
    cut = reg.get_run(handle.run_id)
    assert reg.cut_cause(cut) == "network_lost", f"ended {cut.status}: {cut.stream_digest}"
    assert quota.blocked_until("claude", "model-a") is None, "a lost network shut a model"
    owed = reg.cut_resume(cut)
    assert owed is not None and owed.resumes == "itself" and owed.at - cut.finished_at <= 5.0 + 1
