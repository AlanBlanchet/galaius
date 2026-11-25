"""The PC's own loop (`MachineAgents.resume_paused`: schedule, claim, resume, the dispatcher's turn) on the stand-in vendor
(`tests/agents/limits/model_table_standin.py`): a run cut in the middle of its work by a refusal that came after the launch's own probe
window comes back with NO hand, on the table's next unit, the turn a real subprocess. The runner's service loop that calls
`resume_paused` on a timer is not run here (its own tests); everything it calls is."""

import asyncio
from pathlib import Path

import pytest

from galaius.agents import agent_queue, registry as reg
from galaius.agents.limits import quota
from galaius.agents.run import run_agent
from tests.agents.limits.test_model_table_living import _out, standin   # noqa: F401 — the fixture
from tests.machine.test_machine_agents import _agents, base   # noqa: F401 — the fixture


async def _cut_midwork(standin_fixture, monkeypatch: pytest.MonkeyPatch, cause: str) -> tuple[str, Path]:
    plan, asked, tmp_path = standin_fixture
    plan(**_out(cause, 1))
    monkeypatch.setenv("STANDIN_WORK", "1")
    monkeypatch.setenv("STANDIN_DELAY", "1.2")   # past the launch's probe window (0.5 s): the run is committed to model-a when it is refused
    handle = await run_agent(None, "go", agent="tester", cwd=str(tmp_path), quota_window=0.5)
    await handle.wait()
    monkeypatch.delenv("STANDIN_DELAY")
    run = reg.get_run(handle.run_id)
    assert run.model == "model-a" and run.status == "failed", f"asked {asked()}, ended {run.status} on {run.model}"
    return run.run_id, tmp_path


@pytest.mark.parametrize("cause", ["overloaded", "pool"])
async def test_a_run_cut_mid_work_by_a_refusal_comes_back_on_the_next_model_with_no_hand(standin, base: Path, monkeypatch: pytest.MonkeyPatch, cause: str) -> None:
    plan, asked, tmp_path = standin
    reg.cut_resume_since(now=0.0)
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)
    run_id, _ = await _cut_midwork(standin, monkeypatch, cause)
    assert reg.cut_cause(reg.get_run(run_id)) in {"overloaded", "usage_limit"}
    agents = _agents(base, tmp_path)
    resumed = await agents.resume_paused()       # the PC's own pass: nothing sent by a person
    assert resumed == [run_id], f"the PC did not resume the cut run: {resumed}"
    await asyncio.to_thread(agent_queue.dispatch, run_id)   # its dispatcher takes the resume
    run = reg.get_run(run_id)
    assert (run.status, run.model) == ("done", "model-b"), f"asked {asked()}, ended {run.status} on {run.model}"
    assert [turn for turn in asked()][:2] == ["model-a", "model-b"], "the shut model is never asked again"
    assert quota.shut("claude", "model-a") is not None


async def test_a_run_cut_mid_work_on_every_model_of_its_login_waits_for_the_reopening_and_says_so(standin, base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing of the table can run on the one login (its whole window shut): no resume is made into it, the run's record names the cut and
    when its resume comes (the named reset), never a loop of turns."""
    plan, asked, tmp_path = standin
    reg.cut_resume_since(now=0.0)
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)
    plan(**_out("week", 1))
    monkeypatch.setenv("STANDIN_WORK", "1")
    monkeypatch.setenv("STANDIN_DELAY", "1.2")
    handle = await run_agent(None, "go", agent="tester", cwd=str(tmp_path), quota_window=0.5)
    await handle.wait()
    monkeypatch.delenv("STANDIN_DELAY")
    run_id = handle.run_id
    owed = reg.schedule_resumes()
    [(_, cut)] = [item for item in owed if item[0].run_id == run_id]
    assert cut.cause == "usage_limit" and cut.at is not None and cut.at > reg.get_run(run_id).finished_at + 3600, "the resume is due at the named reset"
    assert await _agents(base, tmp_path).resume_paused() == [], "nothing is asked of a login whose window is shut"
    assert asked() == ["model-a"], f"the shut login was asked again: {asked()}"
