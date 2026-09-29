"""The web drives agents on a machine only where, and only on what, its owner allowed there."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import asyncio
import hashlib
import hmac
import logging
import time

from interact_core import MACHINE_AGENT_REQUESTS, MachineAgentModel, MachineAgentRequest
from interact.agents import registry as reg
from interact.agents.host import ConversationRefused
from interact.machine_agents import LogRing, MachineAgents, MachineSessions, WebRun, WebRuns, interaction_digest, redact
from interact.fence import FenceSpec
from interact.machines import MachineConfig, MachineRunner
from interact.place_reviews import PlaceReviews


@pytest.fixture
def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("INTERACT_AGENTS_DIR", str(tmp_path / "registry"))
    root = tmp_path / "dev"
    for folder in ("interact-files", "project/src/deep", "project/.secret", "other"):
        (root / folder).mkdir(parents=True)
    (root / "project" / "outside").symlink_to(tmp_path)
    return root


def _config(base: Path, **settings) -> MachineConfig:
    return MachineConfig(server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(), token="t" * 40, permission_ceiling="full_access",
                         working_directory=base, agent_roots=("project",), **settings)


def _agents(base: Path, tmp_path: Path, cli: tuple[str, ...] = ("false",), **settings) -> MachineAgents:
    config = _config(base, **settings)
    return MachineAgents(roots=config.agent_roots_by_name(), permission=config.agent_permission, run_agents=config.run_agents, session="web-test",
                         continue_conversations=config.continue_conversations, answer_approvals=config.answer_approvals,
                         runs=WebRuns(path=tmp_path / "web-runs.json"), environment={"PATH": os.environ["PATH"]}, cli=cli)


def _answer(agents: MachineAgents, request: MachineAgentRequest):
    return asyncio.run(agents.answer(request))


def _request(op: str, config: MachineConfig | None = None, **fields) -> MachineAgentRequest:
    machine, workspace = (config.machine_id, config.workspace_id) if config else (uuid4(), uuid4())
    return MACHINE_AGENT_REQUESTS.validate_python({"id": str(uuid4()), "machine": {"id": str(machine)}, "workspace_id": str(workspace), "op": op, "initiator_account": str(uuid4()),
                                                   "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(), "signature": "0" * 64, **fields})


@pytest.mark.parametrize(("roots", "usable", "refused"), [
    (("project",), ["project"], []),
    (("interact-files",), [], ["interact-files"]),          # the same folder as a sandbox
    ((".",), [], ["."]),                                     # around the sandbox (and the working directory itself)
    (("project/.secret",), [], ["project/.secret"]),         # hidden
    (("../",), [], ["../"]),                                 # above the working directory
])
def test_agent_roots_never_touch_a_sandbox(base: Path, tmp_path: Path, roots, usable, refused) -> None:
    config = _config(base, places={"interact-files": "sandbox"}).model_copy(update={"agent_roots": roots})
    found, refusals = config.usable_agent_roots()
    assert [path.relative_to(base.resolve()).as_posix() for path in found] == usable
    assert list(refusals) == refused


@pytest.mark.parametrize(("path", "allowed"), [("", True), ("src/deep", True), ("src/../../other", False), (".secret", False), ("outside", False), ("missing", False)])
def test_start_folder_stays_beneath_a_root(base: Path, tmp_path: Path, path: str, allowed: bool) -> None:
    agents = _agents(base, tmp_path)
    if allowed:
        assert agents.folder("project", path) == base.resolve() / "project" / path
    else:
        with pytest.raises(PermissionError):
            agents.folder("project", path)


def test_folders_lists_plain_subfolders_only(base: Path, tmp_path: Path) -> None:
    agents = _agents(base, tmp_path)
    assert _answer(agents, _request("folders")).roots == ("project",)
    listing = _answer(agents, _request("folders", root="project"))
    assert [entry.name for entry in listing.entries] == ["src"] and listing.permission == "workspace_write"


def test_only_runs_started_from_the_web_are_reachable(base: Path, tmp_path: Path) -> None:
    agents = _agents(base, tmp_path)
    mine, child, foreign = (str(uuid4()) for _ in range(3))
    for run_id, parent in ((mine, None), (child, mine), (foreign, None)):
        reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name=run_id[:8], cwd=str(base / "project"), parent_run_id=parent, started_at=1.0, exit_code=0))
    agents.runs.add(WebRun(run_id=mine, root="project"))
    listed = _answer(agents, _request("runs")).runs
    assert {str(run.run_id) for run in listed} == {mine, child}
    assert _answer(agents, _request("tail", run_id=child)).cursor == 0
    with pytest.raises(PermissionError, match="not started from the web"):
        _answer(agents, _request("stop", run_id=foreign))


def test_off_switch_and_missing_roots_refuse_everything(base: Path, tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="agents are off"):
        _answer(_agents(base, tmp_path, run_agents=False), _request("folders"))
    empty = _agents(base, tmp_path).model_copy(update={"roots": {}})
    with pytest.raises(PermissionError, match="no agent folders"):
        _answer(empty, _request("folders"))


def test_tail_reads_whole_lines_from_the_cursor(base: Path, tmp_path: Path) -> None:
    agents = _agents(base, tmp_path)
    run_id = str(uuid4())
    reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name="r", cwd=str(base / "project"), started_at=1.0))
    agents.runs.add(WebRun(run_id=run_id, root="project"))
    path = reg.events_path(run_id)
    path.write_bytes(b'{"kind":"text","text":"one"}\n{"kind":"text","te')
    first = _answer(agents, _request("tail", run_id=run_id, cursor=0))
    assert [json.loads(line)["text"] for line in first.lines] == ["one"] and first.cursor == len(b'{"kind":"text","text":"one"}\n')
    with path.open("ab") as handle:
        handle.write(b'xt":"two"}\n')
    assert [json.loads(line)["text"] for line in _answer(agents, _request("tail", run_id=run_id, cursor=first.cursor)).lines] == ["two"]


@pytest.mark.parametrize("brief", ["- fix the header", "--help", "-x"])
def test_a_brief_or_message_starting_with_a_dash_stays_text(base: Path, tmp_path: Path, brief: str) -> None:
    """Everything after "--" is an argument, never an option: the CLI receives the brief intact."""
    seen = tmp_path / "argv.json"
    recorder = ("python3", "-c", f"import json,sys; json.dump(sys.argv[1:], open({str(seen)!r}, 'w')); print({str(uuid4())!r})")
    agents = _agents(base, tmp_path, cli=recorder)
    _answer(agents, _request("start", root="project", role="app-engineer", text=brief))
    argv = json.loads(seen.read_text())
    assert argv[-2:] == ["--", brief] and argv[:2] == ["agents", "spawn"]
    run_id = str(agents.runs.read()[-1].run_id)
    reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name="r", cwd=str(base / "project"), started_at=1.0))
    _answer(agents, _request("send", run_id=run_id, text=brief))
    assert json.loads(seen.read_text()) == ["agents", "send", "--", run_id, brief]


def test_an_action_is_accepted_once(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = MachineRunner(config_path=tmp_path / "machine.json")
    config = _config(base)
    runner.save(config)
    monkeypatch.setattr(MachineRunner, "audit", staticmethod(lambda log, entry: None))
    request = _request("stop", config, run_id=str(uuid4()))
    unsigned = request.model_dump(mode="json", exclude={"signature"})
    key = hashlib.sha256(config.token.get_secret_value().encode()).digest()
    signed = {**unsigned, "signature": hmac.new(key, json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()}

    class Socket:
        sent: list[dict] = []
        async def send(self, text: str) -> None:
            self.sent.append(json.loads(text)["result"])

    socket = Socket()
    asyncio.run(runner._answer_agent_request(socket, config, signed))
    asyncio.run(runner._answer_agent_request(socket, config, signed))
    assert "not started from the web" in socket.sent[0]["error"] and socket.sent[1]["error"] == "agent request was already used"
    asyncio.run(runner._answer_agent_request(socket, config, {**signed, "signature": "0" * 64}))
    assert socket.sent[2]["error"] == "agent request signature is invalid"


def test_start_answers_while_the_agent_it_launched_still_runs(base: Path, tmp_path: Path) -> None:
    """The launcher's child keeps the CLI's descriptors: the start still answers at once."""
    run_id = str(uuid4())
    lingering = ("python3", "-c", f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); print({run_id!r})")
    agents = _agents(base, tmp_path, cli=lingering)
    started = time.monotonic()
    assert str(_answer(agents, _request("start", root="project", role="app-engineer", text="go")).run_id) == run_id
    assert time.monotonic() - started < 10


def test_a_run_that_finished_unwatched_reads_done_not_lost(base: Path, tmp_path: Path) -> None:
    """Its process is gone and no exit was recorded, but its own stream says it finished."""
    agents = _agents(base, tmp_path)
    run_id = str(uuid4())
    reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name="r", cwd=str(base / "project"), started_at=1.0, pid=None))
    reg.events_path(run_id).write_text('{"kind":"text","text":"ok","at":2}\n{"kind":"done","text":"ok","at":3,"final_text":true}\n')
    agents.runs.add(WebRun(run_id=run_id, root="project"))
    assert [run.status for run in _answer(agents, _request("runs")).runs] == ["done"]


@pytest.mark.parametrize("exit_code", [143, -15, 1])
def test_a_stopped_run_stays_stopped_when_its_process_exits(base: Path, exit_code: int) -> None:
    """Stop records "stopped"; the killed turn's reaper then reports its exit (143 through a shell)."""
    run_id = str(uuid4())
    reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name="r", cwd=str(base / "project"), started_at=1.0, pid=None, lifecycle_token="t" * 32))
    assert reg.stop(run_id)
    reg.finish(run_id, exit_code=exit_code, expected_lifecycle_token="t" * 32)
    assert reg.get_run(run_id).status == "stopped"


