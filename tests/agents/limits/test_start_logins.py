"""A start walks every login of his, as a resume does (`limits.walk`): one at its usage limit hands the start to another at once, the
start waits only when all are held (said, each with its reopening), and a refusal naming no candidate stays a record whatever the
table's length. 2026-11-26/28: spawns failed on a 149-long refusal, waited a day on one login while another had usage."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from galaius_core import NoCandidate, SubscriptionLimits, UsageWindow

from galaius.agents import registry as reg
from galaius.agents import run as run_module
from galaius.agents.limits import quota, walk
from galaius.agents.limits.admission import Paused
from galaius.agents.limits.subscriptions import RunSubscription, Subscription
from galaius.agents.run import ModelUnavailable, run_agent
from galaius.agents.spawn_line import SpawnLine
from galaius.agents.transport.events import RunCut
from galaius.usage_dialects import CountedTokens
from tests.agents.limits.test_agent_subscriptions import _Claude, _Codex
from tests.agents.limits.test_resume_swap import _stopped_run, _week_line
from tests.support.agents import install_provider, register_run, use_policy


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """`start(subscription, parent)`: role `tester` ranked on one Claude model, a child saying the login folder it runs with."""
    use_policy(monkeypatch, agents={"tester": "fixture-model"}, reasoning={"tester": "medium"})
    definition = tmp_path / "tester.md"
    definition.write_text("---\nname: tester\n---\nTest.\n", encoding="utf-8")
    monkeypatch.setattr(_Claude, "definition_path", lambda self, agent: definition)
    install_provider(monkeypatch, _Claude())
    monkeypatch.setattr(run_module, "rank_candidates", lambda rule, env, *, providers, weights="", role=None, among=None: (
        reg.LaunchCandidate(provider="claude", model="fixture-model", rank=0),))
    quota.forget()

    async def start(**options) -> tuple[reg.AgentRun, str]:
        handle = await run_agent(None, "go", agent="tester", cwd=str(tmp_path), environment={"PATH": "/usr/bin:/bin"}, **options)
        await asyncio.wait_for(handle.wait(), timeout=30)
        said = "".join(event.text for event in reg.read_events(handle.run_id) if event.kind == "text")
        return reg.get_run(handle.run_id), said

    return start


def _login(name: str) -> Subscription:
    held = Subscription.create(uuid4(), "claude")
    (held.folder / ".credentials.json").write_text(f'{{"token": "{name}"}}', encoding="utf-8")
    return held


def _shut(subscription: Subscription | None) -> None:
    quota.record_refusal("claude", "fixture-model", said=_week_line(), subscription=None if subscription is None else subscription.id)


@pytest.mark.parametrize(("pinned", "held", "runs_on"), [
    pytest.param(None, ("own",), "b", id="own-login-at-its-limit"),
    pytest.param("a", ("a",), "b", id="pinned-login-at-its-limit"),
    pytest.param("a", ("a", "b"), "own", id="only-the-own-login-left"),
    pytest.param(None, (), "own", id="nothing-held-stays-on-own"),
])
async def test_a_start_on_a_login_at_its_limit_runs_on_another_of_his_logins_at_once(claude, pinned: str | None, held: tuple[str, ...], runs_on: str) -> None:
    logins = {"a": _login("a"), "b": _login("b")} if pinned else {"b": _login("b")}
    for name in held:
        _shut(logins.get(name))
    asked = {}
    if pinned:   # a child inheriting its parent's login
        parent = register_run("parent", pid=None, agent="tester", subscription=RunSubscription(subscription_id=logins[pinned].id, limits=SubscriptionLimits()))
        asked = {"parent_run_id": parent.run_id}
    run, folder = await claude(**asked)
    expected = logins.get(runs_on)
    assert run.subscription_id == (None if expected is None else expected.id)
    assert folder == ("" if expected is None else str(expected.folder))


async def test_a_start_waits_only_when_every_login_is_held_and_names_each_with_its_reopening(claude) -> None:
    spare = _login("b")
    _shut(None)
    _shut(spare)
    with pytest.raises(Paused) as paused:
        await claude()
    assert {hold.subscription for hold in paused.value.holds} == {None, spare.id}
    said = str(paused.value)
    assert "ce PC" in said and f"abonnement {str(spare.id)[:8]}" in said and said.count("jusqu'à") == 2


async def test_a_start_leaves_a_login_past_his_switch_threshold_for_one_under_it_and_never_waits_for_it(claude) -> None:
    near, free = _login("a"), _login("b")
    week = datetime.now(UTC) + timedelta(days=2)
    near.observe((UsageWindow(window="weekly", used_percent=96, resets_at=week, observed_at=datetime.now(UTC)),))
    parent = register_run("parent", pid=None, agent="tester", subscription=RunSubscription(subscription_id=near.id, limits=SubscriptionLimits()))
    run, _ = await claude(parent_run_id=parent.run_id)
    assert run.subscription_id == free.id, "the next of his logins under every share"
    for subscription in (None, free):
        _shut(subscription)
    again, _ = await claude(parent_run_id=parent.run_id)
    assert again.subscription_id == near.id, "past a threshold is no limit: the one login left takes the start"


async def test_a_start_on_the_own_login_past_its_weekly_share_goes_to_a_subscription_under_it(claude) -> None:
    free = _login("b")
    quota.usage_heard("claude", (UsageWindow(window="weekly", used_percent=95, resets_at=datetime.now(UTC) + timedelta(days=2), observed_at=datetime.now(UTC)),),
                      asked=time.time())
    run, folder = await claude()
    assert (run.subscription_id, folder) == (free.id, str(free.folder))


async def test_a_login_signed_out_hands_the_same_model_to_the_next_one(claude, monkeypatch: pytest.MonkeyPatch) -> None:
    spare = _login("b")

    async def authenticated(self, env, *, timeout):
        return bool(env.get("CLAUDE_CONFIG_DIR"))   # the own login answers « signed out », the subscription signed in

    monkeypatch.setattr(_Claude, "authenticated", authenticated)
    run, _ = await claude()
    assert run.subscription_id == spare.id and [skip.reason for skip in run.skipped] == ["unauthenticated"]


async def test_each_login_the_vendor_refuses_is_asked_once_then_the_start_waits_naming_them_all(claude, monkeypatch: pytest.MonkeyPatch) -> None:
    spare = _login("b")
    rejected = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "seven_day", "resetsAt": int(time.time()) + 7200}})
    monkeypatch.setattr(_Claude, "script", f"import sys\nprint({rejected!r}, flush=True); sys.exit(1)\n")
    with pytest.raises(Paused) as paused:
        await claude(quota_window=2.0)
    assert {hold.subscription for hold in paused.value.holds} == {None, spare.id} and len(paused.value.holds) == 2


@pytest.mark.parametrize(("answers", "asked"), [
    pytest.param((None, True), [10, 30], id="answers-on-the-second-try"),
    pytest.param((None, None), [10, 30], id="never-completes"),
])
async def test_a_login_check_that_times_out_on_a_loaded_pc_never_ends_a_start(claude, monkeypatch: pytest.MonkeyPatch, answers: tuple[bool | None, ...], asked: list[int]) -> None:
    """The member: load 51, `claude auth status` timed out, « No candidate » on every model while six runs worked on that login."""
    heard: list[float] = []

    async def authenticated(self, env, *, timeout):
        heard.append(timeout)
        return answers[min(len(heard), len(answers)) - 1]

    monkeypatch.setattr(_Claude, "authenticated", authenticated)
    run, _ = await claude()
    assert run.status == "done" and heard == asked and not run.skipped


async def test_the_login_a_person_asked_for_stays_while_its_vendor_runs_it(claude) -> None:
    picked = _login("a")
    _login("b")
    picked.observe((UsageWindow(window="weekly", used_percent=97, resets_at=datetime.now(UTC) + timedelta(days=2), observed_at=datetime.now(UTC)),))
    run, _ = await claude(subscription=RunSubscription(subscription_id=picked.id, limits=SubscriptionLimits()))
    assert run.subscription_id == picked.id


async def test_a_refusal_past_the_records_places_keeps_every_reason_and_says_it_in_one_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, claude) -> None:
    """The member: 149 candidates passed over, `NoCandidate.skips` holds 64, the spawn died on the record's own validation."""
    install_provider(monkeypatch, _Codex())
    use_policy(monkeypatch, agents={"tester": "fixture-model"}, reasoning={"tester": "medium"}, providers={"codex": False})
    table = tuple(reg.LaunchCandidate(provider="codex", model=f"model-{index}", rank=index) for index in range(148))
    table += (reg.LaunchCandidate(provider="claude", model="fixture-model", rank=148),)
    monkeypatch.setattr(run_module, "rank_candidates", lambda rule, env, *, providers, weights="", role=None, among=None: table)
    with pytest.raises(ModelUnavailable) as refused:
        await claude(passed_over=(("claude", "fixture-model"),))
    refusal = refused.value.refusal
    assert isinstance(refusal, NoCandidate)
    assert len(refusal.skips) == 64 and refusal.reasons == {"provider_off", "quota_exceeded"}
    assert [skip.model for skip in refusal.skips[:2]] == ["model-0", "model-1"], "ranked order kept"
    said = str(refused.value)
    assert "\n" not in said and len(said) <= 500
    assert "No candidate for 'tester'" in said and "148 provider_off" in said and "1 quota_exceeded" in said and "codex/model-0" in said


