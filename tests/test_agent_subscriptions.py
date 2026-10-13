"""A subscription signed in on this PC: its own folder, never the default logins; a run pinned to it
for every turn and child; its usage read from the vendor's own lines; its caps held mid-run."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from galaius_core import AgentMoveRequest, MachineRef, SubscriptionAllowance, SubscriptionChoice, UsageWindow, SubscriptionChoices, SubscriptionLimits

from galaius.agents import registry as reg
from galaius.agents import quota
from galaius.agents.events import AgentEvent
from galaius.agents.providers import PROVIDERS, ClaudeCodeProvider
from galaius.agents import run as run_module
from galaius.agents.run import run_agent
from galaius.agents.subscription_watch import SubscriptionWatch
from galaius.agents.subscriptions import RunSubscription, Subscription, SubscriptionCapped, SubscriptionRefused, UsageLimited
from galaius.fence import FenceBind, FenceSpec
from galaius.machine_programs import AgentPrograms
from galaius.machine_agents import MachineAgents
from galaius.cli.app_commands import agents_resume
from tests.support.agents import ScriptedProvider, install_fake_cli, install_provider, use_policy

#: Windows keeps no POSIX mode bits (`chmod` sets only the read-only flag).
POSIX_MODES = sys.platform != "win32"
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
    path = os.pathsep.join((str(bin_dir), str(Path(sys.executable).parent)))
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
    assert (not POSIX_MODES or folder.stat().st_mode & 0o777 == 0o700) and (folder / ".credentials.json").exists()
    assert default.account == "owner@example.com"
    programs.sign_out("claude", subscription_id)
    assert not folder.exists()
    assert (tree(home / ".claude"), tree(home / ".codex")) == before


def test_two_subscriptions_in_one_launch_are_two_login_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_fake_cli(bin_dir, "claude", FAKE_CLAUDE)
    path = os.pathsep.join((str(bin_dir), str(Path(sys.executable).parent)))
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


class _Claude(_Codex):
    """A Claude-named child saying which login folder it runs with."""

    name = "claude"
    auth_home_env = ("CLAUDE_CONFIG_DIR",)
    script = _Codex.script.replace("CODEX_HOME", "CLAUDE_CONFIG_DIR")


@pytest.mark.asyncio
async def test_each_candidate_runs_on_its_own_providers_subscription_and_the_run_keeps_the_one_it_started_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """His order puts Codex first, the role ranks Claude only: it runs on his Claude subscription, never refused for
    the Codex one; a provider he named none of runs on the PC's own login; a capped one is passed over, and when it
    stood alone the refusal says the cap."""
    use_policy(monkeypatch, agents={"tester": "fixture-model"}, reasoning={"tester": "medium"})
    definition = tmp_path / "tester.md"
    definition.write_text("---\nname: tester\n---\nTest.\n", encoding="utf-8")
    monkeypatch.setattr(_Claude, "definition_path", lambda self, agent: definition)
    for provider in (_Codex(), _Claude()):
        install_provider(monkeypatch, provider)
    monkeypatch.setattr(run_module, "rank_candidates", lambda rule, env, *, providers, weights="", role=None: (
        reg.LaunchCandidate(provider="claude", model="fixture-model", rank=0),))
    codex, claude = Subscription.create(uuid4(), "codex"), Subscription.create(uuid4(), "claude")
    choice = lambda held, **limits: SubscriptionChoice(provider=held.provider, subscription_id=held.id, limits=SubscriptionLimits(**limits))  # noqa: E731
    launch = lambda choices: run_agent(None, "go", agent="tester", cwd=str(tmp_path), environment={"PATH": "/usr/bin:/bin"},  # noqa: E731
                                       choices=SubscriptionChoices(subscriptions=choices))
    for choices, folder, pinned in (((choice(codex), choice(claude)), str(claude.folder), claude.id), ((choice(codex),), "", None)):
        handle = await launch(choices)
        await asyncio.wait_for(handle.wait(), timeout=30)
        said = [event.text for event in reg.read_events(handle.run_id) if event.kind == "text"]
        assert said == [folder] and reg.get_run(handle.run_id).subscription_id == pinned
    # Fenced, the started candidate's login folder is the only one bound: every other chosen one is closed.
    fence = FenceSpec(working_directory=tmp_path, start=tmp_path, state=tmp_path / "state", providers=("claude", "codex"))
    closed = Subscription.root() / Subscription.CLOSED
    for choices, homes in (((choice(codex), choice(claude)), {"claude": claude.folder, "codex": closed / "codex"}), ((choice(codex),), {"codex": closed / "codex"})):
        handle = await run_agent(None, "go", agent="tester", cwd=str(tmp_path), environment={"PATH": "/usr/bin:/bin"}, fence=fence,
                                 choices=SubscriptionChoices(subscriptions=choices))
        await asyncio.wait_for(handle.wait(), timeout=30)
        assert reg.get_run(handle.run_id).fence.homes == homes
    claude.observe((UsageWindow(window="weekly", used_percent=85, observed_at=datetime.now(UTC)),))
    with pytest.raises(SubscriptionCapped) as capped:  # its only provider is capped: never its default login instead
        await launch((choice(codex), choice(claude, weekly_percent=80)))
    assert (capped.value.refusal.reason, capped.value.cap.cap_percent) == ("capped", 80)
    spare = Subscription.create(uuid4(), "claude")  # his second Claude subscription: the walk falls back to it, never to the default login
    handle = await launch((choice(codex), choice(claude, weekly_percent=80), choice(spare)))
    await asyncio.wait_for(handle.wait(), timeout=30)
    record = reg.get_run(handle.run_id)
    assert ([event.text for event in reg.read_events(handle.run_id) if event.kind == "text"], record.subscription_id) == ([str(spare.folder)], spare.id)
    with pytest.raises(SubscriptionRefused) as elsewhere:  # a subscription offered as another provider's login is never run as this one
        await launch((SubscriptionChoice(provider="claude", subscription_id=codex.id, limits=SubscriptionLimits()),))
    assert elsewhere.value.refusal.reason == "not_signed_in" and "codex login here, never claude" in str(elsewhere.value)
    with pytest.raises(SubscriptionRefused) as missing:  # its only provider's subscription is not on this PC: said so, typed
        await launch((SubscriptionChoice(provider="claude", subscription_id=uuid4(), limits=SubscriptionLimits()),))
    assert missing.value.refusal.reason == "not_signed_in"


@pytest.mark.parametrize(("parent", "asked", "named", "pinned"), [
    ("x", None, "x", "x"),          # a child inherits its parent's
    ("x", "y", "x", None),          # a child asking another is refused
    ("x", None, "y", None),         # an environment naming another folder is refused
    (None, None, "x", None),        # a default-login parent's child naming a subscription folder is refused
    (None, "x", None, "x"),         # a start from the web on its subscription
])
def test_a_child_runs_on_its_parents_subscription_and_never_another(parent, asked, named, pinned) -> None:
    folders = {name: Subscription.create(uuid4(), "claude") for name in ("x", "y")}
    run = {name: RunSubscription(subscription_id=folders[name].id, limits=SubscriptionLimits()) for name in folders}
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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no parent walk (`registry.ancestor_run`)")
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


@pytest.mark.parametrize("fenced", [False, True])
def test_codex_usage_is_read_from_its_session_record_and_reported_once_sent(tmp_path: Path, fenced: bool) -> None:
    """Unfenced, Codex keeps its session record in the subscription folder; fenced, in the run's own
    private state (`FenceSpec.sessions`)."""
    subscription = Subscription.create(uuid4(), "codex")
    fence = FenceSpec(working_directory=tmp_path, start=tmp_path, state=tmp_path / "state", providers=("codex",)) if fenced else None
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        run = reg.register(run_id=str(uuid4()), pid=worker.pid, provider="codex", name="w", provider_session_id="01a0440c-3356-7bb3-ba6b-b314ca533469",
                           subscription=RunSubscription(subscription_id=subscription.id, limits=SubscriptionLimits()), fence=fence)
        sessions = fence.sessions("codex")[0] if fence is not None else subscription.folder / "sessions"
        record = sessions / "2026" / "09" / "22" / f"rollout-2026-09-22T20-46-43-{run.provider_session_id}.jsonl"
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


def test_a_subscription_takes_the_owners_hooks_permissions_and_instructions_and_nothing_else() -> None:
    home = Path.home()
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    (home / ".claude" / "hooks").mkdir()
    (home / ".claude" / "hooks" / "stop.sh").write_text("#!/bin/sh\nexit 0\n")
    (home / ".claude" / "hooks" / "stop.sh").chmod(0o700)
    (home / ".claude" / "hooks" / "turn-end-asks.md").write_text("the gate's asks, read from beside it\n")
    (home / ".claude" / "hooks" / "lib").mkdir()
    (home / ".claude" / "hooks" / "lib" / "common.py").write_text("SHARED = 1\n")
    (home / ".claude" / "hooks" / "codex-agent-policy.sh").write_text("#!/bin/sh\nexit 0\n")
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/stop.sh"},
                                      {"type": "command", "command": "python3 $HOME/.claude/.credentials.json"}]}]},
        "permissions": {"allow": ["Bash(git:*)"]},
        "env": {"ANTHROPIC_API_KEY": "sk-owner"}, "apiKeyHelper": "~/bin/key", "model": "owner-model"}))
    (home / ".claude" / "CLAUDE.md").write_text("owner instructions\n")
    (home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "owner"}}')
    (home / ".codex" / "AGENTS.md").write_text("owner codex instructions\n")
    (home / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [
        {"type": "command", "command": "$HOME/.claude/hooks/codex-agent-policy.sh"}]}]}, "other": "never taken"}))
    before = tree(home / ".claude"), tree(home / ".codex")
    claude, codex = Subscription.create(uuid4(), "claude"), Subscription.create(uuid4(), "codex")
    (claude.folder / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "own"}, "env": {"KEPT": "1"}}))

    for subscription in (claude, codex):
        subscription.adopt({"HOME": str(home), **subscription.overlay()})  # as inside a pinned run: its variable names the folder

    settings = json.loads((claude.folder / "settings.json").read_text())
    folder = claude.folder.as_posix()
    assert settings == {"statusLine": {"type": "command", "command": "own"}, "env": {"KEPT": "1"}, "permissions": {"allow": ["Bash(git:*)"]},
                        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"{folder}/hooks/stop.sh"},
                                                      {"type": "command", "command": f"python3 {folder}/.credentials.json"}]}]}}
    assert ".claude/" not in json.dumps(settings["hooks"]) and (not POSIX_MODES or (claude.folder / "hooks" / "stop.sh").stat().st_mode & 0o700 == 0o700)
    source = home / ".claude" / "hooks"
    assert all((claude.folder / "hooks" / path.relative_to(source)).read_bytes() == path.read_bytes() for path in source.rglob("*") if path.is_file())
    codex_hooks = json.loads((codex.folder / "hooks.json").read_text())
    assert ".claude/" not in json.dumps(codex_hooks) and codex_hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == f"{codex.folder.as_posix()}/hooks/codex-agent-policy.sh"
    assert "other" not in codex_hooks and (codex.folder / "hooks" / "codex-agent-policy.sh").is_file()
    assert (claude.folder / "CLAUDE.md").read_text() == "owner instructions\n" and (codex.folder / "AGENTS.md").read_text() == "owner codex instructions\n"
    assert not (claude.folder / ".credentials.json").exists() and not (codex.folder / "auth.json").exists()
    assert (tree(home / ".claude"), tree(home / ".codex")) == before


def test_continuing_on_another_subscription_carries_the_session_never_a_credential_and_pins_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """Contract A9: his « Continuer sur Claude pro » after a usage limit copies that session's transcript from A's folder
    to B's (nothing else, never a credential), pins the run to B; a Codex login is never a target of a Claude session; a
    turn asked while a cap or the vendor's limit holds is refused typed, with when it reopens."""
    a, b, codex = Subscription.create(uuid4(), "claude"), Subscription.create(uuid4(), "claude"), Subscription.create(uuid4(), "codex")
    session, cwd = str(uuid4()), tmp_path / "project"
    transcript = ClaudeCodeProvider.project_folder(a.folder, cwd) / f"{session}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type":"user","message":"go"}\n', encoding="utf-8")
    (a.folder / ".credentials.json").write_text('{"token":"A"}', encoding="utf-8")
    run_id = str(uuid4())
    reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name="r", cwd=str(cwd), started_at=1.0, finished_at=2.0, status="failed", provider_session_id=session,
                              subscription=RunSubscription(subscription_id=a.id, limits=SubscriptionLimits())))
    move = lambda target: AgentMoveRequest(id=uuid4(), machine=MachineRef(id=uuid4()), workspace_id=uuid4(), initiator_account=uuid4(), expires_at=datetime.now(UTC),  # noqa: E731
                                           signature="0" * 64, run_id=UUID(run_id), subscription_id=target.id, limits=SubscriptionLimits(weekly_percent=90))
    with pytest.raises(SubscriptionRefused) as other:
        MachineAgents._carry(reg.get_run(run_id), move(codex))
    assert other.value.refusal.reason == "pinned_elsewhere"
    raw = reg.raw_events_path(run_id)
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text('{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{"seven_day":{"utilization":0.97}}}}\n', encoding="utf-8")  # A's reading
    moved = MachineAgents._carry(reg.get_run(run_id), move(b))
    watch = SubscriptionWatch()
    assert watch._appended(raw, moved.usage_from[str(raw)]) == []  # A's 97 % never reaches B's run nor B's usage
    with raw.open("a", encoding="utf-8") as stream:
        stream.write('{"type":"result"}\n')
    assert watch._appended(raw, moved.usage_from[str(raw)]) == ['{"type":"result"}']
    carried = ClaudeCodeProvider.project_folder(b.folder, cwd) / f"{session}.jsonl"
    assert carried.read_bytes() == transcript.read_bytes() and not (b.folder / ".credentials.json").exists()
    assert moved.subscription == RunSubscription(subscription_id=b.id, limits=SubscriptionLimits(weekly_percent=90))
    fence = FenceSpec(working_directory=tmp_path, start=tmp_path, state=tmp_path / "state", providers=("claude", "codex"), homes=RunSubscription.of(
        SubscriptionChoice(provider="claude", subscription_id=a.id, limits=SubscriptionLimits())).homes())
    (fence.state / "claude").mkdir(parents=True)
    (fence.state / "claude" / ".claude.json").write_text('{"oauthAccount": "A"}', encoding="utf-8")
    rehomed = fence.rehomed(moved.subscription.homes())  # a fenced run moved to B never keeps A's account copy
    assert rehomed.homes["claude"] == b.folder and not (fence.state / "claude" / ".claude.json").exists()
    b.observe((UsageWindow(window="weekly", used_percent=95, observed_at=datetime.now(UTC)),))
    with pytest.raises(SubscriptionCapped):
        moved.reopened()
    reopens = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
    monkeypatch.setattr(reg, "limit_resume_at", lambda run: reopens.timestamp() + quota.RESET_SLACK)
    unpinned = moved.model_copy(update={"subscription": None})
    with pytest.raises(UsageLimited) as limited:
        unpinned.reopened()
    assert limited.value.refusal.model_dump(exclude_none=True) == {"reason": "usage_limit", "resets_at": reopens}
    # His editor-launched run's page resumes through `galaius agents resume`: refused the same way, typed on stderr, nothing queued.
    reg.save_run(unpinned)
    monkeypatch.setattr(reg, "get_run", lambda run_id: unpinned)
    with pytest.raises(SystemExit) as stopped:
        agents_resume([run_id])
    said = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert (stopped.value.code, said["subscription_refusal"]["reason"], said["subscription_refusal"]["resets_at"]) == (1, "usage_limit", reopens.isoformat().replace("+00:00", "Z"))


