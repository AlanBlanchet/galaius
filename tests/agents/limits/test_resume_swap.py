"""A resumed conversation never waits or refuses while a login or a model of his still has usage: it moves to his other
subscription login (the session carried, the same model), else to the next ranked model on its own login; a line that is no
quota (unknown model, a full context) holds nothing (owner 2026-11-20 « The harness should never dissalow if i have any form
of usage »)."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from galaius_core import SubscriptionLimits

from galaius.agents import agent_queue, messaging, registry as reg, run as run_module
from galaius.agents.limits import fallback, quota
from galaius.agents.limits.subscriptions import RunSubscription, Subscription
from galaius.agents.providers import PROVIDERS
from galaius.agents.transport.events import RunCut
from galaius.usage_dialects import CountedTokens
from tests.support.agents import QueueProvider, setup_queue

FABLE_POOL = "You've reached your Fable limit. Switch to another model."
WEEK = "You've hit your weekly limit · resets {at} (UTC)"


class _Claude(QueueProvider):
    """A resumable Claude-named CLI keeping each session in `<login folder>/<session>.jsonl`, the model it was asked for printed."""

    name = "claude"
    auth_home_env = ("CLAUDE_CONFIG_DIR",)

    def __init__(self) -> None:
        super().__init__("import json,os; print(json.dumps({'type':'assistant','session_id':'vendor','message':{'content':[{'type':'text','text':os.environ.get('CLAUDE_CONFIG_DIR','')}]}})); "
                         "print(json.dumps({'type':'result','is_error':False,'session_id':'vendor'}))")
        self.models: list[str | None] = []

    def resume_command(self, session_id, message, **kwargs):
        self.models.append(kwargs.get("model"))
        return super().resume_command(session_id, message, **kwargs)

    def session_files(self, home: Path, cwd: Path, session: str) -> tuple[Path, ...]:
        return (home / f"{session}.jsonl",)


def _week_line() -> str:
    reset = datetime.now(UTC) + timedelta(days=2)
    return WEEK.format(at=f"{reset:%b} {reset.day}, {reset.hour % 12 or 12}{'am' if reset.hour < 12 else 'pm'}")


def _stopped_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model: str = "fable-fixture") -> tuple[_Claude, dict[str, Subscription], str]:
    """Run `r1` stopped on login `a`, its session held there; `b` is his other login; two models ranked, the first the run's own."""
    provider = setup_queue(monkeypatch, _Claude())
    monkeypatch.setattr(run_module, "rank_candidates", lambda rule, env, *, providers, weights="", role=None, among=None: (
        reg.LaunchCandidate(provider="claude", model="lower-model", rank=1), reg.LaunchCandidate(provider="claude", model="fable-fixture", rank=0)))
    monkeypatch.setattr(run_module, "_capability_of", lambda model, candidates: {"fable-fixture": 90.0, "lower-model": 50.0}.get(model))
    monkeypatch.setattr(run_module, "resolve_model", lambda model, env, *, provider, weights="": ({}, model))
    logins = {"a": Subscription.create(uuid4(), "claude"), "b": Subscription.create(uuid4(), "claude")}
    monkeypatch.setitem(PROVIDERS, "claude", provider)
    session = "vendor"
    for name, login in logins.items():
        (login.folder / ".credentials.json").write_text('{"token": "%s"}' % name, encoding="utf-8")
    (logins["a"].folder / f"{session}.jsonl").write_text('{"type":"user","message":"go"}\n', encoding="utf-8")
    run = reg.get_run("r1")
    assert run is not None
    reg.save_run(run.model_copy(update={"status": "done", "finished_at": time.time(), "model": model, "cwd": str(tmp_path), "provider_session_id": session,
                                        "subscription": RunSubscription(subscription_id=logins["a"].id, limits=SubscriptionLimits())}))
    return provider, logins, session


def _hold(kind: str, login: Subscription) -> None:
    if kind == "pool":
        quota.record_refusal("claude", "fable-fixture", said=FABLE_POOL, subscription=login.id)
    elif kind == "stop":
        login.observe_pools((quota.ModelPool(pool="fable", used_percent=97, resets_at=datetime.now(UTC) + timedelta(days=2), observed_at=datetime.now(UTC)),))
    else:   # the login's own window
        quota.record_refusal("claude", "lower-model", said=_week_line(), subscription=login.id)


