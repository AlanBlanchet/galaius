"""A conversation moved from one of its owner's PCs to another stays the same conversation (`PcFeature.RUN_CARRY`)."""

import io
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from galaius_core import AgentRefused

from galaius.agents import registry as reg
from galaius.agents.providers import ClaudeCodeProvider
from galaius.machine.projects.workspace_copy import WorkspaceRefused
from galaius.machine.web_runs import WebRun
from tests.machine.test_machine_agents import TransferServer, _agents, _answer, _request

CLAUDE = ClaudeCodeProvider()


class Pc:
    """One PC of the test: its own registry and Claude login folder, switched to before it answers."""

    def __init__(self, tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch, root: str) -> None:
        self.base, self.monkeypatch = tmp_path / name, monkeypatch
        self.registry, self.home = self.base / "registry", self.base / "claude"
        self.home.mkdir(parents=True)
        (self.base / root / "aino").mkdir(parents=True)
        (self.base / "state").mkdir()
        self.use()
        self.agents = _agents(self.base, self.base / "state", agent_roots=(root,))

    def use(self) -> "Pc":
        self.monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(self.registry))
        self.monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self.home))
        return self

    def answer(self, op: str, **fields):
        self.use()
        return _answer(self.agents, _request(op, **fields))


@pytest.fixture
def pcs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Pc, Pc, TransferServer]:
    return Pc(tmp_path, "a", monkeypatch, "project"), Pc(tmp_path, "b", monkeypatch, "galaius/projets"), TransferServer(monkeypatch)


def _conversation(pc: Pc) -> tuple[str, Path]:
    """A finished conversation started on `pc` from the web in project/aino: its record, streams, an image, its Claude
    transcript and sub-agent file, beside that project folder's memory and the login's credentials (never carried)."""
    pc.use()
    run_id, cwd = str(uuid4()), pc.base / "project" / "aino"
    reg.register(run_id=run_id, pid=None, provider="claude", name="Matelas", task="trouve un matelas", cwd=str(cwd), agent="main", permission_mode="full_access",
                 provider_session_id=run_id)
    reg._update_fields(run_id, {"status": "done", "exit_code": 0, "finished_at": time.time(), "last": "Le Emma Hybrid à 399 €", "cost_usd": 0.42})
    reg.raw_events_path(run_id).write_text('{"type":"system","session_id":"%s"}\n' % run_id)
    reg.messages_path(run_id).write_text('{"kind":"message","text":"trouve un matelas"}\n')
    (reg.agents_dir() / f"{run_id}.media.{'a' * 20}.png").write_bytes(b"png")
    (reg.agents_dir() / f"{run_id}.stderr").write_text("never carried")
    folder = CLAUDE.project_folder(pc.home, cwd)
    (folder / run_id / "subagents").mkdir(parents=True)
    (folder / f"{run_id}.jsonl").write_text('{"type":"user","message":"trouve un matelas"}\n')
    (folder / run_id / "subagents" / "agent-1.jsonl").write_text("{}\n")
    (folder / "memory").mkdir()
    (folder / "memory" / "MEMORY.md").write_text("never carried")
    (pc.home / ".credentials.json").write_text("secret")
    pc.agents.runs.add(WebRun(run_id=run_id, root="project", path="aino"))
    return run_id, cwd


def _carried(run_id: str, session: str | None = None) -> dict:
    return {"role": "main", "name": "Matelas", "task": "trouve un matelas", "provider": "claude", "session": session or run_id,
            "moved_from": "PC-A", "moved_at": datetime.now(UTC).isoformat()}