def test_logs_and_run_output_never_show_credentials() -> None:
    address = "https://" + ":".join(("user", "hunter22")) + "@host/x"  # a password in a URL, built so no scanner mistakes the fixture for one
    line = f"GET {address} Authorization: Bearer abc.def token iwm_0123456789abcdef sk-ABCDEFGHIJKLMNOP and MYSECRETVALUE"
    shown = redact(line, ("MYSECRETVALUE",))
    assert not any(secret in shown for secret in ("hunter22", "abc.def", "0123456789abcdef", "ABCDEFGHIJKLMNOP", "MYSECRETVALUE"))
    ring = LogRing(keep=2)
    for index in range(3):
        logging.getLogger("interact.machines").addHandler(ring)
        logging.getLogger("interact.machines").warning("line %d with iwk_0123456789abcdef", index)
        logging.getLogger("websockets.client").warning("header Authorization: Bearer leaked")
    assert len(ring.lines) == 2 and all("0123456789abcdef" not in line and "leaked" not in line for line in ring.lines)


def _editor(tmp_path: Path, base: Path, cwd: Path, age: float = 3600) -> tuple[Path, UUID]:
    session_id = uuid4()
    folder = tmp_path / "projects" / "-dev-project"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in (
        {"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": "<command-name>clear</command-name>"}},
        {"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": "Fix the header"}},
        {"type": "assistant", "cwd": str(cwd), "message": {"role": "assistant", "content": [{"type": "text", "text": "Header fixed."}]}},
    )) + "\n")
    os.utime(path, (time.time() - age, time.time() - age))
    return path, session_id


