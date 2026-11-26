"""GUARD, driven: each cell decision point × cause run through the REAL lifecycle on a stand-in vendor (`standin_vendor.py`):
a real subprocess per turn, `run_agent` walking the table at the start, the stream read as the vendor's own, `registry.finish` recording
the cut, `registry.schedule_resumes` / `claim_resume` timing the automatic resume, `agent_queue.dispatch` running the resumed turn.
Not the owner's galaius process and not a real vendor: the PC-level service loop that calls those functions is not run (its own tests).

RED on e31c445d (each cell names what the stand-in saw; the lines behind them are in the report, not skipped):
- start / overloaded, start / refusal0, mid-work / refusal0, resume / refusal0: the run stays on model-a, a refusal said as a finished answer
  (exit 0) is no refusal to the walk, a 529 at the first turn is not stepped past;
- mid-work / overloaded: the automatic resume waits 15 s (`registry.overload_resume_at`) with model-b answering;
- resume / overloaded: his « Continuer » asks model-a seven times and never model-b (`_remember_vendor_refusal` benches only when
  `run.resumed_cut == "overloaded"`, which his own message never sets);
- resume / week: the dispatcher asks every model of the shut login nine turns in a row (a, b, c, c, …) before anything stops.

Table: model-a > model-b > model-c on ONE login of ONE provider (nothing else installed). A cell asks: the turn that refused is
followed AT ONCE by a turn on the next model of the table; a cause that shuts the whole login (`week`, `login_lost`) leaves nothing
that can run, so only there may it wait, and then it says so (its record names the cause)."""

import json
import sys
import time
from pathlib import Path

import pytest

from galaius.agents import agent_queue, messaging, registry as reg, run as run_module

from galaius.agents.limits import walk
from galaius.agents.limits import admission, fallback, quota
from galaius.agents.run import run_agent
from tests.support.agents import QueueProvider, setup_queue, use_policy

STANDIN = Path(__file__).with_name("standin_vendor.py")
TABLE = ("model-a", "model-b", "model-c")
WHOLE_LOGIN = {"week", "login_lost"}   # every model of the one login is out with the cause


class _Standin(QueueProvider):
    name = "claude"
    auth_home_env = ()

    def command(self, task, *, model=None, **_ignored):
        return [sys.executable, str(STANDIN), "--model", model or ""]

    def resume_command(self, session_id, message, **kwargs):
        self.messages.append(str(message))
        return [sys.executable, str(STANDIN), "--model", kwargs.get("model") or ""]

    def parse(self, line, ledger=None):
        from galaius.agents.providers import ClaudeCodeProvider
        return ClaudeCodeProvider().parse(line, ledger)


@pytest.fixture
def standin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    provider = setup_queue(monkeypatch, _Standin())
    use_policy(monkeypatch, agents={"tester": "queue-model"}, reasoning={"tester": "medium"}, providers={"claude": True})
    monkeypatch.setattr(walk, "PROVIDERS", {"claude": provider})
    monkeypatch.setattr(run_module, "rank_candidates", lambda rule, env, *, providers, weights="", role=None, among=None: tuple(
        reg.LaunchCandidate(provider="claude", model=name, rank=index) for index, name in enumerate(TABLE)))

    monkeypatch.setattr(run_module, "_context_of", lambda model, candidates: None)
    monkeypatch.setattr(run_module, "resolve_model", lambda model, env, *, provider, weights="": ({}, model))
    monkeypatch.setenv("STANDIN_STATE", str(tmp_path / "turns"))
    quota.forget()
    reg.cut_resume_since(now=0.0)

    def plan(**turns: dict[str, str]) -> None:
        (tmp_path / "plan.json").write_text(json.dumps({name.replace("_", "-") if name != "all" else "*": value for name, value in turns.items()}), encoding="utf-8")
        monkeypatch.setenv("STANDIN_PLAN", str(tmp_path / "plan.json"))

    def asked() -> list[str]:
        log = tmp_path / "turns.log"
        return [line.split()[1] for line in log.read_text().splitlines()] if log.exists() else []

    return plan, asked, tmp_path


