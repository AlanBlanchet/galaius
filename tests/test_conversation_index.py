"""The conversation index: what a search finds, what it never shows, and whose conversations it reads."""

import asyncio
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest

from galaius.agents import registry as reg
from galaius.agents.conversations import ConversationIndex, ConversationScope
from galaius.agents.events import AgentEvent, instant
from galaius.agents.subscriptions import Subscription
from galaius.agents.subscriptions import RunSubscription
from galaius.fence import FenceSpec
from galaius.machine_agents import redact
from galaius.server import tools_agents

# Built in pieces so no secret scanner mistakes the fixture for a real key.
SECRET = "sk-" + "proj" + "A1b2C3d4E5f6G7h8I9j0"
ENV_SECRET = "hunter" + "2-correct-horse"
EVERYWHERE = ConversationScope(root="")


def _claude_session(home: Path, project: Path, session: str, records: list[dict]) -> Path:
    path = home / "projects" / str(project).replace("/", "-") / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    base = {"cwd": str(project), "sessionId": session}
    path.write_text("".join(json.dumps(base | record) + "\n" for record in records), encoding="utf-8")
    return path


def _turns(prompt: str, answer: str) -> list[dict]:
    """A saved Claude session as the CLI writes it: one record per block of an answer."""
    return [
        {"type": "user", "timestamp": "2026-10-11T09:00:00Z", "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "timestamp": "2026-10-11T09:00:05Z", "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}},
        {"type": "assistant", "timestamp": "2026-10-11T09:00:05Z", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": f"curl -H 'Authorization: Bearer {SECRET}' https://api.example.test"}}]}},
        {"type": "user", "timestamp": "2026-10-11T09:00:06Z", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "zanzibar tool output never indexed"}]}},
    ]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    project, other = tmp_path / "work" / "billing", tmp_path / "work" / "website"
    for folder in (project, other):
        (folder / ".git").mkdir(parents=True)
    monkeypatch.setenv("SERVICE_PASSWORD", ENV_SECRET)
    claude = tmp_path / ".claude"
    _claude_session(claude, project, str(uuid4()), _turns("why does the invoice rounding drift", f"rounding drifts in cents; the key {ENV_SECRET} was in the log"))
    _claude_session(claude, other, str(uuid4()), _turns("make the landing hero bigger", "the invoice page is elsewhere"))
    codex = tmp_path / ".codex" / "sessions" / "2026" / "10" / "11"
    codex.mkdir(parents=True)
    thread = str(uuid4())
    (codex / f"rollout-2026-10-11T10-00-00-{thread}.jsonl").write_text("".join(json.dumps(record) + "\n" for record in (
        {"type": "session_meta", "timestamp": "2026-10-11T10:00:00Z", "payload": {"cwd": str(project)}},
        {"type": "response_item", "timestamp": "2026-10-11T10:00:01Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>harness</environment_context>"}]}},
        {"type": "response_item", "timestamp": "2026-10-11T10:00:02Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "port the invoice exporter to parquet"}]}},
        {"type": "response_item", "timestamp": "2026-10-11T10:00:03Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "the exporter now writes parquet"}]}},
    )), encoding="utf-8")
    run_id = str(uuid4())
    reg.save_run(reg.AgentRun(run_id=run_id, provider="claude", name="fixer", agent="backend-engineer", cwd=str(project),
                              task="fix the invoice total", started_at=1_760_000_000.0, pid=None, lifecycle_token="t" * 32))
    for event in (AgentEvent(kind="text", text="invoice total fixed in the ledger", at=1_760_000_010.0),
                  AgentEvent(kind="tool_result", text="invoice zanzibar result"),
                  AgentEvent(kind="tool", tool="Write", tool_input=f"file_path='/x/.env' content='TOKEN={SECRET}'", at=1_760_000_020.0)):
        reg.append_event(run_id, event)
    index = ConversationIndex()
    assert index.refresh(None).pending == 0
    return {"project": project, "other": other, "run_id": run_id, "thread": thread, "claude": claude, "index": index}


def test_search_finds_every_source_and_never_tool_output(world: dict) -> None:
    index = world["index"]
    assert sorted(hit.conversation.source for hit in index.search("invoice", EVERYWHERE)) == ["claude", "claude", "codex", "galaius"]
    assert index.search("zanzibar", EVERYWHERE) == []  # what a tool returned
    assert index.search("content", EVERYWHERE) == []  # a tool call past its leading argument
    assert [hit.conversation.id for hit in index.search('"invoice exporter"', EVERYWHERE)] == [world["thread"]]


def test_credentials_are_masked_in_lines_titles_and_snippets(world: dict) -> None:
    index = world["index"]
    shown = [hit.line.text for hit in index.search("invoice", EVERYWHERE)] + [hit.line.text for hit in index.search("curl", EVERYWHERE)]
    for talk in (hit.conversation for hit in index.search("invoice", EVERYWHERE)):
        shown += [talk.title, *(line.text for line in index.read(talk, None, 200))]
    assert not [text for text in shown if SECRET in text or ENV_SECRET in text]
    started = time.monotonic()
    redact("a." * 20000)  # a long run of name characters: linear, never a stalled refresh
    assert time.monotonic() - started < 1.0


