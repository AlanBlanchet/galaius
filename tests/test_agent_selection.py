"""One ranked candidate list across every provider, walked in order until one can start.

"We rank based on all providers, we get the first one from the criteria, and if not available
we get the next one." Availability is a fact about THIS machine before anything runs — CLI on
PATH, provider switched on, logged in, the requested mode and attachments understood. A denial
or a failure once the child could act is the run's outcome, never a reason to try the next.
"""

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from interact.agents import registry as reg
from interact.agents.providers import PROVIDERS, PermissionMode, UnsupportedToolPolicy
from tests.support.agents import ScriptedProvider, use_policy
from interact.agents import quota
from interact.agents.run import (
    ModelUnavailable,
    _quota_probe as quota_probe,
    _child_output,
    _quota_message,
    _answered_at_startup,
    _startup_refusal,
    rank_candidates,
    run_agent,
)
import interact.server.tools_agents as tools_agents
from interact.cli import app_commands as cli
from tests.support.models import catalog_of, model

CRITERION = "cap.vlm and price.in >= 0"


class _Cli(ScriptedProvider):
    """A provider whose availability facts are set by the test, running a real subprocess."""

    verified = True
    installed = True
    logged_in = True
    probes = 0
    last_timeout = None

    def available(self):
        return self.installed

    async def authenticated(self, env, *, timeout=10):
        type(self).probes += 1
        type(self).last_timeout = timeout
        return self.logged_in


class _Alpha(_Cli):
    name = "alpha"
    binary = "alpha-cli"
    native_providers = frozenset({"vendor-a"})


class _Beta(_Cli):
    name = "beta"
    binary = "beta-cli"
    native_providers = frozenset({"vendor-b"})


class _Crashing(_Alpha):
    def command(self, *a, **k):
        return [sys.executable, "-c", "import sys; sys.exit(3)"]


class _QuotaRefusing(_Beta):
    """A provider that refuses instantly with the vendor's own quota/rate-limit wording (#181)."""

    def command(self, *a, **k):
        return [sys.executable, "-c",
                "import sys; sys.stderr.write(\"You've reached your model limit. "
                "Switch to another model\\n\"); sys.exit(1)"]


@pytest.fixture
def team(monkeypatch, tmp_path):
    """Two CLIs, each running its own vendor; the cheapest clearing model sits with `beta`."""
    alpha, beta = _Alpha(), _Beta()
    for cls in (_Alpha, _Beta):
        cls.installed = True
        cls.logged_in = True
        cls.probes = 0
        cls.last_timeout = None
    monkeypatch.setattr("interact.agents.providers.PROVIDERS", {"alpha": alpha, "beta": beta})
    monkeypatch.setattr("interact.agents.run.PROVIDERS", {"alpha": alpha, "beta": beta})
    monkeypatch.setattr(reg, "PROVIDERS", {"alpha": alpha, "beta": beta})
    use_policy(monkeypatch, agents={"tester": CRITERION}, reasoning={"tester": "medium"})
    with catalog_of(
        model(id="b-strong", provider="vendor-b", score=90.0, input_cost=0.2, output_cost=0.2),
        model(id="a-mid", provider="vendor-a", score=60.0, input_cost=1.0, output_cost=1.0),
        model(id="a-weak", provider="vendor-a", score=20.0, input_cost=2.0, output_cost=2.0),
        model(id="unreachable", provider="vendor-c", score=99.0, input_cost=0.1, output_cost=0.1),
    ):
        yield alpha, beta


def _ids(candidates):
    return [(c.provider, c.model, c.rank) for c in candidates]


def test_the_list_is_ranked_across_providers_and_deterministic(team):
    alpha, beta = team
    ranked = rank_candidates(CRITERION, {}, providers=[alpha, beta])
    assert _ids(ranked) == [("beta", "b-strong", 0), ("alpha", "a-mid", 1), ("alpha", "a-weak", 2)]
    assert [c.catalog_id for c in ranked] == ["vendor-b/b-strong", "vendor-a/a-mid", "vendor-a/a-weak"]
    assert rank_candidates(CRITERION, {}, providers=[beta, alpha]) == ranked