def test_each_login_here_says_its_own_usage_and_a_subscriptions_never_mixes_with_the_default_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner 2026-10-13 « I still can't see the session usage which I am at currently »: each program's default login
    (his editor sessions') is asked its usage by its own CLI, a subscription's in its own folder; signed out: none; a CLI
    that could not answer leaves the last reading; the server hears it once until it moves."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        install_fake_cli(bin_dir, name, Path(__file__).parent / "fixtures" / "agents" / "fake_usage_cli.py")
    environment = {"PATH": os.pathsep.join((str(bin_dir), str(Path(sys.executable).parent))), "HOME": str(Path.home())}
    monkeypatch.setenv("PATH", environment["PATH"])
    subscription = Subscription.create(uuid4(), "claude")
    watch = SubscriptionWatch()

    asyncio.run(watch.ask(environment))
    assert [(window.window, window.used_percent) for window in watch.login_usage("claude")] == [("five_hour", 13.0), ("weekly", 36.0)]
    assert [(window.window, window.used_percent) for window in subscription.usage()] == [("five_hour", 13.0), ("weekly", 80.0)]
    assert watch.login_usage("codex") == ()
    now = datetime.now(UTC)
    report = watch.report(now)
    assert [(login.provider, len(login.windows)) for login in report.logins] == [("claude", 2), ("codex", 0)]  # first report: « none » too
    assert [usage.subscription_id for usage in report.usage] == [subscription.id]
    watch.sent(report, now)
    assert watch.report(now) is None
    (Path.home() / "silent").touch()
    asyncio.run(watch.ask(environment))
    assert [window.used_percent for window in watch.login_usage("claude")] == [13.0, 36.0]
