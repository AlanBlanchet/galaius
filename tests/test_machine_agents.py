"""The web drives agents on a machine only where, and only on what, its owner allowed there."""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import asyncio
import base64
import subprocess
import hashlib
import hmac
import logging
import time

from galaius_core import MACHINE_AGENT_REQUESTS, MachineAgentModel, MachineAgentRequest, MachineAgentSettings, MachineAgentSettingsUpdate, MachineWorkspaceJob, WebLoad
from galaius.agents import agent_queue, messaging
from galaius.agents import registry as reg
from galaius.agents.host import ConversationRefused
from galaius.machine_agents import LogRing, MachineAgents, MachineSessions, WebRun, WebRuns, interaction_digest, redact
from galaius.fence import FenceSpec
from galaius.config import UserConfig
from galaius.machines import MachineConfig, MachineRunner
from galaius.machine_workspaces import KeptJob, MachineWorkspaces, WorkspaceJobs
from galaius.workspace_git import CloneFailure, Git
from galaius.workspace_copy import WorkspaceTransfers
from galaius.agents.providers import PROJECT_SETTINGS_OFF, ClaudeCodeProvider
from galaius.place_reviews import PlaceReviews
from galaius.private_files import PRIVATE_FILES
from galaius.project_secrets import MARKER, ProjectEnv
from galaius_core.sealing import SecretsSeal


@pytest.fixture
def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(tmp_path / "registry"))
    root = tmp_path / "dev"
    for folder in ("interact-files", "project/src/deep", "project/.secret", "other"):
        (root / folder).mkdir(parents=True)
    (root / "project" / "outside").symlink_to(tmp_path)
    return root


def _config(base: Path, **settings) -> MachineConfig:
    return MachineConfig(server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(), token="t" * 40, permission_ceiling="full_access",
                         working_directory=base, **{"agent_roots": ("project",), "run_agents": True, **settings})


def _agents(base: Path, tmp_path: Path, cli: tuple[str, ...] = ("false",), **settings) -> MachineAgents:
    config = _config(base, **settings)
    return MachineAgents(roots=config.agent_roots_by_name(), permission=config.agent_permission, run_agents=config.run_agents, session="web-test",
                         continue_conversations=config.continue_conversations, answer_approvals=config.answer_approvals,
                         runs=WebRuns(path=tmp_path / "web-runs.json"), environment={"PATH": os.environ["PATH"]}, cli=cli,
                         workspaces=MachineWorkspaces(roots=config.agent_roots_by_name(), origins=config.clone_origins, jobs=WorkspaceJobs(path=tmp_path / "workspaces.json"),
                                                      environment={"PATH": os.environ["PATH"]}, working_directory=base,
                                                      transfers=WorkspaceTransfers(endpoint=lambda path: f"http://server{path}", headers={})))


def _answer(agents: MachineAgents, request: MachineAgentRequest):
    return asyncio.run(agents.answer(request))