def test_an_explicit_provider_is_a_filter_on_the_same_list(team):
    alpha, _ = team
    assert _ids(rank_candidates(CRITERION, {}, providers=[alpha])) == [("alpha", "a-mid", 1), ("alpha", "a-weak", 2)]


@pytest.mark.parametrize("switch", ["beta/b-strong", "b-strong", "vendor-b/b-strong"])
def test_a_model_switched_off_is_never_a_candidate_even_as_fallback(team, monkeypatch, switch):
    """Owner 2026-10-05: « Woow, fable was used. Please make sure we don't activate it. It costs too much... »"""
    alpha, beta = team
    use_policy(monkeypatch, agents={"tester": CRITERION}, models={switch: False})
    assert _ids(rank_candidates(CRITERION, {}, providers=[alpha, beta])) == [("alpha", "a-mid", 1), ("alpha", "a-weak", 2)]
    with pytest.raises(ModelUnavailable, match="switched off"):
        rank_candidates("b-strong", {}, providers=[beta])


def test_nothing_runnable_names_every_pool(team):
    with pytest.raises(ModelUnavailable, match="alpha, beta"):
        rank_candidates("price.in > 100", {}, providers=list(team))


def test_plain_model_unavailable_for_selected_provider_names_pool(team):
    alpha, beta = team
    with pytest.raises(ModelUnavailable, match="alpha.*b-strong"):
        rank_candidates("b-strong", {}, providers=[alpha])


@pytest.mark.asyncio
async def test_failed_auth_probe_has_its_own_skip_reason(team, tmp_path):
    alpha, beta = team
    beta.logged_in = None
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(run.wait(), 30)
    record = reg.get_run(run.run_id)
    assert record.provider == "alpha"
    assert [item.reason for item in record.skipped] == ["auth_check_failed"]
    assert record.skipped[0].message.startswith("login intent:")


@pytest.mark.asyncio
async def test_the_first_candidate_runs_when_available(team, tmp_path):
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(run.wait(), 30)
    saved = reg.get_run(run.run_id)
    assert (saved.provider, saved.model, saved.reasoning) == ("beta", "b-strong", "medium")
    assert saved.skipped == () and [c.model for c in saved.candidates] == ["b-strong", "a-mid", "a-weak"]
    assert _Beta.last_timeout == 10


@pytest.mark.asyncio
async def test_machine_touch_default_reaches_the_spawned_run(team, tmp_path, monkeypatch):
    """A local shared touch intent is resolved before provider selection and recorded on the
    actual run, rather than leaving the child in the harness's read-only default."""
    use_policy(monkeypatch, agents={"tester": CRITERION}, reasoning={"tester": "medium"},
               defaults={"touch": "workspace_write"})
    for provider_type in (_Alpha, _Beta):
        monkeypatch.setattr(provider_type, "validate_permission_mode", lambda self, mode: None)
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(run.wait(), 30)
    assert reg.get_run(run.run_id).permission_mode == "workspace_write"


@pytest.mark.asyncio
@pytest.mark.parametrize("fact,reason", [("installed", "cli_missing"), ("logged_in", "unauthenticated")])
async def test_an_unavailable_first_candidate_falls_through_to_the_next(team, tmp_path, fact, reason):
    _, beta = team
    setattr(_Beta, fact, False)
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(run.wait(), 30)
    saved = reg.get_run(run.run_id)
    assert (saved.provider, saved.model) == ("alpha", "a-mid")
    assert [(s.candidate.provider, s.reason) for s in saved.skipped] == [("beta", reason)]


