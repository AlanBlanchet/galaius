"""Conversation ownership is explicit and independent of cwd, PID and vendor child IDs."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from galaius.agents import registry as reg
from galaius.agents import run as run_module
from galaius.agents.providers import ClaudeCodeProvider, CodexProvider
from tests.support.agents import use_policy
from galaius.cli.app import app
from galaius.cli import app_commands
from galaius.server import tools_agents
from galaius.config import UserConfig


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(UserConfig, "PATH", tmp_path / ".galaius" / "config.env")
    for key in ("GALAIUS_PARENT_RUN_ID", "GALAIUS_SESSION_ID", *reg.CALLER_THREAD_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(reg, "_discover_foreign", lambda: [{"sessionId": "foreign-session", "name": "foreign-window", "cwd": "/same"}])


def recorded(run_id, session_id=None, parent_run_id=None):
    return reg.register(run_id=run_id, pid=None, provider="claude", name=run_id,
                        cwd="/same", session_id=session_id, parent_run_id=parent_run_id)


def test_two_conversations_same_directory_nested_children_and_unknown_history():
    first = recorded("first", "conversation-a")
    recorded("second", "conversation-b")
    recorded("nested", parent_run_id=first.run_id)
    recorded("grandchild", parent_run_id="nested")
    unknown = recorded("old-unknown")
    path = reg.agents_dir() / f"{unknown.run_id}.json"
    old = json.loads(path.read_text())
    del old["session_id"]
    old["provider_session_id"] = "conversation-a"
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    assert {run.run_id for run in reg.session_runs(session_id="conversation-a")} == {"first", "nested", "grandchild"}
    assert {run.run_id for run in reg.session_runs(session_id="conversation-b")} == {"second"}
    assert reg.session_runs(session_id="unused") == []
    assert path.read_bytes() == before
    assert {run.run_id for run in reg.session_runs(all_sessions=True)} == {"first", "second", "nested", "grandchild", "old-unknown"}
    # Session ownership never replaces genealogy or the provider's own continuation handle.
    assert first.parent_run_id is None and first.root_run_id is None and first.provider_session_id is None
    assert reg.get_run("nested").parent_run_id == "first"


def test_missing_identity_never_means_global_and_foreign_requires_explicit_all(monkeypatch):
    recorded("unrelated", "other")
    monkeypatch.setenv("CODEX_THREAD_ID", "server-inherited-thread")
    with pytest.raises(ValueError, match="Session identity required"):
        reg.session_runs(include_foreign=True)
    assert reg.session_runs(session_id="empty", include_foreign=True) == []
    assert "foreign-session" in {run.run_id for run in reg.session_runs(all_sessions=True, include_foreign=True)}
    with pytest.raises(ValueError, match="not both"):
        reg.session_runs(session_id="other", all_sessions=True)


def test_parent_owner_wins_and_conflicting_supplied_owner_is_rejected(monkeypatch):
    recorded("parent", "conversation-a")
    monkeypatch.setenv("GALAIUS_PARENT_RUN_ID", "parent")
    assert reg.resolve_session_id() == "conversation-a"
    assert recorded("child").session_id == "conversation-a"
    with pytest.raises(ValueError, match="conflicts"):
        recorded("wrong", "conversation-b")
    assert not (reg.agents_dir() / "wrong.json").exists()


@pytest.mark.parametrize("parent_id", ["old-parent", "missing-parent"])
def test_child_harness_thread_cannot_invent_unknown_parent_owner(monkeypatch, parent_id):
    recorded("old-parent")
    monkeypatch.setenv("GALAIUS_PARENT_RUN_ID", parent_id)
    monkeypatch.setenv("CODEX_THREAD_ID", "child-vendor-thread")
    assert reg.resolve_session_id(cli_harness=True) is None
    assert reg.resolve_session_id("explicit-owner", cli_harness=True) == "explicit-owner"


@pytest.mark.parametrize("variable", reg.CALLER_THREAD_ENV)
def test_a_cli_launch_records_the_calling_conversation_of_either_agent_cli(monkeypatch, variable):
    """A script or background command run inside a Claude Code or Codex conversation launches a
    run that belongs to that conversation (it recorded none, so the Runs page had no prompt)."""
    monkeypatch.setenv(variable, "conversation-a")
    with reg.session_context(cli_harness=True):
        assert recorded("launched").session_id == "conversation-a"
    # An MCP server may be shared between conversations: it never reads the caller's thread.
    assert reg.resolve_session_id() is None


@pytest.mark.parametrize("identity", ["", "..", "../other", "with space", "x" * 161])
def test_invalid_scope_rejected_at_edge(identity):
    with pytest.raises(ValueError):
        recorded("invalid", identity)


@pytest.mark.asyncio
async def test_concurrent_spawn_contexts_are_isolated_and_reset_on_exception():
    async def launch(identity):
        with reg.session_context(identity):
            await asyncio.sleep(0)
            return recorded(identity)
    first, second = await asyncio.gather(launch("conversation-a"), launch("conversation-b"))
    assert first.session_id == "conversation-a" and second.session_id == "conversation-b"
    with pytest.raises(RuntimeError), reg.session_context("temporary"):
        raise RuntimeError("fixture")
    assert reg.resolve_session_id() is None


def test_provider_native_child_inherits_recorded_owner_not_readers_environment(monkeypatch):
    recorded("root", "conversation-a")
    monkeypatch.setenv("GALAIUS_SESSION_ID", "reader-b")
    child = reg.upsert_provider_child(run_id="native", provider="claude", parent_run_id="root", root_run_id="root",
        spawned_by_event_id="event", cwd="/same", task="fixture", requested_model=None, status="running")
    assert child.session_id == "conversation-a"
    assert child.root_run_id == "root" and child.parent_run_id == "root"
    assert child.provider_session_id == "native"


@pytest.mark.asyncio
async def test_mcp_list_scopes_actual_registry_and_requires_missing_identity(monkeypatch):
    recorded("visible-a", "conversation-a")
    recorded("hidden-b", "conversation-b")
    recorded("unknown")
    monkeypatch.setenv("CODEX_THREAD_ID", "conversation-b")
    missing = await tools_agents.agent_list(include_foreign=False)
    assert "Session identity required" in missing and "hidden-b" not in missing
    result = await tools_agents.agent_list(session_id="conversation-a")
    assert "visible-a" in result and "hidden-b" not in result and "unknown" not in result
    result = await tools_agents.agent_list(all_sessions=True)
    assert all(name in result for name in ("visible-a", "hidden-b", "unknown"))
    assert "foreign-window" not in result
    result = await tools_agents.agent_list(all_sessions=True, include_foreign=True)
    assert "foreign-window" in result


@pytest.mark.asyncio
async def test_mcp_spawn_records_explicit_scope_without_process_env_mutation(monkeypatch):
    async def launch(provider, task, **kwargs):
        await asyncio.sleep(0)
        return recorded(task)
    monkeypatch.setattr(tools_agents, "run_agent", launch)
    monkeypatch.setattr(ClaudeCodeProvider, "available", lambda self: True)
    monkeypatch.setattr(ClaudeCodeProvider, "valid_definition", lambda self, name: True)
    first, second = await asyncio.gather(
        tools_agents.agent_spawn("child-a", agent="tester", session_id="conversation-a"),
        tools_agents.agent_spawn("child-b", agent="tester", session_id="conversation-b"))
    assert "Session: conversation-a" in first and "Session: conversation-b" in second
    assert reg.get_run("child-a").session_id == "conversation-a"
    assert reg.get_run("child-b").session_id == "conversation-b"
    assert "GALAIUS_SESSION_ID" not in os.environ


def test_cli_parser_scope_and_all_opt_in(capsys, monkeypatch):
    recorded("visible-a", "conversation-a")
    recorded("hidden-b", "conversation-b")
    with pytest.raises(SystemExit) as result:
        app(["agents", "list"])
    assert result.value.code == 2
    assert "Session identity required" in capsys.readouterr().err
    monkeypatch.setenv("CODEX_THREAD_ID", "conversation-a")
    with pytest.raises(SystemExit) as result:
        app(["agents", "list"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "visible-a" in output and "hidden-b" not in output
    with pytest.raises(SystemExit) as result:
        app(["agents", "list", "--all-sessions"])
    assert result.value.code == 0
    assert "hidden-b" in capsys.readouterr().out


def test_cli_spawn_harness_scope_and_explicit_scope_reach_registry(monkeypatch, capsys):
    async def launch(provider, task, **kwargs):
        return recorded(task, parent_run_id=kwargs["parent_run_id"])
    monkeypatch.setattr(app_commands, "_agent_runner", lambda: launch)
    monkeypatch.setenv("CODEX_THREAD_ID", "harness-thread")
    app_commands.agents_spawn("automatic", agent="tester")
    app_commands.agents_spawn("explicit", agent="tester", session_id="chosen-thread")
    app_commands.agents_spawn("nested", agent="tester", parent_run_id="explicit")
    assert reg.get_run("automatic").session_id == "harness-thread"
    assert reg.get_run("explicit").session_id == "chosen-thread"
    assert reg.get_run("nested").session_id == "chosen-thread"
    assert capsys.readouterr().out.splitlines() == ["automatic", "explicit", "nested"]


def test_actual_cli_process_reads_scoped_registry_without_vendor_launch():
    recorded("visible-a", "conversation-a")
    for index in range(217):
        recorded(f"hidden-{index}", "conversation-b")
    result = subprocess.run([str(Path(sys.executable).with_name("galaius")), "agents", "list", "--session-id", "conversation-a"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "visible-a" in result.stdout and "hidden-" not in result.stdout
    command = [str(Path(sys.executable).with_name("galaius")), "agents", "list"]
    missing = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert missing.returncode == 2 and "Session identity required" in missing.stderr
    assert "hidden-" not in missing.stdout
    every = subprocess.run([*command, "--all-sessions"], capture_output=True, text=True, timeout=20)
    assert every.returncode == 0
    assert sum("hidden-" in line for line in every.stdout.splitlines()) == 217 and "visible-a" in every.stdout


@pytest.mark.asyncio
async def test_real_launcher_and_child_process_inherit_owner_through_recorded_parent(tmp_path, monkeypatch):
    provider = CodexProvider()
    monkeypatch.setattr(CodexProvider, "available", lambda self: True)
    monkeypatch.setattr(CodexProvider, "authenticated", AsyncMock(return_value=True))
    use_policy(monkeypatch, run_module, agents={"tester": "fixture-model"}, reasoning={"tester": "medium"})
    monkeypatch.setattr(run_module, "resolve_model", lambda model, env, **kwargs: ({}, model))
    monkeypatch.setenv("GALAIUS_SESSION_ID", "stale-launcher-conversation")
    script = "import json; from galaius.agents import registry; print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':registry.resolve_session_id()}}))"
    monkeypatch.setattr(CodexProvider, "command", lambda self, *args, **kwargs: [sys.executable, "-c", script])
    with reg.session_context("conversation-a"):
        parent = await run_module.run_agent(provider, "fixture parent", agent="tester", cwd=str(tmp_path), mesh=False)
    await asyncio.wait_for(parent.wait(), timeout=20)
    child = await run_module.run_agent(provider, "fixture child", agent="tester", cwd=str(tmp_path), parent_run_id=parent.run_id, mesh=False)
    await asyncio.wait_for(child.wait(), timeout=20)
    runs = reg.session_runs(session_id="conversation-a")
    assert {run.run_id for run in runs} == {parent.run_id, child.run_id}
    assert reg.get_run(child.run_id).parent_run_id == parent.run_id
    for handle in (parent, child):
        assert any(event.text == "conversation-a" for event in reg.read_events(handle.run_id))


@pytest.mark.asyncio
async def test_mcp_registered_schema_and_dispatch_accept_explicit_scope():
    recorded("visible-a", "conversation-a")
    recorded("hidden-b", "conversation-b")
    tools = {tool.name: tool for tool in await tools_agents.mcp.list_tools()}
    properties = tools["agent_list"].inputSchema["properties"]
    assert properties["all_sessions"]["default"] is False
    assert "session_id" in properties and "session_id" in tools["agent_spawn"].inputSchema["properties"]
    result = await tools_agents.mcp.call_tool("agent_list", {"session_id": "conversation-a"})
    text = str(result)
    assert "visible-a" in text and "hidden-b" not in text