def _out(cause: str, turn: int) -> dict:
    """The cause striking from `turn` on: the top model alone or, for a login-wide cause, every model, every turn after (it stays out)."""
    if cause in WHOLE_LOGIN:
        return {"all": {str(n): cause for n in range(turn, turn + 8)}}
    return {"model_a": {str(n): cause for n in range(turn, turn + 8)}}


async def _launch(tmp_path: Path) -> reg.AgentRun:
    handle = await run_agent(None, "go", agent="tester", cwd=str(tmp_path), quota_window=0.5)
    await handle.wait()
    return reg.get_run(handle.run_id)


def _continue(run_id: str, sender: str) -> reg.AgentRun:
    messaging.deliver_message(run_id, "go on", sender=sender)
    agent_queue.dispatch(run_id)
    return reg.get_run(run_id)


def _says(run: reg.AgentRun) -> str:
    return (run.stream_digest.ending_text if run.stream_digest is not None else "") or ""


CAUSES = ["pool", "week", "overloaded", "login_lost", "refusal0"]


#: A limit worded as the turn's finished answer (exit 0, no error flagged) is read nowhere: no recorded run of the real CLI ends a refused turn
#: that way, and a reader of the answer's words would let an agent's own text (a page it quotes) shut a model on a login. The cell stays red,
#: strict, until a recorded run shows it.
UNSHOWN = "a limit worded as a finished answer is not shown real: no answer's words are read as a vendor refusal"
CELLS = [pytest.param(cause, marks=pytest.mark.xfail(strict=True, reason=UNSHOWN)) if cause == "refusal0" else cause for cause in CAUSES]


@pytest.mark.parametrize("cause", CELLS)
async def test_start_a_refused_first_turn_ends_on_the_next_model_of_the_table(standin, cause: str) -> None:
    plan, asked, tmp_path = standin
    plan(**_out(cause, 1))
    if cause == "week":   # the vendor's window answered at the start: the launch ends SAYING so (`admission.Paused`, its date), never silent
        with pytest.raises(admission.Paused):
            await _launch(tmp_path)
        return
    run = await _launch(tmp_path)
    if cause in WHOLE_LOGIN:
        assert run.status == "failed" and _says(run), f"nothing runs on this one login: it must say why, asked {asked()}, ended {run.status}"
        return
    assert run.model == "model-b" and run.status == "done", f"asked {asked()}, ended {run.status} on {run.model}"


@pytest.mark.parametrize("cause", CELLS)
async def test_midwork_a_refused_turn_comes_back_at_once_on_the_next_model(standin, cause: str) -> None:
    """The turn after work is refused; the run is then what the system leaves it (the dispatcher's own retry, then the cut scheduler's
    automatic resume): done on the next model, or due NOW; never a wait while model-b answers."""
    plan, asked, tmp_path = standin
    plan(**_out(cause, 2))
    run = await _launch(tmp_path)
    assert run.status == "done" and run.model == "model-a"
    state = _continue(run.run_id, "operator")
    if cause in WHOLE_LOGIN:
        assert state.status == "failed" and _says(state), f"nothing can run on this login; asked {asked()}, ended {state.status}"
        return
    if state.status != "done":
        reg.schedule_resumes()
        cut = reg.get_run(run.run_id)
        waited = None if cut.resume_at is None else cut.resume_at - cut.finished_at
        assert waited is not None and waited < 1, f"cut by {cause}, asked {asked()}: its own resume waits {waited} s ({cut.resumes}) while model-b answers"
        reg.claim_resume(run.run_id, now=cut.resume_at + 1)
        state = _continue(run.run_id, reg.CUT_RESUME_SENDER)
    assert state.status == "done" and state.model == "model-b", f"asked {asked()}, ended {state.status} on {state.model}"


@pytest.mark.parametrize("cause", CELLS)
async def test_resume_his_message_after_a_refused_turn_runs_on_the_next_model(standin, cause: str) -> None:
    plan, asked, tmp_path = standin
    plan(**_out(cause, 2))
    run = await _launch(tmp_path)
    _continue(run.run_id, "operator")           # the turn that is refused
    after = _continue(run.run_id, "operator")   # his « Continuer »
    if cause in WHOLE_LOGIN:
        assert after.status == "failed" and _says(after), f"asked {asked()}"
        return
    assert after.model == "model-b" and after.status == "done", f"asked {asked()}, ended {after.status} on {after.model}"