def test_a_scope_holds_reads_and_lookups(world: dict) -> None:
    index, inside = world["index"], ConversationScope(root=str(world["project"]))
    assert {hit.conversation.cwd for hit in index.search("invoice", inside)} == {str(world["project"])}
    with pytest.raises(LookupError, match="no conversation"):  # outside the scope reads as absent
        index.conversation(index.search("hero", EVERYWHERE)[0].conversation.id, inside)
    run = index.conversation(world["run_id"][:8], inside)
    assert (run.agent, run.title) == ("backend-engineer", "fix the invoice total")
    assert [line.kind for line in index.read(run, None, 50)] == ["prompt", "text", "tool"]
    assert [line.n for line in index.read(run, around=2, limit=2)] == [1, 2]
    # Active since: by its last line's moment, never its record file's (rewritten long after the run).
    assert "galaius" not in {hit.conversation.source for hit in index.search("invoice", EVERYWHERE, since=instant("2026-01-01"))}


def test_a_pinned_login_lists_every_login_and_drops_nothing(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """The index is shared: a run whose CLI login is a subscription's folder must still list the
    default folder's sessions, or its refresh would drop them as deleted."""
    before = len(world["index"].search("invoice", EVERYWHERE))
    pinned = Subscription.create(uuid4(), "claude")
    _claude_session(pinned.folder, world["project"], str(uuid4()), _turns("invoice in the pinned login", "seen"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(pinned.folder))
    assert ConversationIndex(environment=dict(os.environ)).refresh(None).pending == 0
    assert len(world["index"].search("invoice", EVERYWHERE)) == before + 1


def test_a_file_grows_is_rewritten_breaks_and_goes(world: dict) -> None:
    index = world["index"]
    path = next((world["claude"] / "projects").glob("*billing/*.jsonl"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "user", "cwd": str(world["project"]), "message": {"role": "user", "content": "and the gazpacho rate"}}) + "\n")
    index.refresh(None)
    assert len(index.search("gazpacho", EVERYWHERE)) == 1
    path.write_text(json.dumps({"type": "user", "cwd": str(world["project"]), "message": {"role": "user", "content": "a fresh start entirely"}}) + "\n", encoding="utf-8")
    unreadable = _claude_session(world["claude"], world["project"], str(uuid4()), _turns("never read", "-"))
    unreadable.chmod(0)
    # Records of a shape the reader never met: skipped, the lines around them kept.
    _claude_session(world["claude"], world["project"], str(uuid4()), [{"type": "user", "message": "x"}, {"type": "assistant", "message": {"content": [None]}},
                                                                      *_turns("the odd record survivor", "-")])
    assert index.refresh(None).pending == 0  # one file it cannot open stops none of the others
    assert (index.search("gazpacho", EVERYWHERE), len(index.search("fresh", EVERYWHERE)), len(index.search("survivor", EVERYWHERE))) == ([], 1, 1)
    path.unlink()
    index.refresh(None)
    assert index.search("fresh", EVERYWHERE) == []


@pytest.mark.parametrize("folder", ["home", "root", "loose"])
def test_a_folder_outside_any_repository_searches_nothing(world: dict, tmp_path: Path, folder: str) -> None:
    cwd = {"home": str(Path.home()), "root": "/", "loose": str(tmp_path / "loose")}[folder]
    Path(cwd).mkdir(exist_ok=True)
    with pytest.raises(PermissionError):
        ConversationScope.of_caller({}, cwd, False)


@pytest.mark.parametrize("held", ["denied", "lent", "fenced parent", "unknown"])
def test_a_held_run_searches_nothing_and_reads_only_its_team(world: dict, held: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run = reg.get_run(world["run_id"])
    environment = {"GALAIUS_PARENT_RUN_ID": run.run_id}
    assert ConversationScope.of_caller(environment, "/", False).root == str(world["project"])
    with pytest.raises(PermissionError):
        ConversationScope.of_caller(environment, "/", True)  # every project: the person's own session only
    if held == "denied":
        reg.save_run(run.model_copy(update={"denied_tools": ("Read",)}))
    elif held == "lent":
        reg.save_run(run.model_copy(update={"subscription": RunSubscription(lent=True)}))
    elif held == "fenced parent":  # a fenced run in the middle of an unfenced team
        root, parent = str(uuid4()), str(uuid4())
        reg.save_run(reg.AgentRun(run_id=root, provider="claude", name="root", cwd=str(world["project"]), started_at=1.0, pid=None, lifecycle_token="r" * 32))
        reg.save_run(reg.AgentRun(run_id=parent, provider="claude", name="parent", cwd=str(world["project"]), started_at=1.0, pid=None, lifecycle_token="p" * 32,
                                  root_run_id=root, parent_run_id=root, fence=FenceSpec(working_directory=world["project"], start=world["project"], state=world["project"] / "state")))
        reg.save_run(run.model_copy(update={"root_run_id": root, "parent_run_id": parent}))
    else:
        environment = {"GALAIUS_PARENT_RUN_ID": str(uuid4())}
    with pytest.raises(PermissionError):
        ConversationScope.of_caller(environment, "/", False)
    other = str(uuid4())
    reg.save_run(reg.AgentRun(run_id=other, provider="claude", name="other", cwd=str(world["other"]), started_at=1.0, pid=None, lifecycle_token="u" * 32))
    monkeypatch.setenv("GALAIUS_PARENT_RUN_ID", environment["GALAIUS_PARENT_RUN_ID"])
    assert asyncio.run(tools_agents.agent_events(other)).startswith("ERROR:")