@pytest.mark.asyncio
async def test_a_quota_refusal_falls_through_to_the_next_candidate(team, tmp_path, monkeypatch):
    """#181: a provider answering "you've reached your <model> limit, switch to another model"
    is UNAVAILABLE for that candidate the same as a missing CLI — the run falls through under
    the SAME criterion and records which candidate it used and why the first was skipped."""
    alpha, beta = team
    refusing = _QuotaRefusing()
    monkeypatch.setattr("interact.agents.run.PROVIDERS", {"alpha": alpha, "beta": refusing})
    monkeypatch.setattr("interact.agents.providers.PROVIDERS", {"alpha": alpha, "beta": refusing})
    monkeypatch.setattr(reg, "PROVIDERS", {"alpha": alpha, "beta": refusing})
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(run.wait(), 30)
    saved = reg.get_run(run.run_id)
    assert (saved.provider, saved.model) == ("alpha", "a-mid")
    assert [(s.candidate.provider, s.reason) for s in saved.skipped] == [("beta", "quota_exceeded")]
    assert "quota intent:" in saved.skipped[0].message
    assert "Retry" in saved.skipped[0].message
    assert saved.status == "done"


_QUOTED = "You've hit your weekly limit · resets 9pm (Europe/Paris)"
_ALLOWED_5H = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}}


def _turn(model: str, text: str = "on it") -> dict:
    return {"type": "assistant", "message": {"model": model, "content": [{"type": "text", "text": text}]}}


@pytest.mark.parametrize(("lines", "verdict", "answered"), [
    pytest.param([{"type": "user", "message": {"content": [{"type": "tool_result", "content": _QUOTED}]}}],
                 None, False, id="a tool's output quoting a refusal refused nothing"),
    pytest.param([_turn("claude-opus-5-5", _QUOTED)], None, True, id="the model's own words refused nothing"),
    pytest.param([{"type": "result", "is_error": False, "result": _QUOTED}], None, False, id="a healthy final answer"),
    pytest.param([_turn("<synthetic>", _QUOTED)], "quota_exceeded", False, id="the CLI's own synthetic notice"),
    # 59d1a39a: the five-hour pool allows, the next line refuses for the seven-day one.
    pytest.param([_ALLOWED_5H, {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "rateLimitType": "seven_day_overage_included"}}],
                 "quota_exceeded", False, id="an allowed line before a refusal proves nothing"),
    pytest.param([{"type": "error", "message": "The 'gpt-6-astra' model is not supported when using Codex with a ChatGPT account."}],
                 "model_capability_unsupported", False, id="codex refusing the model for this login"),
    pytest.param([{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "rateLimitType": "seven_day",
                   "overageStatus": "rejected", "utilization": 0.99}}], None, False, id="a warning is no refusal"),
    pytest.param([{"type": "item.completed", "item": {"type": "agent_message", "text": _QUOTED}}], None, True,
                 id="a codex item is the model answering"),
    # Codex warns about the model as an `error` item, then refuses it (7b9198ba).
    pytest.param([{"type": "item.completed", "item": {"type": "error", "message": "Model metadata for `x` not found."}},
                  {"type": "error", "message": "The 'x' model is not supported when using Codex with a ChatGPT account."}],
                 "model_capability_unsupported", False, id="a codex warning item is not the model answering"),
])
def test_the_startup_probe_reads_only_what_the_vendor_says(tmp_path, monkeypatch, lines, verdict, answered):
    """A child that read the launcher's code or a brief must not be killed as « refused »; only a
    turn the model wrote ends the look early."""
    monkeypatch.setenv("INTERACT_AGENTS_DIR", str(tmp_path / "agents"))
    path = reg.raw_events_path("probe")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n"
                            for line in [{"type": "system", "subtype": "init"}, *lines]))
    assert _startup_refusal(_child_output("probe")) == verdict
    assert _answered_at_startup("probe") is answered


@pytest.mark.asyncio
async def test_a_refusal_after_an_allowed_line_is_still_caught_by_a_live_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERACT_AGENTS_DIR", str(tmp_path / "agents"))
    raw = reg.raw_events_path("probe-live")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(_ALLOWED_5H) + "\n")

    async def refuse():
        await asyncio.sleep(0.5)
        with raw.open("a") as stream:
            stream.write(json.dumps(_turn("<synthetic>", _QUOTED), ensure_ascii=False) + "\n")

    writer = asyncio.create_task(refuse())
    reason = await quota_probe("probe-live", SimpleNamespace(returncode=None), window=2.0)
    await writer
    assert reason == "quota_exceeded"


