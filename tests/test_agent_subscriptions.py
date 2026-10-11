"""A subscription signed in on this PC: its own folder, never the default logins; a run pinned to it
for every turn and child; its usage read from the vendor's own lines; its caps held mid-run."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from galaius_core import SubscriptionAllowance, SubscriptionLimits

from galaius.agents import registry as reg
from galaius.agents.events import AgentEvent
from galaius.agents.providers import PROVIDERS, ClaudeCodeProvider
from galaius.agents.run import run_agent
from galaius.agents.subscription_watch import SubscriptionWatch
from galaius.agents.subscriptions import RunSubscription, Subscription, SubscriptionCapped, SubscriptionRefused
from galaius.fence import FenceBind, FenceSpec
from galaius.machine_programs import AgentPrograms
from tests.support.agents import ScriptedProvider, install_fake_cli, install_provider, use_policy

FAKE_CLAUDE = Path(__file__).parent / "fixtures" / "agents" / "fake_claude_login.py"
#: Lines as the vendors wrote them (a Claude Code run's stream; a Codex session record), only ids shortened.
CLAUDE_LINE = ('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1791559200,"rateLimitType":"five_hour","overageStatus":"rejected",'
               '"overageDisabledReason":"org_level_disabled","isUsingOverage":false,"unifiedWindows":{"five_hour":{"utilization":0.03,"resetsAt":1791559200},'
               '"seven_day":{"utilization":0.93,"resetsAt":1791842400}}},"uuid":"3bddb0b5","session_id":"ff5eac43"}')
CODEX_LINE = ('{"timestamp":"2026-09-22T20:46:43.619Z","type":"event_msg","payload":{"type":"token_count","info":null,"rate_limits":{"limit_id":"codex","limit_name":null,'
              '"primary":{"used_percent":16.0,"window_minutes":10080,"resets_at":1788452794},"secondary":null,"credits":{"has_credits":false,"unlimited":false,"balance":"0"},'
              '"individual_limit":null,"spend_control_reached":null,"plan_type":"prolite","rate_limit_reached_type":null}}}')


def tree(folder: Path) -> str:
    """Every path and byte beneath `folder`, as one digest."""
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        digest.update(str(path.relative_to(folder)).encode() + (path.read_bytes() if path.is_file() else b"/"))
    return digest.hexdigest()


@pytest.mark.parametrize(("provider", "line", "windows"), [
    ("claude", CLAUDE_LINE, [("five_hour", 3.0, 1791559200), ("weekly", 93.0, 1791842400)]),
    ("codex", CODEX_LINE, [("weekly", 16.0, 1788452794)]),
])
def test_usage_windows_come_from_the_vendors_own_lines(provider: str, line: str, windows: list) -> None:
    event = PROVIDERS[provider].parse(line)
    assert event.kind == "rate_limit"
    assert [(window.window, window.used_percent, window.resets_at.timestamp()) for window in event.usage] == windows


def test_a_subscription_signs_in_and_out_in_its_own_folder_and_the_default_logins_never_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = Path.home()
    for folder, name in ((home / ".claude", ".credentials.json"), (home / ".codex", "auth.json")):
        folder.mkdir()
        (folder / name).write_text('{"email": "owner@example.com"}')
        (folder / "settings.json").write_text("{}")
    before = tree(home / ".claude"), tree(home / ".codex")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_fake_cli(bin_dir, "claude", FAKE_CLAUDE)
    path = f"{bin_dir}:{Path(sys.executable).parent}"
    monkeypatch.setenv("PATH", path)
    monkeypatch.setattr(AgentPrograms, "poll_seconds", 0.05)
    monkeypatch.setattr(AgentPrograms, "installable", staticmethod(lambda: (PROVIDERS["claude"],)))
    programs = AgentPrograms(environment={"PATH": path, "HOME": str(home)}, log_path=tmp_path / "programs.log")
    subscription_id = uuid4()

    assert programs.install("claude", subscription_id).step == "signing_in"
    deadline = time.monotonic() + 10
    while (state := next(item for item in programs.states() if item.subscription_id == subscription_id)).step == "signing_in" and time.monotonic() < deadline:
        time.sleep(0.05)
    folder = Subscription.of(subscription_id).folder
    default = next(item for item in programs.states() if item.subscription_id is None)
    assert (state.step, state.account) == ("ready", "second@example.com") and len(state.login) == 16 and state.login != default.login
    assert folder.stat().st_mode & 0o777 == 0o700 and (folder / ".credentials.json").exists()
    assert default.account == "owner@example.com"
    programs.sign_out("claude", subscription_id)
    assert not folder.exists()
    assert (tree(home / ".claude"), tree(home / ".codex")) == before


def test_two_subscriptions_in_one_launch_are_two_login_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_fake_cli(bin_dir, "claude", FAKE_CLAUDE)
    path = f"{bin_dir}:{Path(sys.executable).parent}"
    monkeypatch.setenv("PATH", path)
    log = tmp_path / "status.log"
    first, second = Subscription.create(uuid4(), "claude"), Subscription.create(uuid4(), "claude")
    for subscription in (first, second):
        (subscription.folder / ".credentials.json").write_text('{"email": "signed@example.com"}')
    claude = ClaudeCodeProvider()
    for subscription in (first, second, first):
        asyncio.run(claude.authenticated({"PATH": path, "HOME": str(Path.home()), "CLAUDE_STATUS_LOG": str(log), **subscription.overlay()}))
    assert log.read_text().splitlines() == [str(first.folder), str(second.folder)]


class _Codex(ScriptedProvider):
    """A Codex-named child saying which login folder it runs with."""

    name = "codex"
    auth_home_env = ("CODEX_HOME",)
    script = (
        'import json,os\n'
        'print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":os.environ.get("CODEX_HOME","")}]}}), flush=True)\n'
        'print(json.dumps({"type":"result","subtype":"success","is_error":False,"usage":{"output_tokens":1},"session_id":"SID"}), flush=True)\n'
    )


@pytest.mark.asyncio
async def test_a_run_and_its_child_run_in_the_subscription_folder_never_the_default_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_policy(monkeypatch, agents={"tester": "fixture-model"}, reasoning={"tester": "medium"})
    install_provider(monkeypatch, _Codex())
    subscription = Subscription.create(uuid4(), "codex")
    assert Subscription.create(uuid4(), "claude").overlay().keys() == {"CLAUDE_CONFIG_DIR"}

    parent = await run_agent(None, "p", agent="tester", cwd=str(tmp_path), environment={"PATH": "/usr/bin:/bin"},
                             subscription=RunSubscription(subscription_id=subscription.id, limits=SubscriptionLimits(weekly_percent=80)))
    child = await run_agent(None, "c", agent="tester", cwd=str(tmp_path), parent_run_id=parent.run_id, environment={"PATH": "/usr/bin:/bin"})
    for handle in (parent, child):
        await asyncio.wait_for(handle.wait(), timeout=30)
        said = [event.text for event in reg.read_events(handle.run_id) if event.kind == "text"]
        assert said == [str(subscription.folder)] and reg.get_run(handle.run_id).subscription_id == subscription.id
    assert (subscription.folder / "config.toml").read_text() == 'cli_auth_credentials_store = "file"\n'


@pytest.mark.parametrize(("parent", "asked", "named", "pinned"), [
    ("x", None, "x", "x"),          # a child inherits its parent's
    ("x", "y", "x", None),          # a child asking another is refused
    ("x", None, "y", None),         # an environment naming another folder is refused
    (None, None, "x", None),        # a default-login parent's child naming a subscription folder is refused
    (None, "x", None, "x"),         # a start from the web on its subscription
])
def test_a_child_runs_on_its_parents_subscription_and_never_another(parent, asked, named, pinned) -> None:
    folders = {name: Subscription.create(uuid4(), "claude") for name in ("x", "y")}
    run = {name: RunSubscription(subscription_id=folders[name].id) for name in folders}
    environment = {"CLAUDE_CONFIG_DIR": str(folders[named].folder)} if named else {}
    resolve = lambda: RunSubscription.resolve(run.get(asked), parent=run.get(parent), child=parent is not None or asked is None and named is not None,
                                              environment=environment)
    if pinned is None:
        with pytest.raises(SubscriptionRefused):
            resolve()
    else:
        assert resolve().subscription_id == folders[pinned].id


@pytest.mark.parametrize(("at_cap", "stopped"), [("finish", False), ("cut", True)])
def test_a_cap_reached_mid_run_holds_or_cuts_and_an_allowance_lets_it_go_on(at_cap: str, stopped: bool) -> None:
    subscription = Subscription.create(uuid4(), "claude")
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)  # stop() ends its whole group
    try:
        run = reg.register(run_id=str(uuid4()), pid=worker.pid, provider="claude", name="w",
                           subscription=RunSubscription(subscription_id=subscription.id, limits=SubscriptionLimits(five_hour_percent=2, weekly_percent=80, at_cap=at_cap)))
        reading = PROVIDERS["claude"].parse(CLAUDE_LINE)
        reg.subscription_heard(run, [AgentEvent(kind="text", text="working"), reading])
        held = reg.get_run(run.run_id)
        assert (held.subscription.capped.window, held.subscription.capped.used_percent, held.subscription.capped.at_cap) == ("weekly", 93.0, at_cap)
        assert (held.status == "stopped") is stopped
        with pytest.raises(SubscriptionCapped, match="93% of its weekly window, its cap is 80%"):
            held.subscription.check()  # no next turn while the week stays at its cap
        reg.merge_subscription(run.run_id, allowance=SubscriptionAllowance(window="weekly", up_to_percent=95), capped=None)
        with pytest.raises(SubscriptionCapped, match="3% of its five hour window, its cap is 2%"):
            reg.get_run(run.run_id).subscription.check()  # the week's allowance never lifts the five hours' cap
        reg.merge_subscription(run.run_id, limits=SubscriptionLimits(weekly_percent=80, at_cap=at_cap))
        reg.get_run(run.run_id).subscription.check()
    finally:
        worker.kill()
        worker.wait()


def test_a_fenced_run_binds_its_subscriptions_credential_and_no_other_login(tmp_path: Path) -> None:
    subscription = Subscription.create(uuid4(), "claude")
    (subscription.folder / ".credentials.json").write_text("{}")
    for folder, name in ((".claude", ".credentials.json"), (".codex", "auth.json")):
        (Path.home() / folder).mkdir()
        (Path.home() / folder / name).write_text("{}")
    work = tmp_path / "work"
    work.mkdir()
    pinned = RunSubscription(subscription_id=subscription.id, limits=SubscriptionLimits())
    fence = FenceSpec(working_directory=work, start=work, state=tmp_path / "state", providers=("claude", "codex"), homes=pinned.homes()).build()
    credential = subscription.folder / ".credentials.json"
    assert FenceBind(source=credential, target=credential, writable=False) in fence.binds
    assert not any(bind.source.is_relative_to(Path.home() / folder) for bind in fence.binds for folder in (".claude", ".codex"))
    assert len({bind.target for bind in fence.binds if bind.writable}) == len([bind for bind in fence.binds if bind.writable])


@pytest.mark.asyncio
async def test_a_launch_inside_a_pinned_run_stays_on_its_subscription_whatever_its_environment_says(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The run this process descends from (here: pytest's own parent, registered as a pinned run)
    holds a launch with no parent id and no login variable to that run's subscription."""
    use_policy(monkeypatch, agents={"tester": "fixture-model"}, reasoning={"tester": "medium"})
    install_provider(monkeypatch, _Codex())
    subscription = Subscription.create(uuid4(), "codex")
    holder = reg.register(run_id=str(uuid4()), pid=os.getppid(), provider="codex", name="holder",
                          subscription=RunSubscription(subscription_id=subscription.id, limits=SubscriptionLimits()))
    assert reg.ancestor_run().run_id == holder.run_id
    child = await run_agent(None, "c", agent="tester", cwd=str(tmp_path), environment={"PATH": "/usr/bin:/bin"})
    await asyncio.wait_for(child.wait(), timeout=30)
    assert [event.text for event in reg.read_events(child.run_id) if event.kind == "text"] == [str(subscription.folder)]