def test_editor_conversations_are_listed_only_inside_agent_roots_and_continue_as_a_copy(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agents = _agents(base, tmp_path, continue_conversations=True).model_copy(update={"editor_projects": tmp_path / "projects"})
    _, inside = _editor(tmp_path, base, base / "project" / "src")
    _editor(tmp_path, base, base / "other")
    listed = _answer(agents, _request("sessions")).sessions
    assert [(item.session_id, item.root, item.path, item.title, item.last, item.live) for item in listed] == [(inside, "project", "src", "Fix the header", "Header fixed.", False)]
    turns = []
    monkeypatch.setattr("interact.machine_agents.launch_editor_turn", lambda provider, run, text, *, environment, fork_from=None: turns.append((run.cwd, text, fork_from, run.permission_mode)))
    started = _answer(agents, _request("continue", session_id=str(inside), text="- and the footer"))
    assert turns == [(str(base.resolve() / "project" / "src"), "- and the footer", str(inside), "workspace_write")]
    assert agents.runs.read()[-1].kind == "continued" and agents.runs.read()[-1].run_id == started.run_id


def test_editor_conversations_need_their_own_opt_in(base: Path, tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="continuing your editor conversations"):
        _answer(_agents(base, tmp_path), _request("sessions"))


def test_an_approval_answer_must_match_what_is_waiting(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agents = _agents(base, tmp_path, answer_approvals=True).model_copy(update={"sessions": MachineSessions(base)})
    run_id = str(uuid4())
    reg.save_run(reg.AgentRun(run_id=run_id, kind="conversation", provider="codex", name="s", cwd=str(base / "project"), started_at=1.0, status="waiting"))
    agents.runs.add(WebRun(run_id=run_id, root="project", kind="session"))
    asked = {"id": "i1", "kind": "file_change_approval", "title": "Apply?", "fields": [{"key": "decision", "kind": "choice", "label": "Allow?", "options": ["accept", "decline"]}], "disclosure": ["edit a.py"]}
    reg.events_path(run_id).write_text(json.dumps({"kind": "interaction", "at": 1, "event_id": "i1", "interaction": asked}) + "\n")
    monkeypatch.setattr(reg, "read_events", lambda rid: [reg.AgentEvent.model_validate({"kind": "interaction", "event_id": "i1", "interaction": asked})])
    answered = []

    class Host:
        async def answer(self, rid, interaction, values):
            answered.append((interaction, values))

    async def host(self):
        return Host()
    monkeypatch.setattr(MachineSessions, "host", host)
    pending = _answer(agents, _request("runs")).runs[0].pending
    assert [item.id for item in pending] == ["i1"] and pending[0].digest == interaction_digest(pending[0].model_dump(mode="json"))
    with pytest.raises(PermissionError, match="no longer the one waiting"):
        _answer(agents, _request("answer", run_id=run_id, interaction_id="i1", digest="0" * 64, values={"decision": "decline"}))
    with pytest.raises(PermissionError, match="accepted in the editor"):
        _answer(agents, _request("answer", run_id=run_id, interaction_id="i1", digest=pending[0].digest, values={"decision": "accept"}))
    _answer(agents, _request("answer", run_id=run_id, interaction_id="i1", digest=pending[0].digest, values={"decision": "decline"}))
    assert answered == [("i1", {"decision": "decline"})]


def test_sessions_and_unknown_models_are_refused_without_their_setting(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agents = _agents(base, tmp_path)
    with pytest.raises(PermissionError, match="approvals from the web is off"):
        _answer(agents, _request("start", root="project", kind="session", text="hi"))
    with pytest.raises(PermissionError, match="runs on the model its rule picks"):
        _answer(agents, _request("start", root="project", role="app-engineer", provider="claude", model="claude-sonnet-5", text="go"))


def test_a_message_to_a_session_on_its_turn_waits_for_the_turn_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    delivered, busy = [], [True, True, False]

    class Host:
        async def send(self, run_id, text):
            if busy and busy.pop(0):
                raise ConversationRefused("conflict", "Conversation already has an active turn.")
            delivered.append(text)

    sessions = MachineSessions(tmp_path)
    async def host(self):
        return Host()
    monkeypatch.setattr(MachineSessions, "host", host)

    async def scenario():
        first = await sessions.send("r", "one")
        second = await sessions.send("r", "two")
        task = sessions._deliveries["r"]
        await asyncio.wait_for(task, 20)
        return first, second
    assert asyncio.run(scenario()) == ("queued", "queued") and delivered == ["one", "two"]


def test_a_continued_copy_stops_taking_turns_once_the_opt_in_is_off(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    on = _agents(base, tmp_path, continue_conversations=True).model_copy(update={"editor_projects": tmp_path / "projects"})
    _, editor = _editor(tmp_path, base, base / "project")
    monkeypatch.setattr("interact.machine_agents.launch_editor_turn", lambda *args, **kwargs: None)
    copy = _answer(on, _request("continue", session_id=str(editor), text="go"))
    reg.finish(str(copy.run_id), exit_code=0)
    off = on.model_copy(update={"continue_conversations": False})
    with pytest.raises(PermissionError, match="continuing your editor conversations"):
        _answer(off, _request("send", run_id=str(copy.run_id), text="and more"))


def test_a_failed_first_turn_never_leaves_the_copy_running(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agents = _agents(base, tmp_path, continue_conversations=True).model_copy(update={"editor_projects": tmp_path / "projects"})
    _, editor = _editor(tmp_path, base, base / "project")
    def refuse(*args, **kwargs):
        raise OSError("claude is not installed")
    monkeypatch.setattr("interact.machine_agents.launch_editor_turn", refuse)
    with pytest.raises(OSError):
        _answer(agents, _request("continue", session_id=str(editor), text="go"))
    assert [run.status for run in _answer(agents, _request("runs")).runs] == ["failed"]


def test_questions_of_a_turn_that_ended_are_no_longer_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = {"id": "i1", "kind": "command_approval", "title": "Run?", "fields": [{"key": "decision", "kind": "choice", "label": "Allow?", "options": ["accept", "decline"]}], "disclosure": ["ls"]}
    events = [{"kind": "interaction", "event_id": "i1", "interaction": asked}, {"kind": "cancelled", "event_id": "t1"},
              {"kind": "interaction", "event_id": "i2", "interaction": {**asked, "id": "i2"}}, {"kind": "interaction_resolved", "event_id": "i2:closed:declined"},
              {"kind": "interaction", "event_id": "i3", "interaction": {**asked, "id": "i3"}}]
    monkeypatch.setattr(reg, "read_events", lambda run_id: [reg.AgentEvent.model_validate(event) for event in events])
    assert [item.id for item in MachineAgents.pending("r")] == ["i3"]


def test_a_continued_copy_opens_with_the_editor_history(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agents = _agents(base, tmp_path, continue_conversations=True).model_copy(update={"editor_projects": tmp_path / "projects"})
    _, editor = _editor(tmp_path, base, base / "project")
    monkeypatch.setattr("interact.machine_agents.launch_editor_turn", lambda *args, **kwargs: None)
    copy = _answer(agents, _request("continue", session_id=str(editor), text="go"))
    lines = [json.loads(line) for line in _answer(agents, _request("tail", run_id=str(copy.run_id))).lines]
    assert [(line["kind"], line["text"]) for line in lines][:2] == [("prompt", "Fix the header"), ("text", "Header fixed.")]


def test_codex_conversations_of_the_owner_continue_as_a_session_copy(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agents = _agents(base, tmp_path, continue_conversations=True, answer_approvals=True).model_copy(
        update={"sessions": MachineSessions(base), "editor_projects": tmp_path / "projects"})
    owners, ours = str(uuid4()), str(uuid4())
    forked = []

    class Host:
        async def threads(self, route):
            return [{"id": owners, "preview": "Plan the page", "cwd": str(base / "project"), "originator": "codex_vscode", "updatedAt": 100},
                    {"id": ours, "preview": "Run ls", "cwd": str(base / "project"), "originator": "interact", "updatedAt": 200},
                    {"id": str(uuid4()), "preview": "elsewhere", "cwd": str(base / "other"), "originator": "codex_cli_rs", "updatedAt": 300}]

        async def fork(self, thread_id, prompt, workspace, *, route_id, model):
            forked.append((thread_id, prompt, workspace))
            return reg.AgentRun(run_id=str(uuid4()), provider="codex", name="copy", cwd=str(workspace))

    async def host(self):
        return Host()

    async def route(self):
        return "codex:local_session", ("gpt-6-luna",), ""
    monkeypatch.setattr(MachineSessions, "host", host)
    monkeypatch.setattr(MachineSessions, "route", route)
    listed = _answer(agents, _request("sessions")).sessions
    assert [(str(item.session_id), item.provider, item.title) for item in listed] == [(owners, "codex", "Plan the page")]
    started = _answer(agents, _request("continue", session_id=owners, text="- go on"))
    assert forked == [(owners, "- go on", base.resolve() / "project")] and agents.runs.read()[-1].kind == "session"
    assert agents.runs.read()[-1].run_id == started.run_id


def test_options_for_a_role_ask_the_pc_what_its_rule_picks(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agents = _agents(base, tmp_path)
    monkeypatch.setattr(MachineAgents, "role_models", lambda self, role: (MachineAgentModel(provider="claude", model="claude-sonnet-5"),) if role == "source-validator" else ())
    assert [(item.provider, item.model) for item in _answer(agents, _request("options", role="source-validator")).models] == [("claude", "claude-sonnet-5")]


def test_providers_switch_through_the_cli_the_editor_uses_and_settings_survive_a_bad_answer(base: Path, tmp_path: Path) -> None:
    """The web's provider switch is the editor toggle's own command; a PC whose model read fails
    still answers with its providers, and says why the models are missing."""
    seen = tmp_path / "argv.json"
    recorder = ("python3", "-c", f"import json,sys; json.dump(sys.argv[1:], open({str(seen)!r}, 'w')); print('not json')")
    agents = _agents(base, tmp_path, cli=recorder)
    switched = _answer(agents, _request("provider", provider="codex", active=False))
    assert json.loads(seen.read_text()) == ["agents", "providers", "--name", "codex", "--state", "off"] and switched.detail == "off"
    assert {item.provider for item in switched.providers} == {"claude", "codex"}
    settings = _answer(agents, _request("settings"))
    assert json.loads(seen.read_text()) == ["config", "models", "--json-out"]
    assert settings.tool_models == () and settings.detail.startswith("models not read here") and settings.providers


def test_settings_carry_each_tool_rule_as_the_pc_resolves_it(base: Path, tmp_path: Path) -> None:
    payload = {"ok": True, "roles": [{"role": "image", "criterion": "cap.vlm", "configured": False, "models": [{"provider": "gemini", "model": "gemini/x"}], "reason": ""},
                                     {"role": "audio", "criterion": "cap.nope", "configured": True, "models": [], "reason": "unknown capability"}]}
    agents = _agents(base, tmp_path, cli=("python3", "-c", f"print({json.dumps(json.dumps(payload))})"))
    settings = _answer(agents, _request("settings"))
    assert [(role.role, role.configured, [m.model for m in role.models], role.reason) for role in settings.tool_models] == [
        ("image", False, ["gemini/x"], ""), ("audio", True, [], "unknown capability")]


def test_a_rule_that_cannot_be_read_says_why_instead_of_resolving(monkeypatch: pytest.MonkeyPatch) -> None:
    from interact.cli import app_commands
    from interact.config import Config

    class Settings:  # the runtime proxy's surface, without reading this computer's account
        inner = Config(audio_criteria="cap.not_a_capability")
        def refresh(self): return self
        def __getattr__(self, name): return getattr(self.inner, name)

    monkeypatch.setattr(app_commands, "config", Settings())
    roles = {role["role"]: role for role in app_commands._tool_models()["roles"]}
    assert roles["audio"]["configured"] and roles["audio"]["models"] == [] and roles["audio"]["reason"]
    assert not roles["sovereign"]["configured"] and roles["sovereign"]["criterion"] == "cap.vlm"
    assert set(roles) == {"image", "component", "video", "audio", "sovereign"}


def _fenced_agents(base: Path, tmp_path: Path, cli: tuple[str, ...], **levels) -> MachineAgents:
    config = _config(base, places=levels, fence_agents=True)
    return _agents(base, tmp_path, cli=cli).model_copy(update={
        "places": config.place_map(), "fence_agents": True, "reviews": PlaceReviews(root=tmp_path / "reviews")})


def test_with_the_fence_on_a_web_start_carries_the_spec_its_turns_are_fenced_by(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("interact.machine_agents.available", lambda: (True, ""))
    held = tmp_path / "fence.json"
    recorder = ("python3", "-c", f"import shutil,sys; shutil.copy(sys.argv[sys.argv.index('--fence') + 1], {str(held)!r}); print({str(uuid4())!r})")
    agents = _fenced_agents(base, tmp_path, recorder, project="write", other="read")
    answer = _answer(agents, _request("start", root="project", role="app-engineer", text="tidy"))
    spec = FenceSpec.model_validate_json(held.read_text())
    assert spec.start == base.resolve() / "project" and spec.levels == {"project": "write", "other": "read"} and spec.state.is_relative_to(tmp_path)
    binds = {(bind.target.relative_to(base.resolve()).as_posix(), bind.writable) for bind in spec.build().binds if bind.target.is_relative_to(base.resolve())}
    assert ("other", False) in binds and not any(target.startswith("interact-files") for target, _ in binds)
    assert answer.fenced is True and agents.runs.read()[-1].fenced is True


def test_with_the_fence_on_an_agent_folder_never_opens_on_its_own(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent folder gets no level the owner did not set (it used to open as Write)."""
    monkeypatch.setattr("interact.machine_agents.available", lambda: (True, ""))
    agents = _fenced_agents(base, tmp_path, ("false",), other="read")
    with pytest.raises(PermissionError, match="no level opens project"):
        _answer(agents, _request("start", root="project", role="app-engineer", text="tidy"))


def test_with_the_fence_on_an_agent_never_starts_unfenced(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("interact.machine_agents.available", lambda: (False, "bubblewrap (bwrap) is not installed"))
    agents = _fenced_agents(base, tmp_path, ("false",))
    with pytest.raises(PermissionError, match="cannot be built: bubblewrap"):
        _answer(agents, _request("start", root="project", role="app-engineer", text="tidy"))
    with pytest.raises(PermissionError, match="not fenced yet"):
        _answer(agents, _request("start", root="project", kind="session", text="hello"))
    assert agents.runs.read() == ()


def test_in_a_write_after_review_folder_the_agent_works_in_a_staging_copy(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("interact.machine_agents.available", lambda: (True, ""))
    held = tmp_path / "fence.json"
    run_id = uuid4()
    recorder = ("python3", "-c", f"import shutil,sys; shutil.copy(sys.argv[sys.argv.index('--fence') + 1], {str(held)!r}); print({str(run_id)!r})")
    agents = _fenced_agents(base, tmp_path, recorder, project="write_on_review")
    _answer(agents, _request("start", root="project", role="app-engineer", text="tidy"))
    [bind] = [bind for bind in FenceSpec.model_validate_json(held.read_text()).build().binds if bind.target == base.resolve() / "project"]
    assert bind.writable and bind.source.is_relative_to(tmp_path / "reviews")
    (bind.source / "src" / "new.txt").write_text("from the agent")
    [review] = agents.reviews.list()
    assert review.run_id == run_id and [(item.path, item.change) for item in review.files] == [("src/new.txt", "added")]
    assert not (base / "project" / "src" / "new.txt").exists()
