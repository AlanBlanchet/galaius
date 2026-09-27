"""The web drives agents on a machine only where, and only on what, its owner allowed there."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import asyncio
import hashlib
import hmac
import time

from interact_core import MACHINE_AGENT_REQUESTS, MachineAgentRequest
from interact.agents import registry as reg
from interact.machine_agents import MachineAgents, WebRun, WebRuns
from interact.machines import MachineConfig, MachineRunner


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
                         runs=WebRuns(path=tmp_path / "web-runs.json"), environment={"PATH": os.environ["PATH"]}, cli=cli)


def _request(op: str, config: MachineConfig | None = None, **fields) -> MachineAgentRequest:
    machine, workspace = (config.machine_id, config.workspace_id) if config else (uuid4(), uuid4())
    return MACHINE_AGENT_REQUESTS.validate_python({"id": str(uuid4()), "machine": {"id": str(machine)}, "workspace_id": str(workspace), "op": op, "initiator_account": str(uuid4()),
                                                   "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(), "signature": "0" * 64, **fields})


@pytest.mark.parametrize(("roots", "usable", "refused"), [
    (("project",), ["project"], []),
    (("interact-files",), [], ["interact-files"]),          # the same folder as a file root
    ((".",), [], ["."]),                                     # around the file root (and the working directory itself)
    (("project/.secret",), [], ["project/.secret"]),         # hidden
    (("../",), [], ["../"]),                                 # above the working directory
])
def test_agent_roots_never_touch_file_roots(base: Path, tmp_path: Path, roots, usable, refused) -> None:
    config = _config(base).model_copy(update={"agent_roots": roots})
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
    assert agents.answer(_request("folders")).roots == ("project",)
    listing = agents.answer(_request("folders", root="project"))
    assert [entry.name for entry in listing.entries] == ["src"] and listing.permission == "workspace_write"


def test_only_runs_started_from_the_web_are_reachable(base: Path, tmp_path: Path) -> None:
    agents = _agents(base, tmp_path)
    mine, child, foreign = (str(uuid4()) for _ in range(3))
    for run_id, parent in ((mine, None), (child, mine), (foreign, None)):
        reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name=run_id[:8], cwd=str(base / "project"), parent_run_id=parent, started_at=1.0, exit_code=0))
    agents.runs.add(WebRun(run_id=mine, root="project"))
    listed = agents.answer(_request("runs")).runs
    assert {str(run.run_id) for run in listed} == {mine, child}
    assert agents.answer(_request("tail", run_id=child)).cursor == 0
    with pytest.raises(PermissionError, match="not started from the web"):
        agents.answer(_request("stop", run_id=foreign))


def test_off_switch_and_missing_roots_refuse_everything(base: Path, tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="agents are off"):
        _agents(base, tmp_path, run_agents=False).answer(_request("folders"))
    empty = _agents(base, tmp_path).model_copy(update={"roots": {}})
    with pytest.raises(PermissionError, match="no agent folders"):
        empty.answer(_request("folders"))


def test_tail_reads_whole_lines_from_the_cursor(base: Path, tmp_path: Path) -> None:
    agents = _agents(base, tmp_path)
    run_id = str(uuid4())
    reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name="r", cwd=str(base / "project"), started_at=1.0))
    agents.runs.add(WebRun(run_id=run_id, root="project"))
    path = reg.events_path(run_id)
    path.write_bytes(b'{"kind":"text","text":"one"}\n{"kind":"text","te')
    first = agents.answer(_request("tail", run_id=run_id, cursor=0))
    assert [json.loads(line)["text"] for line in first.lines] == ["one"] and first.cursor == len(b'{"kind":"text","text":"one"}\n')
    with path.open("ab") as handle:
        handle.write(b'xt":"two"}\n')
    assert [json.loads(line)["text"] for line in agents.answer(_request("tail", run_id=run_id, cursor=first.cursor)).lines] == ["two"]


@pytest.mark.parametrize("brief", ["- fix the header", "--help", "-x"])
def test_a_brief_or_message_starting_with_a_dash_stays_text(base: Path, tmp_path: Path, brief: str) -> None:
    """Everything after "--" is an argument, never an option: the CLI receives the brief intact."""
    seen = tmp_path / "argv.json"
    recorder = ("python3", "-c", f"import json,sys; json.dump(sys.argv[1:], open({str(seen)!r}, 'w')); print({str(uuid4())!r})")
    agents = _agents(base, tmp_path, cli=recorder)
    agents.answer(_request("start", root="project", role="app-engineer", text=brief))
    argv = json.loads(seen.read_text())
    assert argv[-2:] == ["--", brief] and argv[:2] == ["agents", "spawn"]
    run_id = str(agents.runs.read()[-1].run_id)
    reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name="r", cwd=str(base / "project"), started_at=1.0))
    agents.answer(_request("send", run_id=run_id, text=brief))
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
    assert str(agents.answer(_request("start", root="project", role="app-engineer", text="go")).run_id) == run_id
    assert time.monotonic() - started < 10


def test_a_run_that_finished_unwatched_reads_done_not_lost(base: Path, tmp_path: Path) -> None:
    """Its process is gone and no exit was recorded, but its own stream says it finished."""
    agents = _agents(base, tmp_path)
    run_id = str(uuid4())
    reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name="r", cwd=str(base / "project"), started_at=1.0, pid=None))
    reg.events_path(run_id).write_text('{"kind":"text","text":"ok","at":2}\n{"kind":"done","text":"ok","at":3,"final_text":true}\n')
    agents.runs.add(WebRun(run_id=run_id, root="project"))
    assert [run.status for run in agents.answer(_request("runs")).runs] == ["done"]