def _resume() -> tuple[str | None, object]:
    messaging.deliver_message("r1", "go on", sender="operator")
    agent_queue.dispatch("r1")
    return reg.get_run("r1").model, reg.get_run("r1").subscription_id


@pytest.mark.parametrize(("held_by", "shut_on", "runs_on", "model"), [
    ("pool", ("a",), "b", "fable-fixture"),        # (a) Fable refused on his first login, his second open: the same model on the second, the session carried
    ("stop", ("a",), "b", "fable-fixture"),        # (a) the pool read at his stop, no refusal yet
    ("window", ("a",), "b", "fable-fixture"),      # (b) the first login's whole usage window shut: his second takes it, a login-wide hold is no wait
    ("pool", ("a", "b"), "a", "lower-model"),      # (c) the Fable pool shut on both: the next ranked model on its own login, never a wait for the pool's reset
    ("stop", ("a", "b"), "a", "lower-model"),
])
def test_a_resumed_conversation_goes_to_his_other_login_else_to_the_next_ranked_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                                                    held_by: str, shut_on: tuple[str, ...], runs_on: str, model: str) -> None:
    provider, logins, session = _stopped_run(monkeypatch, tmp_path)
    for name in shut_on:
        _hold(held_by, logins[name])
    if held_by == "window":
        assert quota.login_pause("claude", subscription=logins["a"].id) is not None
    resumed_model, login = _resume()
    assert (login, resumed_model) == (logins[runs_on].id, model)
    assert provider.messages == ["go on"]
    if runs_on == "b":
        assert (logins["b"].folder / f"{session}.jsonl").is_file(), "its session was carried to the login that took the turn"
        assert reg.get_run("r1").subscription_id == logins["b"].id


@pytest.mark.parametrize("line", [
    "There's an issue with the selected model (claude-nonexistent-9). It may not exist or you may not have access to it.",
    "Prompt is too long",
    "Prompt is too long · automatic compaction failed: context window exceeded",
])
def test_an_unrecognised_model_or_a_full_context_holds_no_login_and_benches_no_model(line: str) -> None:
    kind = quota.classify(line)
    assert kind in {None, "context_exhausted", "model_capability_unsupported"}
    quota.remember(kind, "claude", "fable-fixture", login="", said=line) if kind is not None else None
    assert quota.blocked_until("claude", "fable-fixture") is None and quota.login_pause("claude") is None
    assert quota.pool_blocked_until("claude", "fable-fixture") is None


def test_every_part_of_a_logins_usage_is_one_record_in_one_file() -> None:
    """The blocks, the pauses and the lift of one login are read from and written to ONE typed record (`LoginUsage`)."""
    login = uuid4()
    quota.record_refusal("claude", "fable-fixture", said=FABLE_POOL, subscription=login)
    quota.record_refusal("claude", "lower-model", said=_week_line(), subscription=login)
    record = quota._read(time.time())[f"claude#{login}"]
    assert set(record.blocks) == {"~fable", "lower-model"}   # one key per (login, pool): the Fable pool is not noted again under a model id
    assert record.blocks["~fable"].window == "seven_day_overage_included"
    assert record.pauses["usage_limit"].until is not None
    assert sorted(path.name for path in quota._path().parent.glob("quota-*.json")) == ["quota-usage.json"]


@pytest.mark.parametrize(("other_open", "at_once"), [(True, True), (False, False)])
def test_a_turn_cut_by_one_logins_window_resumes_at_once_on_his_other_login_else_at_the_reopening(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                                                                 other_open: bool, at_once: bool) -> None:
    """The account's own window (not a model pool) shut the run's login: with another login open its automatic resume is due now, never
    at the reset the refusal named (owner 2026-11-20: never stop the request, use the fallback)."""
    provider, logins, _ = _stopped_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fallback, "PROVIDERS", {"claude": provider})   # no other provider installed here: only the login rung
    line = _week_line()
    for name in ("a", *(() if other_open else ("b",))):
        quota.record_refusal("claude", "fable-fixture", said=line, subscription=logins[name].id)
    run = reg.get_run("r1")
    digest = reg.StreamDigest(source="x", ending="error", ending_text=line, cut=RunCut.usage_limit, usage=CountedTokens())
    failed = run.model_copy(update={"status": "failed", "finished_at": time.time(), "stream_digest": digest})
    due = reg.limit_resume_at(failed)
    assert due is not None and (due == failed.finished_at) is at_once