def test_quota_skip_message_names_window_and_clear_time() -> None:
    now = 1_790_000_000.0
    said = '{"status":"rejected","rateLimitType":"seven_day","resetsAt":1790000900}'

    message = _quota_message("codex", "gpt-5.6-luna", said, quota.record_refusal("codex", "gpt-5.6-luna", said=said, now=now))

    assert "quota intent: codex/gpt-5.6-luna exhausted seven_day" in message
    assert "2026-09-21T14:29:20+00:00" in message
    assert "Retry then" in message


@pytest.mark.asyncio
async def test_a_refusal_that_reaches_disk_after_the_child_exits_is_still_seen(tmp_path, monkeypatch):
    """The vendor prints its quota refusal through the supervisor's reader, so the line can land
    a moment AFTER the child is gone. Reading the stream once, at the instant the process exits,
    sees an empty file and lets the run commit to a candidate that never ran (#181): the probe
    keeps looking for a short grace period once the child is dead."""
    raw = reg.raw_events_path("probe-run")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("")

    async def refuse_late():
        await asyncio.sleep(0.3)
        raw.write_text(json.dumps({"type": "assistant", "message": {"model": "<synthetic>", "content": [
            {"type": "text", "text": "You've reached your Fable limit. Switch to another model, or manage usage credits"}]}}) + "\n")

    writer = asyncio.create_task(refuse_late())
    reason = await quota_probe("probe-run", SimpleNamespace(returncode=1), window=1.0)
    await writer
    assert reason == "quota_exceeded"


class _ClaudeNamed(_Cli):
    """Two candidates of the SAME vendor: it is the vendor that owns the session id namespace."""

    name = "claude"
    binary = "claude-cli"
    native_providers = frozenset({"vendor-a", "vendor-b"})
    sessions: list[str] = []

    def definition_path(self, agent):
        return Path(os.environ["HOME"]) / ".claude" / "agents" / f"{agent}.md"

    def command(self, task, *, cwd, model, mcp_config, run_id, agent=None,
                permission_mode=None, allowed_tools=None, reasoning=None, image_paths=(),
                coarse_accepted=False):
        type(self).sessions.append(run_id)
        if len(type(self).sessions) == 1:  # the first candidate, out of quota
            return [sys.executable, "-c",
                    "import sys; sys.stderr.write(\"You've reached your model limit\\n\"); sys.exit(1)"]
        return [sys.executable, "-c", self.script]


@pytest.mark.asyncio
async def test_a_terminal_launch_waits_longer_for_the_vendors_refusal(tmp_path, monkeypatch):
    """`run_agent` returns as soon as the child is alive — four seconds, less than a vendor takes
    to answer "you've reached your limit", so the fall-through never sees the refusal. A human
    watching `interact agents spawn` can afford that wait; the supervisor cannot, so the longer
    window belongs to the CLI and the short one stays the default."""
    seen = {}

    async def capture(provider, task, **kwargs):
        seen["window"] = kwargs.get("quota_window")
        return SimpleNamespace(run_id="r")

    monkeypatch.setattr(cli, "run_agent", capture)
    await cli._run_agent_for_cli(None, "t", cwd=str(tmp_path))
    assert seen["window"] == cli.CLI_QUOTA_WINDOW and cli.CLI_QUOTA_WINDOW > 4.0


@pytest.mark.asyncio
async def test_the_launch_hands_its_window_to_the_probe(team, tmp_path, monkeypatch):
    """The knob is worth nothing unless it reaches the one place that waits."""
    seen = {}

    async def probe(run_id, process, *, window=4.0, **rest):
        seen["window"] = window
        return None

    monkeypatch.setattr("interact.agents.run._quota_probe", probe)
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False, quota_window=0.05)
    await asyncio.wait_for(run.wait(), 30)
    assert seen["window"] == 0.05