def _request(op: str, config: MachineConfig | None = None, **fields) -> MachineAgentRequest:
    machine, workspace = (config.machine_id, config.workspace_id) if config else (uuid4(), uuid4())
    return MACHINE_AGENT_REQUESTS.validate_python({"id": str(uuid4()), "machine": {"id": str(machine)}, "workspace_id": str(workspace), "op": op, "initiator_account": str(uuid4()),
                                                   "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(), "signature": "0" * 64, **fields})


def _launches(monkeypatch: pytest.MonkeyPatch, run_id: UUID | None = None) -> list[dict]:
    """Every agent the web starts, as handed to the launcher (no agent CLI runs)."""
    seen: list[dict] = []

    async def launch(provider, task, **options):
        seen.append({"provider": provider, "task": task, "session": reg.resolve_session_id(), **options})
        return SimpleNamespace(run_id=str(run_id or uuid4()))

    monkeypatch.setattr("galaius.machine_agents.run_agent", launch)
    return seen


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


def test_runs_say_which_ranked_candidates_were_passed_over_and_until_when(base: Path, tmp_path: Path) -> None:
    """A run that fell through to Codex because Claude was exhausted carries that fact to the web, with
    the instant Claude reopens — what lets the conversation say « Claude indisponible jusqu'à 22:01,
    lancé avec Codex » instead of showing Codex as the choice."""
    agents = _agents(base, tmp_path)
    run_id = str(uuid4())
    claude = reg.LaunchCandidate(provider="claude", model="claude-opus-5-5", rank=0)
    reg.save_run(reg.AgentRun(run_id=run_id, provider="codex", model="gpt-6-astra", name="r", cwd=str(base / "project"), started_at=1.0,
                              skipped=(reg.SkippedCandidate(candidate=claude, reason="quota_exceeded", message="exhausted", until=1791000000.0),
                                       reg.SkippedCandidate(candidate=reg.LaunchCandidate(provider="claude", model="claude-fable-5-1", rank=1), reason="cli_missing"))))
    agents.runs.add(WebRun(run_id=run_id, root="project"))
    listed = _answer(agents, _request("runs")).runs[0]
    assert listed.provider == "codex"
    assert [(item.provider, item.model, item.reason, item.until) for item in listed.passed_over] == [
        ("claude", "claude-opus-5-5", "quota_exceeded", 1791000000.0), ("claude", "claude-fable-5-1", "cli_missing", None)]


def test_off_switch_and_missing_roots_refuse_everything_but_reading_and_stopping(base: Path, tmp_path: Path) -> None:
    off = _agents(base, tmp_path, run_agents=False)
    with pytest.raises(PermissionError, match="agents are off"):
        _answer(off, _request("folders"))
    assert _answer(off, _request("runs")).runs == ()  # what the web started stays readable and stoppable
    with pytest.raises(PermissionError, match="not started from the web"):
        _answer(off, _request("stop", run_id=str(uuid4())))
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


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


@pytest.mark.parametrize(("case", "served"), [
    pytest.param("named png", "image/png", marks=pytest.mark.xfail(
        sys.platform == "win32", strict=True, raises=PermissionError,
        reason="galaius_core.MEDIA_PATH names only `/` and `~/` image paths: a Windows step's `C:\\…` path is never named")),
    ("named under ~", "image/png"), ("never named", None), ("named link", None), ("named text as .png", None),
    ("named but too large", None), ("named by another run", None),
    pytest.param("named fifo", None, marks=pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="a FIFO is a POSIX file type; no Windows path names one")),
])
def test_a_run_image_is_served_only_when_its_own_step_names_a_plain_image(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, served: str | None) -> None:
    """Deny by default: the web asks by key, never by path; the PC serves only an image file a tool step
    of THAT web run wrote down — with agents switched off too (reading what ran stays possible)."""
    import galaius.machine_agents as machine_agents
    from galaius_core import media_key
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(machine_agents, "MACHINE_AGENT_MEDIA", 64)
    agents = _agents(base, tmp_path, run_agents=False)
    mine, foreign = str(uuid4()), str(uuid4())
    for run_id in (mine, foreign):
        reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name="r", cwd=str(base / "project"), started_at=1.0))
    agents.runs.add(WebRun(run_id=mine, root="project"))
    image = tmp_path / "shot.png"
    image.write_bytes(PNG + (b"\0" * 64 if case == "named but too large" else b"") if case != "named text as .png" else b"not an image")
    if case == "named link":
        (tmp_path / "link.png").symlink_to(image)
    if case == "named fifo":
        os.mkfifo(tmp_path / "pipe.png")  # a FIFO would block a plain open for ever
    written = {"named under ~": "~/shot.png", "named link": str(tmp_path / "link.png"), "named fifo": str(tmp_path / "pipe.png")}.get(case, str(image))
    step = json.dumps({"kind": "tool", "tool": "Read", "tool_input": json.dumps({"file_path": written})}) + "\n"
    reg.events_path(foreign if case == "named by another run" else mine).write_text(step if case != "never named" else "")
    request = _request("media", run_id=mine, name=media_key(written))
    if served is None:
        with pytest.raises(PermissionError):
            _answer(agents, request)
        return
    media = _answer(agents, request).media
    assert media.content_type == served and base64.b64decode(media.data) == PNG