class _Other(QueueProvider):
    """Another installed CLI, one that can resume: the provider a conversation moves to."""

    name = "other"


@pytest.mark.parametrize(("shut_models", "open_login", "other_provider", "exhausted", "switched_on"), [
    (("fable-fixture", "lower-model"), False, True, True, True),        # every model shut on every login, another provider installed
    (("fable-fixture", "lower-model"), True, True, False, True),        # his other login can take it (`swap_login`'s rung)
    (("fable-fixture",), False, True, False, True),                      # one model still answers on its own login (`resolve_continuable_model`'s rung)
    (("fable-fixture", "lower-model"), False, False, False, True),       # no other provider to go to
    (("fable-fixture", "lower-model"), False, True, False, False),       # another provider installed but switched off in his policy
])
def test_a_conversation_moves_to_another_provider_only_when_nothing_of_its_own_can_answer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shut_models: tuple[str, ...],
                                                                                         open_login: bool, other_provider: bool, exhausted: bool, switched_on: bool) -> None:
    provider, logins, _ = _stopped_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fallback, "PROVIDERS", {"claude": provider, **({"other": _Other()} if other_provider else {})})
    monkeypatch.setattr(fallback, "_active", lambda name: switched_on)
    for name, login in logins.items():
        if open_login and name == "b":
            continue
        for model in shut_models:
            quota.record_refusal("claude", model, said="You've hit your weekly limit", subscription=login.id)
    run = reg.get_run("r1")
    assert fallback.exhausted(run, ("fable-fixture", "lower-model")) is exhausted


@pytest.mark.parametrize("spawn_answers", [True, False])
def test_an_exhausted_providers_waiting_message_goes_to_a_successor_on_another_provider_else_to_its_own_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawn_answers: bool) -> None:
    provider, logins, _ = _stopped_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fallback, "PROVIDERS", {"claude": provider, "other": _Other()})
    monkeypatch.setattr(fallback, "_active", lambda name: True)
    monkeypatch.setattr(agent_queue, "_HANDOFF_REFUSED", set())
    for login in logins.values():
        for model in ("fable-fixture", "lower-model"):
            quota.record_refusal("claude", model, said="You've hit your weekly limit", subscription=login.id)
    monkeypatch.setattr(fallback, "candidates", lambda run: ("fable-fixture", "lower-model"))
    made: list[tuple[str, str]] = []
    monkeypatch.setattr(fallback, "hand_on", lambda run, message: (made.append((run.run_id, message)), "successor1" if spawn_answers else None)[1])
    messaging.deliver_message("r1", "go on", sender="operator")
    agent_queue.dispatch("r1")
    [(stopped, task)] = made
    assert stopped == "r1" and "go on" in task
    item = agent_queue.items("r1")[0]
    assert item.state == "replied"
    assert (provider.messages == []) is spawn_answers, "a refused hand-off asks the run's own provider after all, a made one spends no turn on it"


def test_a_hand_on_starts_a_successor_through_the_ranked_walk_and_takes_over_the_runs_asks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The real `SpawnLine.hand_on` (no stub but the CLI start): its provider and model unpinned so the walk chooses, its task the waiting message
    with where to read the conversation, the stopped run's successor recorded (`relations.succeed`)."""
    import asyncio
    from types import SimpleNamespace

    from galaius.agents.spawn_line import SpawnLine

    _stopped_run(monkeypatch, tmp_path)
    started: dict = {}

    async def runner(provider, task, **kwargs):
        started.update(provider=provider, task=task, **kwargs)
        reg.register(run_id=kwargs["run_id"], pid=999998, provider="other", name="w", task=task, agent="tester")
        return SimpleNamespace(run_id=kwargs["run_id"])

    successor = asyncio.run(SpawnLine.here().hand_on(reg.get_run("r1"), "go on please", runner))
    assert started["provider"] is None and started.get("model") is None and "go on please" in started["task"]
    assert reg.get_run("r1").successor == successor