@pytest.mark.asyncio
async def test_a_second_candidate_of_the_same_vendor_gets_its_own_session_id(team, tmp_path, monkeypatch):
    """The vendor refuses a session id its dead first child already claimed ("Session ID ... is
    already in use"), so the run that falls through to another model of the SAME vendor asks the
    vendor for a fresh one instead of dying on arrival."""
    alpha, _ = team
    definitions = tmp_path / ".claude" / "agents"
    definitions.mkdir(parents=True, exist_ok=True)
    (definitions / "tester.md").write_text("---\nname: tester\n---\nBe skeptical.\n", encoding="utf-8")
    vendor = _ClaudeNamed()
    _ClaudeNamed.sessions = []
    monkeypatch.setattr("interact.agents.run.PROVIDERS", {"claude": vendor})
    monkeypatch.setattr("interact.agents.providers.PROVIDERS", {"claude": vendor})
    monkeypatch.setattr(reg, "PROVIDERS", {"claude": vendor})
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(run.wait(), 30)
    saved = reg.get_run(run.run_id)
    assert [s.reason for s in saved.skipped] == ["quota_exceeded"]
    assert len(_ClaudeNamed.sessions) == 2 and len(set(_ClaudeNamed.sessions)) == 2
    assert _ClaudeNamed.sessions[0] == run.run_id
    assert _ClaudeNamed.sessions[1] != run.run_id  # the dead child owns the first one
    assert saved.status == "done"


@pytest.mark.asyncio
async def test_a_model_that_refused_a_moment_ago_is_passed_over_before_any_child_is_spawned(
    team, tmp_path, monkeypatch,
):
    """"Three research agents died on your Fable quota, twice each": a refusal is a fact about
    the account for a period, so the next launch must not spend another child discovering it."""
    alpha, beta = team
    top = rank_candidates(CRITERION, dict(os.environ), providers=[alpha, beta])[0]
    quota.record_refusal(top.provider, top.model)
    _Cli.probes = 0
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(run.wait(), 30)
    saved = reg.get_run(run.run_id)
    assert (saved.provider, saved.model) != (top.provider, top.model)
    assert [(s.candidate.model, s.reason) for s in saved.skipped][0] == (top.model, "quota_exceeded")
    assert saved.status == "done"


@pytest.mark.asyncio
async def test_a_stale_memory_never_stops_every_candidate_from_running(team, tmp_path):
    """The note is a shortcut, never a veto: with every model remembered as refused, the walk
    ignores the memory rather than telling the owner nothing can run."""
    alpha, beta = team
    for candidate in rank_candidates(CRITERION, dict(os.environ), providers=[alpha, beta]):
        quota.record_refusal(candidate.provider, candidate.model)
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(run.wait(), 30)
    saved = reg.get_run(run.run_id)
    assert saved.status == "done" and saved.skipped == ()


@pytest.mark.asyncio
async def test_a_switched_off_provider_is_skipped_with_its_reason(team, tmp_path, monkeypatch):
    use_policy(monkeypatch, agents={"tester": CRITERION}, reasoning={"tester": "medium"}, providers={"beta": False})
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(run.wait(), 30)
    saved = reg.get_run(run.run_id)
    assert saved.provider == "alpha" and [s.reason for s in saved.skipped] == ["provider_off"]


@pytest.mark.asyncio
async def test_every_candidate_unavailable_is_one_clear_failure(team, tmp_path):
    _Beta.installed = False
    _Alpha.logged_in = False
    with pytest.raises(ModelUnavailable) as caught:
        await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    text = str(caught.value)
    assert "beta/b-strong: cli_missing" in text and "alpha/a-mid: unauthenticated" in text
    assert "alpha/a-weak: unauthenticated" in text
    assert "install beta" in text and "sign in with its CLI" in text


