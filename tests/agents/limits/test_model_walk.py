"""The model walk (`galaius.agents.walk`) and the entries it reads (`quota.shut`): strictly top-down over (model, login), the first unit
nothing shuts, else the one reopening first, asked all the same; a member's lent run walks models alone; a program shut on every
login hands the conversation to the next program of the table."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from galaius_core import SubscriptionLimits

from galaius.agents import registry as reg, run as run_module, walk
from galaius.agents.limits import quota
from galaius.agents.limits.subscriptions import RunSubscription
from galaius.agents.walk import Unit
from tests.agents.limits.test_resume_swap import FABLE_POOL, OWN, _hold, _stopped_run, _week_line
from tests.support.agents import QueueProvider, use_policy

A, B = uuid4(), uuid4()


def _units(*pairs: tuple[str, UUID | None]) -> list[Unit]:
    return [Unit("claude", model, login) for model, login in pairs]



def test_the_walk_takes_the_first_unit_nothing_shuts_and_names_what_it_passed() -> None:
    quota.forget()
    quota.record_refusal("claude", "top", said=FABLE_POOL, subscription=A)
    quota.record_refusal("claude", "top", bench=quota.OVERLOAD_BENCH, subscription=B)
    step = walk.walk(_units(("top", A), ("top", B), ("middle", A), ("low", A)))
    assert step is not None and step.open and step.unit == Unit("claude", "middle", A)
    assert [(one.unit.login, one.shut.reason) for one in step.passed] == [(A, "pool"), (B, "overload")]


def test_every_unit_shut_the_one_reopening_first_is_asked_and_a_lost_login_reopens_last() -> None:
    quota.forget()
    now = time.time()
    quota.record_refusal("claude", "top", said=_week_line(), subscription=A, now=now)       # a window two days away: the login's whole
    quota.pause_login("claude", "login_lost", subscription=B, said="Not logged in", now=now)
    quota.record_refusal("claude", "low", bench=quota.OVERLOAD_BENCH, subscription=None, now=now)   # minutes away, on the PC's own login
    step = walk.walk(_units(("top", A), ("top", B), ("low", None)), now=now)
    assert step is not None and not step.open
    assert step.unit == Unit("claude", "low", None) and step.shut.reason == "overload"


def test_a_lost_login_is_an_entry_with_no_instant_and_ends_only_by_a_sign_in() -> None:
    quota.forget()
    quota.pause_login("claude", "login_lost", subscription=A, said="Not logged in · Please run /login")
    entry = quota.shut("claude", "any-model", subscription=A)
    assert entry is not None and (entry.reason, entry.until, entry.login) == ("login_lost", None, str(A))
    assert quota.shut("claude", "any-model", subscription=B) is None
    quota.login_back("claude", subscription=A)
    assert quota.shut("claude", "any-model", subscription=A) is None


def test_an_overload_is_the_vendors_whatever_login_asked() -> None:
    quota.forget()
    quota.remember("overloaded", "claude", "fable", said="API Error: 529 Overloaded.", subscription=A, login="")
    assert quota.shut("claude", "fable", subscription=A).reason == "overload"
    assert quota.shut("claude", "fable", subscription=B).reason == "overload", "his other login reaches the same overloaded API"
    assert quota.shut("claude", "lower", subscription=B) is None
    assert quota.shut("codex", "fable", subscription=B) is None


def test_an_overload_bench_grows_with_each_refusal_in_a_row_and_stops_growing() -> None:
    quota.forget()
    now = time.time()
    lengths = []
    for step in range(8):
        until = quota.record_refusal("claude", "fable", bench=quota.OVERLOAD_BENCH, now=now + step * 10_000)
        lengths.append(until - (now + step * 10_000))
    assert lengths[0] == quota.OVERLOAD_BENCH and lengths == sorted(lengths) and max(lengths) <= quota.OVERLOAD_BENCH_MAX


def test_the_entries_a_pc_reports_are_what_its_walk_reads() -> None:
    quota.forget()
    quota.record_refusal("claude", "top", said=FABLE_POOL, subscription=A)
    quota.record_refusal("claude", "mid", bench=quota.OVERLOAD_BENCH, subscription=B)
    quota.pause_login("claude", "login_lost", subscription=None, said="Not logged in")
    reported = {(entry.login, entry.reason, entry.family or entry.model) for entry in quota.shut_entries()}
    # a model id with no family token is noted under its own name beside its family's pool (`record_refusal`)
    assert reported == {(str(A), "pool", "fable"), (str(A), "pool", "top"), (str(B), "overload", "mid"), ("", "login_lost", None)}


def test_a_model_that_answers_ends_its_overload_bench_on_every_login_of_the_program() -> None:
    """The overload is the API's: it answered the model on one login, so no login's bench of it still stands."""
    quota.forget()
    now = time.time()
    for login in (A, B):
        quota.record_refusal("claude", "fable", bench=quota.OVERLOAD_BENCH, subscription=login, now=now - 5)
    quota.served("claude", "fable", subscription=B, at=now)
    assert quota.shut("claude", "fable", subscription=A) is None and quota.shut("claude", "fable", subscription=B) is None


def test_the_answers_own_words_never_shut_a_model() -> None:
    """A finished answer quoting a vendor's limit (a page, an issue it read) is the model's text: the end of a turn reads its vendor's error
    alone (`agent_queue.settle`)."""
    from galaius.agents.transport.events import AgentEvent

    quota.forget()
    said = AgentEvent(kind="done", text="You've hit your weekly limit · resets Monday 9am (UTC)")
    assert not (said.kind == "error" and quota.classify(said.text) is not None)


class _Other(QueueProvider):
    name = "other"


def _table(monkeypatch: pytest.MonkeyPatch, *rows: tuple[str, str]) -> None:
    monkeypatch.setattr(run_module, "rank_candidates", lambda rule, env, *, providers, weights="", role=None, among=None: tuple(
        reg.LaunchCandidate(provider=provider, model=model, rank=index) for index, (provider, model) in enumerate(rows)
        if provider in {one.name for one in providers}))


def test_claude_shut_on_every_login_hands_the_conversation_to_the_next_program_of_the_table(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Member 3 (owner 2026-11-20 « any form of usage »): every Claude row is shut on every login of his; another signed-in program is
    in the table, and the step is its row, a successor carrying the conversation."""
    provider, logins, _ = _stopped_run(monkeypatch, tmp_path)
    monkeypatch.setattr(walk, "PROVIDERS", {"claude": provider, "other": _Other()})
    use_policy(monkeypatch, run_module, agents={"tester": "table"}, providers={"claude": True, "other": True})
    _table(monkeypatch, ("claude", "fable-fixture"), ("claude", "lower-model"), ("other", "other-model"))
    for login in (*logins.values(), OWN):
        quota.record_refusal("claude", "fable-fixture", said=_week_line(), subscription=login.id)
    run = reg.get_run("r1")
    step = walk.open_for(run)
    assert step is not None and step.unit == Unit("other", "other-model", None) and step.how(run) == "successor"
    assert {one.shut.reason for one in step.passed} == {"limit"}