def test_a_conversation_moved_to_another_pc_is_the_same_one_there(pcs) -> None:
    """PC-A packs the finished conversation (record, streams, image, transcript and its sub-agent folder; never its error
    output, the folder's memory or a credential) and holds its turns while it moves; PC-B takes it into its own project
    folder on its own login: same id and session, B's folder and permission, A's shown facts, listed there; once B has it
    A lists it no more, its files kept."""
    a, b, server = pcs
    run_id, cwd_a = _conversation(a)
    transfer = uuid4()
    measured = a.answer("agent_export", run_id=run_id, transfer=str(transfer)).carried
    assert (measured.files, str(measured.session)) == (6, run_id)  # record, raw stream, messages, image, transcript, sub-agent (no mirror yet)
    with pytest.raises(AgentRefused, match="moving"):
        a.answer("send", run_id=run_id, text="encore")
    archive = server.outcome(transfer)
    with tarfile.open(fileobj=io.BytesIO(server.parts[(str(transfer), 0)]), mode="r:gz") as packed:
        names = set(packed.getnames())
    assert names == {f"run/{run_id}{suffix}" for suffix in (".json", ".raw.jsonl", ".messages.jsonl", f".media.{'a' * 20}.png")} \
        | {f"session/{run_id}.jsonl", f"session/{run_id}/subagents/agent-1.jsonl"}

    taken = b.answer("agent_import", run_id=run_id, root="galaius/projets", path="aino", archive=archive, run=_carried(run_id))
    assert taken.detail == "taken from PC-A: done"
    run, cwd_b = reg.get_run(run_id), b.base / "galaius" / "projets" / "aino"
    assert (run.cwd, run.provider_session_id, run.agent, run.permission_mode, run.last, run.cost_usd, run.status, run.pid) == (
        str(cwd_b), run_id, "main", b.agents.scope(None), "Le Emma Hybrid à 399 €", 0.42, "done", None)
    landed = CLAUDE.project_folder(b.home, cwd_b)
    assert {path.relative_to(landed).as_posix() for path in landed.rglob("*") if path.is_file()} == {f"{run_id}.jsonl", f"{run_id}/subagents/agent-1.jsonl"}
    assert CLAUDE.session_files(b.home, cwd_b, run_id)[0].read_text() == '{"type":"user","message":"trouve un matelas"}\n'
    assert not (b.home / ".credentials.json").exists() and not (reg.agents_dir() / f"{run_id}.stderr").exists()
    assert [str(item.run_id) for item in b.answer("runs").runs] == [run_id]
    assert any("trouve un matelas" in line for line in b.answer("tail", run_id=run_id).lines)   # its thread shows its history there

    a.answer("agent_release", run_id=run_id, to={"id": str(uuid4())})
    assert a.answer("runs").runs == () and (a.registry / f"{run_id}.json").is_file()
    with pytest.raises(AgentRefused, match="not started"):
        a.answer("send", run_id=run_id, text="encore")


def test_a_move_that_failed_lets_its_conversation_go_on_where_it_was(pcs) -> None:
    a, _, server = pcs
    run_id, _ = _conversation(a)
    a.answer("agent_export", run_id=run_id, transfer=str(uuid4()))
    a.answer("agent_release", run_id=run_id)
    assert not a.agents.runs.entry(UUID(run_id)).moves_now()
    assert [str(item.run_id) for item in a.answer("runs").runs] == [run_id]


def _archive(server: TransferServer, *members: tuple[str, bytes]) -> dict:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as packed:
        for name, content in members:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            packed.addfile(member, io.BytesIO(content))
    transfer = uuid4()
    return {**server.serve(transfer, buffer.getvalue()), "files": len(members)}


@pytest.mark.parametrize("crafted", ["memory", "other_session", "record_session", "no_session"])
def test_a_crafted_conversation_writes_nothing_on_the_pc_receiving_it(pcs, crafted: str) -> None:
    """The receiving PC takes only the run's own registry names and its session's: a file for the CLI folder's memory,
    another session, a record naming another session, or no session at all is refused `unsafe_archive`, nothing written."""
    a, b, server = pcs
    run_id, _ = _conversation(a)
    record = (a.registry / f"{run_id}.json").read_bytes()
    other = str(uuid4())
    members = {"memory": [("session/memory/MEMORY.md", b"x")], "other_session": [(f"session/{other}.jsonl", b"{}")],
               "record_session": [(f"session/{run_id}.jsonl", b"{}")], "no_session": []}[crafted]
    session = other if crafted == "record_session" else run_id
    archive = _archive(server, (f"run/{run_id}.json", record), *members)
    with pytest.raises(WorkspaceRefused) as refused:
        b.answer("agent_import", run_id=run_id, root="galaius/projets", path="aino", archive=archive, run=_carried(run_id, session))
    assert refused.value.refusal.reason == "unsafe_archive"
    assert reg.get_run(run_id) is None and not any(b.home.rglob("*.md")) and not any(b.home.rglob("*.jsonl"))
    assert not any(path.name.startswith(".carry-") for path in reg.agents_dir().iterdir()) if reg.agents_dir().is_dir() else True


def test_a_conversation_never_lands_over_one_the_pc_holds(pcs) -> None:
    a, b, server = pcs
    run_id, _ = _conversation(a)
    transfer = uuid4()
    a.answer("agent_export", run_id=run_id, transfer=str(transfer))
    archive = server.outcome(transfer)
    b.use()
    reg.register(run_id=run_id, pid=None, provider="claude", name="own", cwd=str(b.base))
    with pytest.raises(WorkspaceRefused) as refused:
        b.answer("agent_import", run_id=run_id, root="galaius/projets", path="aino", archive=archive, run=_carried(run_id))
    assert refused.value.refusal.reason == "exists" and reg.get_run(run_id).name == "own"