def test_a_run_on_its_own_login_cut_by_the_weekly_limit_mid_work_comes_back_on_another_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The member: a run on this PC's own login acted, then « You've hit your weekly limit · resets … », and waited for the reset."""
    provider, logins, _ = _stopped_run(monkeypatch, tmp_path)
    monkeypatch.setattr(walk, "PROVIDERS", {"claude": provider})
    reg.cut_resume_since(now=0.0)
    line = _week_line()
    digest = reg.StreamDigest(source="x", ending="error", ending_text=line, cut=RunCut.usage_limit, usage=CountedTokens())
    run = reg.get_run("r1").model_copy(update={"status": "failed", "finished_at": time.time(), "stream_digest": digest, "subscription": None})
    quota.record_refusal("claude", "fable-fixture", said=line, subscription=None)
    owed = reg.cut_resume(run)
    assert owed is not None and owed.at == run.finished_at
    step = walk.open_for(run)
    assert step is not None and step.how(run) == "login" and step.unit.login in {one.id for one in logins.values()}


def test_a_resumed_turn_waiting_on_every_login_names_each_with_its_reopening(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, logins, _ = _stopped_run(monkeypatch, tmp_path)
    run = reg.get_run("r1")
    for subscription in (*(one.id for one in logins.values()), None):
        quota.record_refusal("claude", "fable-fixture", said=_week_line(), subscription=subscription)
    line = SpawnLine(limit=6, waiting_flag_minutes=5, waiting_gap_seconds=60, memory=None)
    with line.resuming(run) as admitted:
        assert not admitted
    said = line.waiting_said()[run.run_id]
    assert said.startswith("en pause : limite d'usage claude jusqu'à ") and said.count("jusqu'à") == 3 and "ce PC" in said