def test_a_message_is_answered_once_durably_queued_and_its_dispatcher_gets_the_machine_environment(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No CLI child and no wait for the resumed turn: the answer leaves as soon as the message is in
    the run's durable queue, and the dispatcher resuming it runs in the scrubbed machine environment."""
    agents = _agents(base, tmp_path)
    run_id = str(uuid4())
    reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", agent="tester", name="r", cwd=str(base / "project"), started_at=1.0, exit_code=0))
    agents.runs.add(WebRun(run_id=run_id, root="project"))
    monkeypatch.setattr(messaging, "policy_for_continuation", lambda run, provider, environment: (None, "criterion", "model", "high"))
    started: dict = {}

    class Dispatcher:
        def __init__(self, argv, **options) -> None:
            started.update(options, argv=argv)
            self.pid = os.getpid()

    monkeypatch.setattr(agent_queue.subprocess, "Popen", Dispatcher)
    answer = _answer(agents, _request("send", run_id=run_id, text="- and one more thing"))
    assert answer.detail.startswith("Queued for r") and started["env"] == agents.environment
    assert started["argv"][-3:-1] == ["--dispatch", run_id] and started["start_new_session"]
    assert [item.state for item in agent_queue.items(run_id)] == ["pending"]
    assert reg.message_for(run_id, agent_queue.items(run_id)[0].message_id).text == "- and one more thing"


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
        logging.getLogger("galaius.machines").addHandler(ring)
        logging.getLogger("galaius.machines").warning("line %d with iwk_0123456789abcdef", index)
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
    monkeypatch.setattr("galaius.machine_agents.launch_editor_turn", lambda provider, run, text, *, environment, fork_from=None: turns.append((run.cwd, text, fork_from, run.permission_mode)))
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
    monkeypatch.setattr("galaius.machine_agents.launch_editor_turn", lambda *args, **kwargs: None)
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
    monkeypatch.setattr("galaius.machine_agents.launch_editor_turn", refuse)
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
    monkeypatch.setattr("galaius.machine_agents.launch_editor_turn", lambda *args, **kwargs: None)
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
                    {"id": ours, "preview": "Run ls", "cwd": str(base / "project"), "originator": "galaius", "updatedAt": 200},
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
    from galaius.cli import app_commands
    from galaius.config import Config

    class Settings:  # the runtime proxy's surface, without reading this computer's account
        inner = Config(audio_criteria="cap.not_a_capability")
        def refresh(self): return self
        def __getattr__(self, name): return getattr(self.inner, name)

    monkeypatch.setattr(app_commands, "config", Settings())
    roles = {role["role"]: role for role in app_commands._tool_models()["roles"]}
    assert roles["audio"]["configured"] and roles["audio"]["models"] == [] and roles["audio"]["reason"]
    assert not roles["sovereign"]["configured"] and roles["sovereign"]["criterion"] == "cap.vlm"
    assert set(roles) == {"image", "component", "video", "audio", "sovereign"}


def _signed_settings(config: MachineConfig, version: int, based_on: int, **settings) -> dict:
    update = MachineAgentSettingsUpdate(machine={"id": config.machine_id}, workspace_id=config.workspace_id, version=version, based_on=based_on,
                                        settings=settings, changed_by=uuid4(), changed_at=datetime.now(UTC), signature="0" * 64)
    return {**update.model_dump(mode="json"), "signature": MachineRunner.signature(config.token.get_secret_value(), update)}


def test_web_settings_apply_once_from_the_revision_the_page_read(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A web version applies only newer than the last, built from this PC's current revision, signed
    for this machine, below full access, while web control is on here; every outcome is reported."""
    runner = MachineRunner(config_path=tmp_path / "machine.json")
    config = _config(base, run_agents=False, agent_roots=())
    runner.save(config)
    monkeypatch.setattr(MachineRunner, "audit", staticmethod(lambda log, entry: None))

    class Socket:
        sent: list[dict] = []
        async def send(self, text: str) -> None:
            self.sent.append(json.loads(text)["state"])

    def apply(payload: dict) -> dict:
        asyncio.run(runner._apply_web_settings(Socket(), config, payload))
        return Socket.sent[-1]

    first = _signed_settings(config, 1, 0, run_agents=True, agent_roots=("project", "workspaces/new"), agent_permission="read_only")
    state = apply(first)
    assert (base / "workspaces" / "new").is_dir()  # a folder the web names is created on the PC
    runner.update(lambda current: current.model_copy(update={"agent_roots": ("project",)}))
    assert (state["revision"], state["version"], state["detail"], state["settings"]["agent_roots"]) == (1, 1, "", ["project", "workspaces/new"])
    assert apply(first)["detail"].startswith("version 1 is not newer") and Socket.sent[-1]["refused_version"] == 1
    runner.update(lambda current: current.model_copy(update={"agent_permission": "workspace_write"}))  # a change made on the PC
    assert "changed on this computer" in apply(_signed_settings(config, 2, 1, run_agents=True, agent_roots=("project", "other")))["detail"]
    full = _signed_settings(config, 2, 2, run_agents=True)
    full["settings"]["agent_permission"] = "full_access"
    sent = len(Socket.sent)
    asyncio.run(runner._apply_web_settings(Socket(), config, full))  # not even a valid web version: dropped
    assert len(Socket.sent) == sent and runner.load().agent_permission == "workspace_write"
    assert "signature" in apply({**_signed_settings(config, 2, 2), "signature": "0" * 64})["detail"]
    # Taking power away applies even when built from an older revision (the web's stop).
    assert apply(_signed_settings(config, 2, 1, run_agents=False, agent_roots=("project",)))["detail"] == ""
    runner.update(lambda current: current.model_copy(update={"remote_settings": False}))
    assert "web control" in apply(_signed_settings(config, 3, 3))["detail"]
    held = runner.load()
    assert (held.run_agents, held.agent_roots, held.agent_permission, held.settings_revision, held.web_settings_version) == (False, ("project",), "workspace_write", 4, 2)


def test_a_start_asks_less_never_more_and_a_cloned_workspace_loads_no_project_settings(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Launched in this process, in the scrubbed machine environment with this PC's persisted
    settings applied (as the CLI child had it), never this process's own: not even the identity of
    a run whose shell started the runner."""
    seen = _launches(monkeypatch)
    UserConfig.PATH.parent.mkdir(parents=True, exist_ok=True)
    UserConfig.PATH.write_text("OLLAMA_HOST=http://gpu-box:11434\n")
    runner_parent = str(uuid4())
    reg.save_run(reg.AgentRun(run_id=runner_parent, provider="claude", name="p", cwd=str(base), started_at=1.0, session_id="another-conversation"))
    monkeypatch.setenv("GALAIUS_PARENT_RUN_ID", runner_parent)
    agents = _agents(base, tmp_path, agent_permission="workspace_write")
    agents.workspaces.jobs.put(MachineWorkspaceJob(id=uuid4(), root="project", name="src", origin="github.com/o/r", state="ready", started_at=datetime.now(UTC)))
    for asked, path, scope, untrusted in ((None, "", "workspace_write", None), ("read_only", "src/deep", "read_only", "1"), ("full_access", "", "workspace_write", None)):
        _answer(agents, _request("start", root="project", path=path, role="tester", text="- go", **({"permission": asked} if asked else {})))
        launched = seen[-1]
        assert (launched["permission_mode"], launched["environment"].get(PROJECT_SETTINGS_OFF), launched["task"]) == (scope, untrusted, "- go")
        assert launched["environment"].keys() <= {"PATH", PROJECT_SETTINGS_OFF, "OLLAMA_HOST"} and launched["environment"]["OLLAMA_HOST"] == "http://gpu-box:11434"
        assert launched["session"] == "web-test" and launched["quota_window"] == 4.0


def test_a_cloned_workspace_start_tells_the_agent_cli_to_load_no_folder_settings() -> None:
    """The switch travels in the run's own environment, not this process's: a runner launching
    in-process for an untrusted clone still starts its CLI with the folder's settings off."""
    assert PROJECT_SETTINGS_OFF not in os.environ
    options = dict(cwd=".", model="m", mcp_config=None, run_id=str(uuid4()))
    trusted = ClaudeCodeProvider().command("go", **options, environment={})
    untrusted = ClaudeCodeProvider().command("go", **options, environment={PROJECT_SETTINGS_OFF: "1"})
    assert [flag for flag in untrusted if flag not in trusted] == list(ClaudeCodeProvider.folder_settings_off)


@pytest.mark.parametrize(("url", "name", "said"), [
    ("https://github.com/other/repo", None, "not among the repositories"),
    ("https://127.0.0.1/owner/repo", "fresh", "local or private address"),
])
def test_a_clone_is_refused_before_git_runs(base: Path, tmp_path: Path, url: str, name: str | None, said: str) -> None:
    agents = _agents(base, tmp_path, clone_origins=("github.com/owner/*", "127.0.0.1/owner/repo"))
    with pytest.raises(PermissionError, match=said):
        _answer(agents, _request("workspace_prepare", root="project", url=url, **({"name": name} if name else {})))
    assert agents.workspaces.jobs.read() == ()


@pytest.mark.parametrize(("output", "code", "said"), [
    ("Host key verification failed.\nfatal: Could not read from remote repository.", "host_unknown", "never connected to github.com over SSH"),
    ("git@github.com: Permission denied (publickey).", "clone_auth_refused", "refused this PC's SSH key"),
    ("fatal: could not read Username for 'https://github.com': terminal prompts disabled", "clone_auth_refused", "no saved git sign-in for github.com"),
    ("remote: Repository not found.\nfatal: repository 'https://github.com/o/r/' not found", "repository_not_found", "does not exist"),
    ("fatal: transport 'file' not allowed", "failed", "transport other than https or ssh"),
    ("fatal: something new", "failed", "git clone failed: fatal: something new"),
])
def test_a_failed_clone_says_why_as_a_code_and_in_plain_words(output: str, code: str, said: str) -> None:
    failure = CloneFailure.explain(output, "github.com")
    assert failure.code == code and said in str(failure)


def test_the_submodule_walk_never_leaves_the_clone(base: Path, tmp_path: Path) -> None:
    """Only gitlinks git records are followed: a `.gitmodules` path pointing outside touches nothing."""
    outside, clone = tmp_path / "outside", base / "project" / "clone"
    for repo in (outside, clone):
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
    module = '[submodule "x"]\n\tpath = {path}\n\turl = https://github.com/owner/x.git\n'
    (clone / ".gitmodules").write_text(module.format(path="../../../outside"))
    # The outside repo looks like one with a submodule of its own: a walk that entered it would init it.
    (outside / ".gitmodules").write_text(module.format(path="x"))
    subprocess.run(["git", "-C", str(outside), "update-index", "--add", "--cacheinfo", f"160000,{'a' * 40},x"], check=True)
    before = (outside / ".git" / "config").read_bytes()
    workspaces = _agents(base, tmp_path, clone_origins=("github.com/owner/*",)).workspaces
    git = Git(options=MachineWorkspaces.GIT_OPTIONS, environment={"PATH": os.environ["PATH"], "HOME": str(tmp_path)}, host="github.com", deadline=time.monotonic() + 30)
    workspaces._submodules(git, clone, clone)
    assert (outside / ".git" / "config").read_bytes() == before


@pytest.mark.parametrize(("where", "root", "path"), [("project/src/aino", "project", "src/aino"), ("other/aino", "other/aino", "")])
def test_an_existing_checkout_is_used_before_any_clone(base: Path, tmp_path: Path, where: str, root: str, path: str) -> None:
    """The owner already has the project on that PC: found by its origin remote, inside an agent root
    as is, elsewhere registered as one - never cloned again, and its own agent settings load."""
    checkout = base / where
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", "git@github.com:Owner/Aino.git"], check=True)
    registered: list[str] = []
    agents = _agents(base, tmp_path, clone_origins=("github.com/owner/*",))
    workspaces = agents.workspaces.model_copy(update={"working_directory": base, "register_root": lambda name: registered.append(name) or True})
    job = _answer(agents.model_copy(update={"workspaces": workspaces}), _request("workspace_prepare", root="project", url="https://github.com/owner/aino.git")).workspaces[0]
    assert (job.state, job.found, job.root, job.name) == ("ready", True, root, path)
    assert registered == ([] if root == "project" else [root]) and not workspaces.prepared(checkout)


def _checkout(folder: Path, origin: str = "git@github.com:owner/aino.git") -> Path:
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    subprocess.run(["git", "-C", str(folder), "remote", "add", "origin", origin], check=True)
    return folder


def test_a_start_with_project_secrets_writes_them_as_the_checkouts_dotenv_only(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opened with this PC's key, written as the .env of the project's own checkout (marker, quoted,
    0600, locally git-excluded), never as the run's environment; values redacted from the PC's logs."""
    checkout = _checkout(base / "project" / "aino")
    seen = _launches(monkeypatch)
    config = _config(base)
    agents = _agents(base, tmp_path).model_copy(update={"seal": SecretsSeal.for_token(config.token.get_secret_value()), "logs": LogRing()})
    project, request_id = uuid4(), uuid4()
    sealed = SecretsSeal.for_token(config.token.get_secret_value()).seal({"DATABASE_URL": "not-a-real-secret-value-for-aino"}, request=request_id, project=project, origin="github.com/owner/aino")
    start = _request("start", root="project", path="aino", role="tester", text="go", project=str(project), with_secrets=True, secrets=sealed.model_dump(mode="json"))
    answer = _answer(agents, start.model_copy(update={"id": request_id}))
    lines = (checkout / ".env").read_text().splitlines()
    assert lines[0].startswith(MARKER) and lines[1] == "DATABASE_URL='not-a-real-secret-value-for-aino'" and answer.detail.startswith("1 project secrets")
    PRIVATE_FILES.check(checkout / ".env")
    assert ".env" in (checkout / ".git" / "info" / "exclude").read_text().splitlines()
    assert "DATABASE_URL" not in seen[-1]["environment"] and "not-a-real-secret-value-for-aino" in agents.logs.secrets


@pytest.mark.parametrize("trap", ["owner", "link", "tracked", "other origin"])
def test_project_secrets_are_never_written_where_they_could_leak_or_clobber(base: Path, tmp_path: Path, trap: str) -> None:
    checkout = _checkout(base / "project" / "aino", "git@github.com:someone/else.git" if trap == "other origin" else "git@github.com:owner/aino.git")
    if trap == "owner":
        (checkout / ".env").write_text("MINE=1\n")
    elif trap == "link":
        (checkout / ".env").symlink_to(tmp_path / "elsewhere")
    elif trap == "tracked":
        (checkout / ".env").write_text(f"{MARKER}\n")
        subprocess.run(["git", "-C", str(checkout), "add", ".env"], check=True)
    with pytest.raises(PermissionError):
        ProjectEnv.locate(checkout, base / "project", "github.com/owner/aino").write({"API_KEY": "secret-value"}, project=uuid4())
    assert not (tmp_path / "elsewhere").exists() and (trap != "owner" or (checkout / ".env").read_text() == "MINE=1\n")



def test_a_worktree_checkout_takes_the_secrets_and_an_empty_vault_clears_them(base: Path, tmp_path: Path) -> None:
    """A git worktree (its .git a file) is a checkout of its repository; an empty set rewrites the
    .env with no secret left in it."""
    main = _checkout(tmp_path / "main")
    subprocess.run(["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "start"], check=True)
    worktree = base / "project" / "aino-wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(worktree)], check=True)
    env = ProjectEnv.locate(worktree, base / "project", "github.com/owner/aino")
    assert env.checkout == worktree.resolve()
    env.write({"API_KEY": "secret-value-one"}, project=uuid4())
    env.write({}, project=uuid4())
    assert [line for line in (worktree / ".env").read_text().splitlines() if not line.startswith("#")] == []
    assert ".env" in (main / ".git" / "info" / "exclude").read_text().splitlines()

def _fenced_agents(base: Path, tmp_path: Path, cli: tuple[str, ...], **levels) -> MachineAgents:
    config = _config(base, places=levels, fence_agents=True)
    return _agents(base, tmp_path, cli=cli).model_copy(update={
        "places": config.place_map(), "fence_agents": True, "reviews": PlaceReviews(root=tmp_path / "reviews")})


def test_with_the_fence_on_a_web_start_carries_the_spec_its_turns_are_fenced_by(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("galaius.machine_agents.available", lambda: (True, ""))
    seen = _launches(monkeypatch)
    agents = _fenced_agents(base, tmp_path, ("false",), project="write", other="read")
    answer = _answer(agents, _request("start", root="project", role="app-engineer", text="tidy"))
    spec = seen[-1]["fence"]
    assert spec.start == base.resolve() / "project" and spec.levels == {"project": "write", "other": "read"} and spec.state.is_relative_to(tmp_path)
    binds = {(bind.target.relative_to(base.resolve()).as_posix(), bind.writable) for bind in spec.build().binds if bind.target.is_relative_to(base.resolve())}
    assert ("other", False) in binds and not any(target.startswith("interact-files") for target, _ in binds)
    assert answer.fenced is True and agents.runs.read()[-1].fenced is True


def test_with_the_fence_on_an_agent_folder_never_opens_on_its_own(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent folder gets no level the owner did not set (it used to open as Write)."""
    monkeypatch.setattr("galaius.machine_agents.available", lambda: (True, ""))
    agents = _fenced_agents(base, tmp_path, ("false",), other="read")
    with pytest.raises(PermissionError, match="no level opens project"):
        _answer(agents, _request("start", root="project", role="app-engineer", text="tidy"))


def test_with_the_fence_on_an_agent_never_starts_unfenced(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("galaius.machine_agents.available", lambda: (False, "bubblewrap (bwrap) is not installed"))
    agents = _fenced_agents(base, tmp_path, ("false",))
    with pytest.raises(PermissionError, match="cannot be built: bubblewrap"):
        _answer(agents, _request("start", root="project", role="app-engineer", text="tidy"))
    with pytest.raises(PermissionError, match="not fenced yet"):
        _answer(agents, _request("start", root="project", kind="session", text="hello"))
    assert agents.runs.read() == ()


def test_in_a_write_after_review_folder_the_agent_works_in_a_staging_copy(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("galaius.machine_agents.available", lambda: (True, ""))
    run_id = uuid4()
    seen = _launches(monkeypatch, run_id)
    agents = _fenced_agents(base, tmp_path, ("false",), project="write_on_review")
    _answer(agents, _request("start", root="project", role="app-engineer", text="tidy"))
    [bind] = [bind for bind in seen[-1]["fence"].build().binds if bind.target == base.resolve() / "project"]
    assert bind.writable and bind.source.is_relative_to(tmp_path / "reviews")
    (bind.source / "src" / "new.txt").write_text("from the agent")
    [review] = agents.reviews.list()
    assert review.run_id == run_id and [(item.path, item.change) for item in review.files] == [("src/new.txt", "added")]
    assert not (base / "project" / "src" / "new.txt").exists()


@pytest.mark.parametrize(("child", "working"), [({"pid": os.getpid()}, True), ({"pid": os.getpid(), "status": "starting"}, True), ({"exit_code": 0}, False), ({"pid": os.getpid(), "exit_code": 0}, False)])
def test_web_runs_are_working_while_a_run_they_launched_still_runs(base: Path, tmp_path: Path, child: dict, working: bool) -> None:
    """What a machine's beat says as `busy`: a web run, or a run it launched, still working."""
    runs = WebRuns(path=tmp_path / "web-runs.json")
    root, launched = str(uuid4()), str(uuid4())
    reg.save_run(reg.AgentRun(run_id=root, provider="claude", name="r", cwd=str(base / "project"), started_at=1.0, exit_code=0))
    reg.save_run(reg.AgentRun(run_id=launched, provider="claude", name="c", cwd=str(base / "project"), parent_run_id=root, started_at=1.0, **child))
    assert not runs.working()
    runs.add(WebRun(run_id=root, root="project"))
    assert runs.working() is working


def test_the_web_cap_counts_conversations_never_the_agents_they_launched(base: Path, tmp_path: Path) -> None:
    """Owner 2026-10-10 (« this PC had refused the message »): two conversations from the web and the agents they
    launched refused his third; each conversation counts once, one waiting for him not at all."""
    agents = _agents(base, tmp_path)

    def run(**fields) -> str:
        run_id = str(uuid4())
        reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name="r", cwd=str(base / "project"), started_at=1.0, pid=os.getpid(), **fields))
        return run_id

    def started(**fields) -> str:
        agents.runs.add(WebRun(run_id=(conversation := run(**fields)), root="project"))
        return conversation

    first, second = started(), started()
    started(kind="conversation", status="waiting")
    for parent in (first, first, first, second, second):
        run(parent_run_id=run(parent_run_id=parent))  # an agent it launched, and the one that agent launched
    run(parent_run_id=str(uuid4()), root_run_id=second)  # its parent's record gone: still the second conversation's
    start = _request("start", root="project", role="main", text="go")
    agents._prepare_start(start)
    assert agents.runs.live() == 2
    run(parent_run_id=started(kind="conversation", status="waiting"))  # idle itself, one of its agents working: it counts
    started()
    with pytest.raises(PermissionError, match=r"^4 conversations started from the web are already working on this computer \(at most 4\)"):
        agents._prepare_start(start)
    listed = _answer(agents, _request("runs"))
    assert listed.web == WebLoad(working=4, limit=4)  # what the web's Auto reads to pass over a full PC


class TransferServer:
    """The server's transfer routes as the PCs reach them over HTTP (`WorkspaceArchive.part_route`):
    parts kept by (transfer, index), each PUT's digest header checked, the ending POST kept."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx
        self.parts: dict[tuple[str, int], bytes] = {}
        self.ended: dict[str, dict] = {}

        def request(method: str, url: str, headers: dict, timeout: float, content: bytes | None = None, json: dict | None = None) -> httpx.Response:
            transfer, _, index = url.removeprefix("http://server/v1/machine/transfers/").partition("/")
            if method == "PUT":
                assert headers["x-galaius-digest"] == hashlib.sha256(content).hexdigest()
                self.parts[(transfer, int(index))] = content
            elif method == "POST":
                self.ended[transfer] = json
            elif (transfer, int(index)) not in self.parts:
                return httpx.Response(404, request=httpx.Request(method, url))
            return httpx.Response(200, content=self.parts.get((transfer, int(index or 0)), b""), request=httpx.Request(method, url))

        monkeypatch.setattr(httpx, "request", request)

    def outcome(self, transfer: UUID) -> dict:
        for _ in range(200):
            if str(transfer) in self.ended:
                return self.ended[str(transfer)]["outcome"]
            time.sleep(0.05)
        raise AssertionError("the upload never ended")

    def serve(self, transfer: UUID, archive: bytes) -> dict:
        """`archive` as uploaded parts; its `WorkspaceArchive`."""
        self.parts[(str(transfer), 0)] = archive
        return {"transfer": str(transfer), "size": len(archive), "digest": hashlib.sha256(archive).hexdigest(), "files": 8}


def _settled(workspaces: MachineWorkspaces, job_id: UUID) -> MachineWorkspaceJob:
    for _ in range(200):
        job = next(item for item in workspaces.jobs.read() if item.id == job_id)
        if job.state != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("the job never ended")


@pytest.mark.usefixtures("directory_backend")
def test_a_project_copied_to_another_pc_arrives_whole_under_a_free_name(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PC-A sends its checkout's tracked files (hidden tracked ones too, never a link, a key or a name
    Windows refuses, each left-out file listed); PC-B writes them beside an existing folder of the same
    name, under `aino-2`, as a folder whose own agent settings never load. An empty project folder
    takes the next free name the same way."""
    server = TransferServer(monkeypatch)
    source = _checkout(base / "project" / "aino", origin="https://github.com/owner/aino.git")
    for name, content in {"README.md": "# aino", "src/app.py": "print(1)", ".gitignore": "*.log", "id_rsa": "key", "aux.c": "int x;", ".env": "TOKEN=x",
                          ".npmrc": "//r/:_authToken=x", "sub/.env.production": "TOKEN=y"}.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(content)
    (source / "link").symlink_to(tmp_path)
    (source / "notes.log").write_text("untracked")
    subprocess.run(["git", "-C", str(source), "add", "-A", "-f", "."], check=True)
    pc_a = _agents(base, tmp_path)
    measured = _answer(pc_a, _request("workspace_pack", root="project", path="aino")).pack
    assert (measured.files, measured.tracked, measured.origin.url) == (4, True, "https://github.com/owner/aino.git")  # README, app.py, .gitignore, notes.log (force-added)
    assert {skip.reason: set(skip.examples) for skip in measured.skipped} == {"link": {"link"}, "credential_store": {".env", ".npmrc", "id_rsa", "sub/.env.production"}, "name": {"aux.c"}}
    transfer = uuid4()
    _answer(pc_a, _request("workspace_pack", root="project", path="aino", transfer=str(transfer)))
    archive = server.outcome(transfer)

    pc_b_base = tmp_path / "pcb"
    (pc_b_base / "galaius" / "projets" / "aino").mkdir(parents=True)  # an empty folder already there is never replaced
    (tmp_path / "b").mkdir()
    pc_b = _agents(pc_b_base, tmp_path / "b", agent_roots=("galaius/projets",))
    job = _answer(pc_b, _request("workspace_create", root="galaius/projets", name="aino", copy_of=archive)).workspaces[0]
    done = _settled(pc_b.workspaces, job.id)
    landed = pc_b_base / "galaius" / "projets" / "aino-2"
    assert (done.state, done.name, done.source, done.done) == ("ready", "aino-2", "copy", archive["size"])
    assert {path.relative_to(landed).as_posix(): path.read_text() for path in landed.rglob("*") if path.is_file()} == {
        "README.md": "# aino", "src/app.py": "print(1)", ".gitignore": "*.log", "notes.log": "untracked"}
    assert pc_b.workspaces.prepared(landed) and (landed.parent / "aino").is_dir() and not any(path.name.endswith(".partial") for path in landed.parent.iterdir())
    empty = [_answer(pc_b, _request("workspace_create", root="galaius/projets", name="notes")).workspaces[0].name for _ in range(2)]
    assert empty == ["notes", "notes-2"] and (pc_b_base / "galaius" / "projets" / "notes-2").is_dir() and not pc_b.workspaces.prepared(pc_b_base / "galaius" / "projets" / "notes")


def _tar(*members: tuple[str, bytes | None, bytes]) -> bytes:
    """A gzip'd tar of (name, link target or None, content) members."""
    import io
    import tarfile
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as packed:
        for name, target, content in members:
            member = tarfile.TarInfo(name)
            if target is not None:
                member.type, member.linkname = tarfile.SYMTYPE, target.decode()
                packed.addfile(member)
            else:
                member.size = len(content)
                packed.addfile(member, io.BytesIO(content))
    return stream.getvalue()


@pytest.mark.usefixtures("directory_backend")
@pytest.mark.parametrize("members", [
    (("/etc/cron.d/x", None, b"x"),), (("../x", None, b"x"),), (("a/../../x", None, b"x"),), ((".git/hooks/post-checkout", None, b"#!/bin/sh"),),
    (("CON.txt", None, b"x"),), (("a:b", None, b"x"),), (("dir/trailing.", None, b"x"),), (("C:\\x", None, b"x"),),
    (("ok.txt", None, b"x"), ("link", b"/home", b"")), (("Read.md", None, b"x"), ("read.MD", None, b"y")), (("GIT~1/config", None, b"x"),),
    ((".g\u200cit/config", None, b"x"),), (("x" * 70_000, None, b"x"),),
])
def test_an_unsafe_copy_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, members: tuple) -> None:
    server = TransferServer(monkeypatch)
    pc_b_base = tmp_path / "pcb"
    (pc_b_base / "galaius" / "projets").mkdir(parents=True)
    pc_b = _agents(pc_b_base, tmp_path, agent_roots=("galaius/projets",))
    archive = server.serve(uuid4(), _tar(*members))
    job = _settled(pc_b.workspaces, _answer(pc_b, _request("workspace_create", root="galaius/projets", name="aino", copy_of=archive)).workspaces[0].id)
    assert (job.state, job.code) == ("failed", "unsafe_archive")
    assert list((pc_b_base / "galaius" / "projets").iterdir()) == [] and not (pc_b_base / "galaius" / "x").exists() and not (pc_b_base.parent / "x").exists()


def test_a_web_clone_is_fast_forwarded_before_a_launch_only_when_clean(base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Its remote moved on: a clean clone follows it; one with uncommitted changes is left as it is;
    any other folder is never touched."""
    remote = tmp_path / "remote"
    subprocess.run(["git", "init", "-q", "-b", "main", str(remote)], check=True)
    commit = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(remote), "commit", "-q", "--allow-empty", "-m"]
    subprocess.run([*commit, "one"], check=True)
    clone = base / "project" / "aino"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "remote", "set-url", "origin", "https://github.com/owner/aino.git"], check=True)
    workspaces = _agents(base, tmp_path, clone_origins=("github.com/owner/*",)).workspaces
    config = hashlib.sha256((clone / ".git" / "config").read_bytes()).hexdigest()
    workspaces.jobs.put(KeptJob(id=uuid4(), root="project", name="aino", origin="github.com/owner/aino", state="ready", started_at=datetime.now(UTC), git_config=config))
    monkeypatch.setattr(MachineWorkspaces, "_public_host", staticmethod(lambda host: None))
    monkeypatch.setattr(MachineWorkspaces, "GIT_OPTIONS", (*MachineWorkspaces.GIT_OPTIONS, "-c", "protocol.file.allow=always", "-c", f"url.{remote}.insteadOf=https://github.com/owner/aino.git"))
    monkeypatch.setattr(Git, "PROTOCOLS", "https:ssh:file")  # the test's remote is a local folder
    subprocess.run([*commit, "two"], check=True)
    assert workspaces.refresh(clone / "src" if (clone / "src").is_dir() else clone) == "updated"
    assert workspaces.refresh(clone) == "current"
    subprocess.run([*commit, "three"], check=True)
    (clone / "tracked.txt").write_text("x")
    subprocess.run(["git", "-C", str(clone), "add", "tracked.txt"], check=True)
    assert workspaces.refresh(clone) == "dirty" and workspaces.refresh(base / "other") is None
    subprocess.run(["git", "-C", str(clone), "reset", "-q", "--hard"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "core.askPass", "/bin/false"], check=True)  # what a fenced agent could plant
    assert workspaces.refresh(clone) == "dirty"
    subprocess.run(["git", "-C", str(clone), "config", "--unset", "core.askPass"], check=True)
    assert workspaces.refresh(clone) == "updated"
    (clone / ".git" / "commondir").write_text(str(remote / ".git"))  # another repository's config, read in place of this one
    assert workspaces.refresh(clone) == "dirty"


def test_a_copy_of_a_folder_with_no_file_arrives_as_an_empty_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = TransferServer(monkeypatch)
    pc_b_base = tmp_path / "pcb"
    (pc_b_base / "galaius" / "projets").mkdir(parents=True)
    pc_b = _agents(pc_b_base, tmp_path, agent_roots=("galaius/projets",))
    archive = {**server.serve(uuid4(), _tar()), "files": 0}
    job = _settled(pc_b.workspaces, _answer(pc_b, _request("workspace_create", root="galaius/projets", name="aino", copy_of=archive)).workspaces[0].id)
    assert (job.state, job.name) == ("ready", "aino") and list((pc_b_base / "galaius" / "projets" / "aino").iterdir()) == []
