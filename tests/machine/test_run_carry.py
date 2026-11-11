"""A conversation is held by the server and continues on any of its owner's PCs as the same conversation
(`PcFeature.RUN_HOLD`): the PC that ran it sends a copy between turns, keeps its turns, and is never needed again."""

import io
import json
import os
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
    return Pc(tmp_path, "a", monkeypatch, "project"), Pc(tmp_path, "b", monkeypatch, "shared"), TransferServer(monkeypatch)


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
    output, the folder's memory or a credential) and keeps listing it, its turns its own still; PC-B takes it into its own project
    folder on its own login: same id and session, B's folder and permission, A's shown facts, listed there; once B has it
    A lists it no more, its files kept."""
    a, b, server = pcs
    run_id, cwd_a = _conversation(a)
    transfer = uuid4()
    measured = a.answer("agent_export", run_id=run_id, transfer=str(transfer)).carried
    assert (measured.files, str(measured.session)) == (6, run_id)  # record, raw stream, messages, image, transcript, sub-agent (no mirror yet)
    assert [str(item.run_id) for item in a.answer("runs").runs] == [run_id] and a.agents.runs.entry(UUID(run_id)).moved_to is None
    archive = server.outcome(transfer)
    with tarfile.open(fileobj=io.BytesIO(server.parts[(str(transfer), 0)]), mode="r:gz") as packed:
        names = set(packed.getnames())
    assert names == {f"run/{run_id}{suffix}" for suffix in (".json", ".raw.jsonl", ".messages.jsonl", f".media.{'a' * 20}.png")} \
        | {f"session/{run_id}.jsonl", f"session/{run_id}/subagents/agent-1.jsonl"}

    taken = b.answer("agent_import", run_id=run_id, root="shared", path="aino", archive=archive, run=_carried(run_id))
    assert taken.detail == "taken from PC-A: done"
    run, cwd_b = reg.get_run(run_id), b.base / "shared" / "aino"
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


def test_a_conversation_released_to_none_goes_on_where_it_was(pcs) -> None:
    a, _, _ = pcs
    run_id, _ = _conversation(a)
    a.answer("agent_release", run_id=run_id, to={"id": str(uuid4())})
    a.answer("agent_release", run_id=run_id)
    assert [str(item.run_id) for item in a.answer("runs").runs] == [run_id]


def test_a_conversation_whose_transcript_its_pc_lost_continues_on_another_pc(pcs) -> None:
    """2026-11-10: his switch alan-neovision → AlanLenovo refused « ne la détient plus »: a sign-out on 11-09 had deleted
    the Claude transcript on alan-neovision. Its copy travels without it, its stream with it; the PC taking it writes the
    transcript from that stream at its next turn — the same conversation, its first ask in it."""
    a, b, server = pcs
    run_id, _ = _conversation(a)
    said = [{"type": "system", "subtype": "init", "session_id": run_id},
            {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": [{"type": "text", "text": "Le Emma Hybrid à 399 €"}]}},
            {"type": "result", "subtype": "success", "result": "Le Emma Hybrid à 399 €"}]
    reg.raw_events_path(run_id).write_text("".join(json.dumps(line) + "\n" for line in said))
    for path in sorted(CLAUDE.session_files(a.home, Path(reg.get_run(run_id).cwd), run_id), reverse=True):
        path.unlink()   # the sign-out
    transfer = uuid4()
    assert a.answer("agent_export", run_id=run_id, transfer=str(transfer)).carried.files == 4   # record, raw stream, messages, image
    b.answer("agent_import", run_id=run_id, root="shared", path="aino", archive=server.outcome(transfer), run=_carried(run_id))
    run = reg.get_run(run_id)
    environment = {"HOME": str(b.base), "PATH": os.environ.get("PATH", ""), "CLAUDE_CONFIG_DIR": str(b.home)}
    reg.resumable(CLAUDE, run, run_id, environment)
    written = [json.loads(line) for line in CLAUDE.session_files(b.home, Path(run.cwd), run_id)[0].read_text().splitlines()]
    assert [line["type"] for line in written] == ["user", "assistant"] and written[0]["message"]["content"] == "trouve un matelas"


def test_a_conversation_with_neither_transcript_nor_stream_is_refused_typed(pcs) -> None:
    a, _, _ = pcs
    run_id, _ = _conversation(a)
    reg.raw_events_path(run_id).unlink()
    for path in sorted(CLAUDE.session_files(a.home, Path(reg.get_run(run_id).cwd), run_id), reverse=True):
        path.unlink()
    with pytest.raises(WorkspaceRefused) as refused:
        a.answer("agent_export", run_id=run_id, transfer=str(uuid4()))
    assert refused.value.refusal.reason == "source_missing"


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
        b.answer("agent_import", run_id=run_id, root="shared", path="aino", archive=archive, run=_carried(run_id, session))
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
        b.answer("agent_import", run_id=run_id, root="shared", path="aino", archive=archive, run=_carried(run_id))
    assert refused.value.refusal.reason == "exists" and reg.get_run(run_id).name == "own"


def test_a_copy_holds_each_stream_as_it_was_between_turns(pcs, tmp_path: Path) -> None:
    """code-reviewer cfe2d4e4 #4, 1a9989c7 L1: its PC's turns go on while the copy is packed; a stream appended meanwhile
    is copied as long as it was when measured, never half a line of the next turn, and a record rewritten whole meanwhile
    is copied as it was, never cut."""
    from galaius.machine.run_carry import RunPackFiles

    a, _, _ = pcs
    run_id, _ = _conversation(a)
    _measure, files = RunPackFiles(run=reg.get_run(run_id), program=CLAUDE).measure()
    before, record = reg.raw_events_path(run_id).read_bytes(), (a.registry / f"{run_id}.json").read_bytes()
    with reg.raw_events_path(run_id).open("ab") as stream:
        stream.write(b'{"type":"assistant","half')   # the next turn, mid-line
    reg._update_fields(run_id, {"last": "a much longer last answer written whole over the record " * 20})   # replaced, not appended
    RunPackFiles.write(files, tmp_path / "copy.tar.gz")
    with tarfile.open(tmp_path / "copy.tar.gz", "r:gz") as packed:
        assert packed.extractfile(f"run/{run_id}.raw.jsonl").read() == before
        assert packed.extractfile(f"run/{run_id}.json").read() == record   # the record as measured, whole


def test_a_members_conversation_on_a_lent_pc_is_never_copied_off_it(pcs) -> None:
    a, _, _ = pcs
    run_id, _ = _conversation(a)
    reg._update_fields(run_id, {"subscription": {"lent": True}})
    with pytest.raises(AgentRefused, match="lent"):
        a.answer("agent_export", run_id=run_id, transfer=str(uuid4()))


def test_a_copy_of_a_conversation_with_many_images_opens_one_file_at_a_time(pcs, tmp_path: Path) -> None:
    """code-reviewer 8eaa1a39 F2: never every file held open while it packs (an open-file limit of 1024 refused 1500)."""
    resource = pytest.importorskip("resource")   # POSIX: the open-file limit lowered for the pack

    from galaius.machine.run_carry import RunPackFiles

    a, _, _ = pcs
    run_id, _ = _conversation(a)
    for index in range(1500):
        (reg.agents_dir() / f"{run_id}.media.{index:020d}.png").write_bytes(b"png")
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(512, hard), hard))
    try:
        measured, files = RunPackFiles(run=reg.get_run(run_id), program=CLAUDE).measure()
        RunPackFiles.write(files, tmp_path / "copy.tar.gz")
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    with tarfile.open(tmp_path / "copy.tar.gz", "r:gz") as packed:
        assert len(packed.getnames()) == measured.files == 1506