@pytest.mark.asyncio
async def test_an_explicit_provider_filters_the_same_auth_checked_path(team, tmp_path):
    alpha, beta = team
    _Beta.installed = False
    with pytest.raises(RuntimeError, match="not installed"):
        await run_agent(beta, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    run = await run_agent(alpha, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(run.wait(), 30)
    saved = reg.get_run(run.run_id)
    assert saved.provider == "alpha" and saved.skipped == ()
    assert [c.provider for c in saved.candidates] == ["alpha", "alpha"]
    assert _Alpha.probes == 1 and _Beta.probes == 0


@pytest.mark.asyncio
async def test_explicit_provider_logged_out_is_refused_without_running_another(team, tmp_path):
    alpha, _ = team
    _Alpha.logged_in = False
    with pytest.raises(ModelUnavailable, match="unauthenticated"):
        await run_agent(alpha, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    assert _Alpha.probes == 1 and _Beta.probes == 0


@pytest.mark.asyncio
async def test_unenforceable_role_tools_skip_candidate_without_weakening_role(team, tmp_path, monkeypatch):
    def reject(self, allowed_tools, denied_tools, *, coarse_accepted=False):
        if not coarse_accepted:
            raise UnsupportedToolPolicy("cannot enforce")
    monkeypatch.setattr(_Beta, "validate_tool_policy", reject)
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(run.wait(), 30)
    saved = reg.get_run(run.run_id)
    assert saved.provider == "alpha"
    assert [item.reason for item in saved.skipped] == ["tool_policy_unsupported"]


@pytest.mark.asyncio
async def test_coarse_tool_policy_acceptance_is_per_role_per_provider_never_a_default(
    team, tmp_path, monkeypatch,
):
    """A provider a role did NOT explicitly accept stays refused (the unmodified assertion
    above); once the OPERATOR names `role -> provider` in policy, that SAME provider — which
    still cannot enforce the role's tool list, only its own coarser sandbox — is no longer
    skipped, and no OTHER role or provider is affected by naming it."""
    def reject(self, allowed_tools, denied_tools, *, coarse_accepted=False):
        if not coarse_accepted:
            raise UnsupportedToolPolicy("cannot enforce")
    monkeypatch.setattr(_Beta, "validate_tool_policy", reject)
    monkeypatch.setattr(_Alpha, "installed", False)  # force the walk onto beta
    use_policy(monkeypatch, agents={"tester": CRITERION}, providers={"alpha": True, "beta": True},
              coarse_tool_policy={"tester": ["beta"]})
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(run.wait(), 30)
    saved = reg.get_run(run.run_id)
    assert saved.provider == "beta" and saved.skipped == ()


@pytest.mark.asyncio
async def test_a_failure_after_launch_is_the_runs_outcome_not_a_retry(team, tmp_path, monkeypatch):
    crashing = _Crashing()
    monkeypatch.setitem(PROVIDERS, "alpha", crashing)
    monkeypatch.setattr("interact.agents.run.PROVIDERS", {"alpha": crashing})
    monkeypatch.setitem(reg.PROVIDERS, "alpha", crashing)
    spawned = []
    original = asyncio.create_subprocess_exec

    async def counting(*argv, **kwargs):
        spawned.append(argv)
        return await original(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting)
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(run.wait(), 30)
    saved = reg.get_run(run.run_id)
    assert saved.status == "failed" and saved.provider == "alpha" and saved.skipped == ()
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_a_permission_mode_the_candidate_cannot_honour_skips_it(team, tmp_path, monkeypatch):
    monkeypatch.setattr(_Alpha, "permission_modes", lambda self: [PermissionMode("careful", "Careful", "asks")])
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False, permission_mode="careful")
    await asyncio.wait_for(run.wait(), 30)
    saved = reg.get_run(run.run_id)
    assert saved.provider == "alpha" and [s.reason for s in saved.skipped] == ["permission_mode_unsupported"]
    assert "touch/approval intent" in saved.skipped[0].message


@pytest.mark.asyncio
async def test_ranked_launch_preserves_workspace_permissions_for_the_chosen_provider(team, tmp_path, monkeypatch):
    monkeypatch.setattr(_Beta, "permission_modes", lambda self: [PermissionMode("careful", "Careful", "asks")])
    run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False, provider_modes={"beta": "careful"})
    await asyncio.wait_for(run.wait(), 30)
    saved = reg.get_run(run.run_id)
    assert saved.provider == "beta" and saved.permission_mode == "careful"


def test_a_historical_record_loads_with_no_candidates():
    run = reg.AgentRun(run_id="old", provider="claude", name="old")
    assert run.candidates == () and run.skipped == ()


def _capture(seen):
    async def capture(provider, task, **kwargs):
        seen.append(provider)
        raise RuntimeError("stop here")
    return capture


@pytest.mark.asyncio
async def test_mcp_spawn_defaults_to_the_ranked_choice(monkeypatch):

    seen = []
    monkeypatch.setattr(tools_agents, "run_agent", _capture(seen))
    out = await tools_agents.agent_spawn("do it", session_id="fixture-conversation")
    assert out.startswith("ERROR") and "stop here" in out
    assert seen == [None]


def test_cli_spawn_defaults_to_the_ranked_choice(monkeypatch):
    # `interact.cli.app` is both a module and the re-exported App object; the command reads the
    # runner off the MODULE, so import it explicitly rather than by attribute access.
    commands = cli
    cli_module = importlib.import_module("interact.cli.app")
    seen = []
    monkeypatch.setattr(cli_module, "_run_agent_for_cli", _capture(seen), raising=False)
    with pytest.raises(SystemExit):
        commands.agents_spawn("do it", agent="tester", session_id="fixture-conversation")
    assert seen == [None]


class TestProviderConstraintOnTheRankedList:
    """A role's criterion can require, prefer, or exclude a provider — the same "and"-joined
    criteria language, applied over the ranked (model, CLI) list. A hard capability or price term
    is unaffected: the provider constraint only filters/orders what already qualifies."""

    def test_require_drops_every_other_providers_candidates(self, team):
        alpha, beta = team
        # b-strong (vendor-b/beta) is the top-ranked candidate; REQUIRE alpha removes it, never
        # falls through silently to it.
        assert _ids(rank_candidates(f"{CRITERION} and provider = alpha", {}, providers=[alpha, beta])) == [
            ("alpha", "a-mid", 1), ("alpha", "a-weak", 2),
        ]

    def test_require_names_itself_in_the_refusal(self, team):
        alpha, beta = team
        with pytest.raises(ModelUnavailable, match=r"REQUIRE"):
            rank_candidates(f"{CRITERION} and provider = alpha and price.in < 0.5",
                            {}, providers=[alpha, beta])

    def test_exclude_drops_only_that_provider(self, team):
        alpha, beta = team
        assert _ids(rank_candidates(f"{CRITERION} and provider != beta", {}, providers=[alpha, beta])) == [
            ("alpha", "a-mid", 1), ("alpha", "a-weak", 2),
        ]

    def test_prefer_reorders_but_never_drops_the_rest(self, team):
        alpha, beta = team
        # b-strong outranks everything on raw score; PREFER alpha still brings alpha's candidates
        # first, but beta stays reachable as a fallback rather than disappearing.
        candidates = rank_candidates(f"{CRITERION} and provider ~ alpha", {}, providers=[alpha, beta])
        assert _ids(candidates) == [("alpha", "a-mid", 1), ("alpha", "a-weak", 2), ("beta", "b-strong", 0)]

    def test_no_provider_constraint_is_unaffected(self, team):
        alpha, beta = team
        assert _ids(rank_candidates(CRITERION, {}, providers=[alpha, beta])) == [
            ("beta", "b-strong", 0), ("alpha", "a-mid", 1), ("alpha", "a-weak", 2),
        ]

    @pytest.mark.asyncio
    async def test_a_required_provider_actually_runs_the_agent(self, team, tmp_path, monkeypatch):
        alpha, beta = team
        use_policy(monkeypatch, agents={"tester": f"{CRITERION} and provider = alpha"},
                   reasoning={"tester": "medium"})
        run = await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
        await asyncio.wait_for(run.wait(), 30)
        saved = reg.get_run(run.run_id)
        assert saved.provider == "alpha"

    @pytest.mark.asyncio
    async def test_a_required_provider_that_cannot_run_refuses_naming_require(self, team, tmp_path, monkeypatch):
        alpha, beta = team
        use_policy(monkeypatch, agents={"tester": f"{CRITERION} and provider = alpha and price.in < 0.5"},
                   reasoning={"tester": "medium"})
        with pytest.raises(ModelUnavailable, match=r"REQUIRE"):
            await run_agent(None, "t", agent="tester", cwd=str(tmp_path), mesh=False)