def test_a_members_lent_run_walks_the_models_of_its_one_program_and_never_leaves_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Member 6: a run lent to a member runs on none of this PC's logins and moves to none, and to no other program either; its table is
    its own program's rows on the one login it has."""
    provider, logins, _ = _stopped_run(monkeypatch, tmp_path)
    monkeypatch.setattr(walk, "PROVIDERS", {"claude": provider, "other": _Other()})
    use_policy(monkeypatch, run_module, agents={"tester": "table"}, providers={"claude": True, "other": True})
    _table(monkeypatch, ("claude", "fable-fixture"), ("claude", "lower-model"), ("other", "other-model"))
    lent = reg.get_run("r1").model_copy(update={"subscription": RunSubscription(lent=True)})
    reg.save_run(lent)
    assert walk.logins_of(lent, "claude") == [None]
    quota.record_refusal("claude", "fable-fixture", said=FABLE_POOL)
    step = walk.open_for(lent)
    assert step is not None and step.unit == Unit("claude", "lower-model", None), "the next row of its own program, never another program"
    quota.record_refusal("claude", "lower-model", said=_week_line())
    assert walk.open_for(lent) is None, "nothing of its own program can run: it is asked as the vendor decides, never handed on or moved"


def test_a_conversation_of_no_role_walks_its_own_model_over_his_logins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider, logins, _ = _stopped_run(monkeypatch, tmp_path)
    monkeypatch.setattr(walk, "PROVIDERS", {"claude": provider})
    quota.pause_login("claude", "login_lost", subscription=logins["a"].id, said="Not logged in")
    bare = reg.get_run("r1").model_copy(update={"agent": None})
    step = walk.open_for(bare)
    assert step is not None and step.unit.login == logins["b"].id and step.how(bare) == "login"


def test_the_provider_going_out_in_the_middle_of_work_moves_the_next_turn_to_the_next_program(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A table of two programs: the first one is fine at the start of the conversation and refuses mid-work; the cut run's resume is
    due at once on the next program's row, not after the first one's reset."""
    provider, logins, _ = _stopped_run(monkeypatch, tmp_path)
    monkeypatch.setattr(walk, "PROVIDERS", {"claude": provider, "other": _Other()})
    use_policy(monkeypatch, run_module, agents={"tester": "table"}, providers={"claude": True, "other": True})
    _table(monkeypatch, ("claude", "fable-fixture"), ("claude", "lower-model"), ("other", "other-model"))
    reg.cut_resume_since(now=0.0)
    line = _week_line()
    for login in (*logins.values(), OWN):
        quota.record_refusal("claude", "fable-fixture", said=line, subscription=login.id)
    from galaius.agents.transport.events import RunCut
    from galaius.usage_dialects import CountedTokens

    digest = reg.StreamDigest(source="x", ending="error", ending_text=line, cut=RunCut.usage_limit, usage=CountedTokens())
    cut = reg.get_run("r1").model_copy(update={"status": "failed", "finished_at": time.time(), "stream_digest": digest})
    assert reg.limit_resume_at(cut) == cut.finished_at
    assert reg.get_run("r1").provider == "claude" and datetime.fromtimestamp(cut.finished_at, UTC) > datetime.now(UTC) - timedelta(seconds=5)