def test_codex_usage_is_read_from_its_session_record_and_reported_once_sent() -> None:
    subscription = Subscription.create(uuid4(), "codex")
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        run = reg.register(run_id=str(uuid4()), pid=worker.pid, provider="codex", name="w", provider_session_id="01a0440c-3356-7bb3-ba6b-b314ca533469",
                           subscription=RunSubscription(subscription_id=subscription.id, limits=SubscriptionLimits()))
        record = subscription.folder / "sessions" / "2026" / "09" / "22" / f"rollout-2026-09-22T20-46-43-{run.provider_session_id}.jsonl"
        record.parent.mkdir(parents=True)
        record.write_text('{"type":"session_meta","payload":{}}\n' + CODEX_LINE + "\n")
        watch = SubscriptionWatch()
        watch.heard()
        assert [(window.window, window.used_percent) for window in subscription.usage()] == [("weekly", 16.0)]
        now = datetime.now(UTC)
        report = watch.report(now)
        assert watch.report(now) == report  # a report not sent yet is due again
        watch.sent(report, now)
        assert watch.report(now) is None
    finally:
        worker.kill()
        worker.wait()


def test_a_folder_with_a_broken_state_file_still_has_its_token_masked() -> None:
    subscription = Subscription.create(uuid4(), "claude")
    token = "sk-ant-oat01-" + "k" * 48
    (subscription.folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
    subscription.state_path.write_text("{not json")
    assert Subscription.all() == () and token in Subscription.tokens()
