"""The web drives agents on a machine only where, and only on what, its owner allowed there."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from interact_core import MachineAgentRequest, MachineRef
from interact.agents import registry as reg
from interact.machine_agents import MachineAgents, WebRun, WebRuns
from interact.machines import MachineConfig


@pytest.fixture
def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("INTERACT_AGENTS_DIR", str(tmp_path / "registry"))
    root = tmp_path / "dev"
    for folder in ("interact-files", "project/src/deep", "project/.secret", "other"):
        (root / folder).mkdir(parents=True)
    (root / "project" / "outside").symlink_to(tmp_path)
    return root


def _agents(base: Path, tmp_path: Path, **settings) -> MachineAgents:
    config = MachineConfig(server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(), token="t" * 40, permission_ceiling="full_access",
                           working_directory=base, agent_roots=("project",), **settings)
    return MachineAgents(config=config, runs=WebRuns(path=tmp_path / "web-runs.json"), environment={"PATH": os.environ["PATH"]}, cli=("false",))


def _request(agents: MachineAgents, op: str, **fields) -> MachineAgentRequest:
    return MachineAgentRequest(id=uuid4(), machine=MachineRef(id=agents.config.machine_id), workspace_id=agents.config.workspace_id, op=op,
                               initiator_account=uuid4(), expires_at=datetime.now(UTC) + timedelta(seconds=30), signature="0" * 64, **fields)


@pytest.mark.parametrize(("roots", "usable", "refused"), [
    (("project",), ["project"], []),
    (("interact-files",), [], ["interact-files"]),          # the same folder as a file root
    ((".",), [], ["."]),                                     # around the file root (and the working directory itself)
    (("project/.secret",), [], ["project/.secret"]),         # hidden
    (("../",), [], ["../"]),                                 # above the working directory
])
def test_agent_roots_never_touch_file_roots(base: Path, tmp_path: Path, roots, usable, refused) -> None:
    config = _agents(base, tmp_path).config.model_copy(update={"agent_roots": roots})
    found, refusals = config.usable_agent_roots()
    assert [path.relative_to(base.resolve()).as_posix() for path in found] == usable
    assert list(refusals) == refused


@pytest.mark.parametrize(("path", "allowed"), [("", True), ("src/deep", True), ("src/../../other", False), (".secret", False), ("outside", False), ("missing", False)])
def test_start_folder_stays_beneath_a_root(base: Path, tmp_path: Path, path: str, allowed: bool) -> None:
    agents = _agents(base, tmp_path)
    if allowed:
        assert agents.folder(agents.roots(), "project", path) == base.resolve() / "project" / path
    else:
        with pytest.raises(PermissionError):
            agents.folder(agents.roots(), "project", path)


def test_folders_lists_plain_subfolders_only(base: Path, tmp_path: Path) -> None:
    agents = _agents(base, tmp_path)
    assert agents.answer(_request(agents, "folders")).roots == ("project",)
    listing = agents.answer(_request(agents, "folders", root="project"))
    assert [entry.name for entry in listing.entries] == ["src"] and listing.permission == "workspace_write"


def test_only_runs_started_from_the_web_are_reachable(base: Path, tmp_path: Path) -> None:
    agents = _agents(base, tmp_path)
    mine, child, foreign = (str(uuid4()) for _ in range(3))
    for run_id, parent in ((mine, None), (child, mine), (foreign, None)):
        reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name=run_id[:8], cwd=str(base / "project"), parent_run_id=parent, started_at=1.0, exit_code=0))
    agents.runs.add(WebRun(run_id=mine, root="project"))
    listed = agents.answer(_request(agents, "runs")).runs
    assert {str(run.run_id) for run in listed} == {mine, child}
    assert agents.answer(_request(agents, "tail", run_id=child)).cursor == 0
    with pytest.raises(PermissionError, match="not started from the web"):
        agents.answer(_request(agents, "stop", run_id=foreign))


def test_off_switch_and_missing_roots_refuse_everything(base: Path, tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="agents are off"):
        _agents(base, tmp_path, run_agents=False).answer(_request(_agents(base, tmp_path), "folders"))
    empty = _agents(base, tmp_path).model_copy(update={"config": _agents(base, tmp_path).config.model_copy(update={"agent_roots": ()})})
    with pytest.raises(PermissionError, match="no agent folders"):
        empty.answer(_request(empty, "folders"))


def test_tail_reads_whole_lines_from_the_cursor(base: Path, tmp_path: Path) -> None:
    agents = _agents(base, tmp_path)
    run_id = str(uuid4())
    reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name="r", cwd=str(base / "project"), started_at=1.0))
    agents.runs.add(WebRun(run_id=run_id, root="project"))
    path = reg.events_path(run_id)
    path.write_bytes(b'{"kind":"text","text":"one"}\n{"kind":"text","te')
    first = agents.answer(_request(agents, "tail", run_id=run_id, cursor=0))
    assert [json.loads(line)["text"] for line in first.lines] == ["one"] and first.cursor == len(b'{"kind":"text","text":"one"}\n')
    with path.open("ab") as handle:
        handle.write(b'xt":"two"}\n')
    assert [json.loads(line)["text"] for line in agents.answer(_request(agents, "tail", run_id=run_id, cursor=first.cursor)).lines] == ["two"]
