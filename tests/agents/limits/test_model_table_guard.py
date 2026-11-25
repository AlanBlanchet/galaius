"""GUARD (owner 2026-11-20 « it should always be working … The harness should never dissalow if i have any form of usage »,
2026-11-25 « every agent should have a dynamic table … if top models aren't available, we select the ones under it until we
find one »): ONE invariant over the class « which model runs now ».

For each (what is out, decision point) the launch, the resume and the mid-work refusal end on the first model of the role's
table that can run, on the first login that can run it, AT ONCE; nothing waits while a lower model or another login can run;
the record says which model was passed over and why. Only with no model and no login anywhere may a turn wait, and then it says so.

Decision points are driven where the code can be driven in-process:
- start: `run.run_agent` on scripted CLIs (this file);
- resume: `messaging.deliver_message` + `agent_queue.dispatch` on a stopped run (`test_resume_swap._stopped_run`);
- mid-work: the run's own cut as its program said it, read by `registry.cut_resume` (when it comes back, never whether the vendor's
  CLI is really refused: that is the living-run stand-in's, `tests/agents/limits/model_table_standin.py`).

Rows marked RED-<n> fail on e31c445d: each names the line behind it (the cause is the point, never a skip).
"""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from galaius.agents import registry as reg, run as run_module, walk
from galaius.agents.limits import fallback, quota
from galaius.agents.run import run_agent
from galaius.agents.transport.events import RunCut
from galaius.usage_dialects import CountedTokens
from tests.agents.limits.test_resume_swap import FABLE_POOL, OWN, _hold, _resume, _stopped_run, _week_line
from tests.support.agents import StreamProvider, install_provider, use_policy

TABLE = ("model-a", "model-b", "model-c")   # the role's ranked table, first is the top model


# ───────────────────────── start: the launch walks the table ─────────────────────────

class _Other(StreamProvider):
    name = "other"


class _SignedOut(StreamProvider):
    """A CLI whose login is lost: its own status check says so, as `claude auth status` does."""

    name = "out"

    async def authenticated(self, env, *, timeout):
        return False


@pytest.fixture
def launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    use_policy(monkeypatch, agents={"tester": "fixture-model"}, reasoning={"tester": "medium"})
    install_provider(monkeypatch, StreamProvider())
    install_provider(monkeypatch, _Other())
    install_provider(monkeypatch, _SignedOut())
    quota.forget()

    async def start(table: list[tuple[str, str]]) -> reg.AgentRun:
        monkeypatch.setattr(run_module, "rank_candidates", lambda rule, env, *, providers, weights="", role=None, among=None: tuple(
            reg.LaunchCandidate(provider=provider, model=model, rank=index) for index, (provider, model) in enumerate(table)))
        handle = await run_agent(None, "go", agent="tester", cwd=str(tmp_path), quota_window=0.5)
        await handle.wait()
        return reg.get_run(handle.run_id)

    return start


def _passed_over(record: reg.AgentRun) -> dict[str, str]:
    return {entry.candidate.model: entry.reason for entry in record.skipped}


@pytest.mark.parametrize(("shut", "runs"), [
    pytest.param((), "model-a", id="nothing-out"),
    pytest.param(("model-a",), "model-b", id="top-out"),
    pytest.param(("model-a", "model-b"), "model-c", id="two-out"),
])
async def test_start_runs_the_first_model_of_the_table_that_can_run(launch, shut: tuple[str, ...], runs: str) -> None:
    for model in shut:
        quota.record_refusal("fake", model, said=FABLE_POOL)
    record = await launch([("fake", model) for model in TABLE])
    assert record.model == runs
    # the record says which was passed over and why: a model above the one that ran is never silently gone
    assert [m for m in TABLE[:TABLE.index(runs)]] == [m for m in _passed_over(record)] or not shut


async def test_start_with_the_whole_provider_out_takes_the_next_provider_of_the_table(launch) -> None:
    record = await launch([("out", "model-a"), ("out", "model-b"), ("other", "model-c")])
    assert (record.provider, record.model) == ("other", "model-c")
    assert {entry.candidate.model: entry.reason for entry in record.skipped} == {"model-a": "unauthenticated", "model-b": "unauthenticated"}


async def test_start_with_every_model_noted_still_asks_the_vendor_never_refuses(launch) -> None:
    """No model at all can run (every note standing): the walk asks the vendor, which decides — never a refusal of his request."""
    for model in TABLE:
        quota.record_refusal("fake", model, said=FABLE_POOL)
    record = await launch([("fake", model) for model in TABLE])
    assert record.model in TABLE


# ───────────────────────── resume: the next turn of a stopped run ─────────────────────────

def test_resume_with_its_login_lost_and_his_other_login_open_goes_there(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider, logins, _ = _stopped_run(monkeypatch, tmp_path)
    quota.pause_login("claude", "login_lost", subscription=logins["a"].id, said="Not logged in · Please run /login")
    model, login = _resume()
    assert (login, model) == (logins["b"].id, "fable-fixture")


def test_resume_when_the_table_gains_a_model_while_the_run_is_down_takes_it_if_it_is_the_first_that_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A model published while the run is stopped (a-3115c190 « must appear by itself »): the top model is out, the table now lists a new
    model between it and the old second one: the resume takes the new one, not the old second."""
    provider, logins, _ = _stopped_run(monkeypatch, tmp_path)
    scores = {"fable-fixture": 90.0, "new-model": 80.0, "lower-model": 50.0}
    monkeypatch.setattr(run_module, "rank_candidates", lambda rule, env, *, providers, weights="", role=None, among=None: tuple(
        reg.LaunchCandidate(provider="claude", model=name, rank=index) for index, name in enumerate(scores)))

    for login in (*logins.values(), OWN):
        _hold("pool", login)
    model, _ = _resume()
    assert model == "new-model"


# ───────────────────────── mid-work: a cut turn comes back at once on what can run ─────────────────────────

def _cut(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cut: RunCut, said: str, *, resumed_cut: str | None = None) -> tuple[reg.AgentRun, dict]:
    provider, logins, _ = _stopped_run(monkeypatch, tmp_path)
    monkeypatch.setattr(walk, "PROVIDERS", {"claude": provider})   # the one provider installed: only logins and the lower model can take it
    reg.cut_resume_since(now=0.0)
    run = reg.get_run("r1")
    digest = reg.StreamDigest(source="x", ending="error", ending_text=said, cut=cut, usage=CountedTokens())
    return run.model_copy(update={"status": "failed", "finished_at": time.time(), "stream_digest": digest, "resumed_cut": resumed_cut}), logins


def _waits(run: reg.AgentRun) -> float | None:
    """Seconds its automatic resume waits after its end; None: it is left to a person (never comes back by itself)."""
    owed = reg.cut_resume(run)
    return None if owed is None or owed.at is None else owed.at - run.finished_at


def test_RED_1_a_login_lost_mid_work_resumes_on_his_other_login_at_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """RED-1 registry.cut_resume: `if cause == "login_lost": return ... CutOwed(cause, "sign_in", None)` — a lost login waits for a sign-in
    though his other login (and a lower model) answers. Seen 20-23 Nov: runs 7a8fbf3a and 9398e3ac down 2 d 13 h."""
    run, logins = _cut(monkeypatch, tmp_path, RunCut.login_lost, "Not logged in · Please run /login")
    quota.pause_login("claude", "login_lost", subscription=logins["a"].id, said="Not logged in")
    owed = reg.cut_resume(run)
    assert owed is not None and owed.resumes == "itself" and owed.at is not None and owed.at - run.finished_at < 1


def test_RED_2_an_api_overloaded_once_resumes_on_the_next_model_without_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """RED-2 registry.overload_resume_at `OVERLOAD_RESUME_BACKOFF[0]` (15 s) and `_remember_vendor_refusal` benches a model only at the
    SECOND refusal in a row: the first overload resumes the same model after a wait while a lower one answers. Seen 25 Nov 12:09: run
    f3578895 (Fable) waited about 2 min and came back on Fable while Opus 5.5 was free."""
    run, _ = _cut(monkeypatch, tmp_path, RunCut.overloaded, "API Error: 529 Overloaded.")
    reg._remember_vendor_refusal(run, "API Error: 529 Overloaded.", cut=RunCut.overloaded)
    waited = _waits(run)
    assert waited is not None and waited < 1
    model, _ = _resume()
    assert model != "fable-fixture", "the overloaded model was asked again instead of the next one of the table"


@pytest.mark.parametrize("other_login_open", [pytest.param(True, id="his-other-login-open"), pytest.param(False, id="only-the-lower-model-of-the-same-login")])
def test_RED_3_an_api_overloaded_twice_resumes_without_waiting_where_something_can_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, other_login_open: bool) -> None:
    """RED-3 registry.overload_resume_at: the automatic resume is `finished_at + OVERLOAD_RESUME_BACKOFF[n]` (15 s, 45 s …) even when his
    other login is open, and `max(due, bench)` (`OVERLOAD_BENCH` 600 s) when only the lower MODEL of the same login can run (no other login,
    no other provider): a wait while a model answers. (A person's own message takes the lower model at once: `test_resume_swap`.)"""
    run, logins = _cut(monkeypatch, tmp_path, RunCut.overloaded, "API Error: 529 Overloaded.", resumed_cut="overloaded")
    for name in ("a", *(() if other_login_open else ("b", "own"))):
        quota.record_refusal("claude", "fable-fixture", subscription=({**logins, "own": OWN}[name]).id, bench=600.0)
    waited = _waits(run)
    assert waited is not None and waited < 1, f"waits {waited} s while a login or a lower model answers"


@pytest.mark.parametrize(("other_login_open", "waits_for_reset"), [(True, False), (False, True)])
def test_a_limit_of_one_logins_window_mid_work_resumes_at_once_only_where_something_can_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                                                         other_login_open: bool, waits_for_reset: bool) -> None:
    line = _week_line()
    run, logins = _cut(monkeypatch, tmp_path, RunCut.usage_limit, line)
    for name in ("a", *(() if other_login_open else ("b", "own"))):
        quota.record_refusal("claude", "fable-fixture", said=line, subscription=({**logins, "own": OWN}[name]).id)
    waited = _waits(run)
    assert waited is not None
    assert (waited > 1) is waits_for_reset


def test_a_model_pool_limit_mid_work_resumes_at_once_on_the_next_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run, logins = _cut(monkeypatch, tmp_path, RunCut.usage_limit, FABLE_POOL)
    for login in (*logins.values(), OWN):
        _hold("pool", login)
    waited = _waits(run)
    assert waited is not None and waited < 1
    assert _resume()[0] == "lower-model"
