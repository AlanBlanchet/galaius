"""The agent-mesh MCP tools — the surface a spawned agent uses to build a team.

An agent cannot ask a follow-up question, so every failure here has to be self-explaining: an
uninstalled provider says which ones ARE installed, an unknown run id says how to find the real
ones. These tests pin that, because an error an agent can't act on stalls a whole team.
"""

import ast
from pathlib import Path
from uuid import uuid4

import pytest
from interact_core import AgentRevisionRef

import interact.server as srv
import interact.server.tools_agents as tools_agents
from interact.agents import registry as reg
from interact.agents.events import AgentEvent


def test_agent_tool_functions_have_no_local_imports() -> None:
    module_path = tools_agents.__file__
    assert module_path is not None
    tree = ast.parse(Path(module_path).read_text())
    functions = (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    )
    local_imports = [
        (function.name, imported.lineno)
        for function in functions
        for imported in ast.walk(function)
        if isinstance(imported, (ast.Import, ast.ImportFrom))
    ]

    assert local_imports == []


@pytest.mark.asyncio
async def test_an_unknown_provider_lists_the_real_ones():
    out = await srv.agent_spawn("do it", provider="gpt5-cli")
    assert out.startswith("ERROR") and "claude" in out


@pytest.mark.asyncio
async def test_an_uninstalled_provider_says_what_is_installed(monkeypatch):
    from interact.agents.providers import CodexProvider

    monkeypatch.setattr(CodexProvider, "available", lambda self: False)
    out = await srv.agent_spawn("do it", provider="codex")
    assert out.startswith("ERROR") and "not installed" in out


@pytest.mark.asyncio
async def test_an_empty_team_still_says_what_is_possible():
    out = await srv.agent_list(session_id="fixture-conversation")
    assert "No agent runs" in out and "Providers available" in out


@pytest.mark.asyncio
async def test_the_listing_shows_status_cost_and_the_tree(monkeypatch):
    reg.register(run_id="p-1", pid=1, provider="claude", name="lead", task="t", cwd="/tmp")
    reg.register(run_id="c-1", pid=2, provider="codex", name="helper", task="t", cwd="/tmp",
                 parent_run_id="p-1")
    reg.append_event("c-1", AgentEvent(kind="done", cost_usd=0.125))
    monkeypatch.setattr(reg, "_alive", lambda pid: True)
    monkeypatch.setattr(reg, "_discover_foreign", lambda: [])

    out = await srv.agent_list(all_sessions=True)
    assert "lead" in out and "helper" in out
    assert "0.1250" in out          # per-agent cost is real, not a placeholder
    assert "Team tree" in out and "spawned by" in out
    assert "API-equivalent" in out  # never presented as fresh spend


@pytest.mark.asyncio
async def test_agent_list_schema_does_not_invent_subscription_billing():
    tools = await srv.mcp.list_tools()
    description = next(tool.description for tool in tools if tool.name == "agent_list")

    assert "already paid" not in description.lower()
    assert "not fresh spend" not in description.lower()
    assert "charge path" in description.lower()
    assert "account impact" in description.lower()
    assert "unknown" in description.lower()


@pytest.mark.asyncio
async def test_foreign_sessions_are_marked_as_not_ours(monkeypatch):
    monkeypatch.setattr(reg, "_discover_foreign",
                        lambda: [{"sessionId": "abcd1234", "name": "their-window",
                                  "cwd": "/x", "kind": "interactive"}])
    out = await srv.agent_list(all_sessions=True, include_foreign=True)
    assert "their-window" in out and "not started by interact" in out


@pytest.mark.asyncio
async def test_events_for_an_unknown_run_say_how_to_find_the_real_ones():
    out = await srv.agent_events("nope")
    assert out.startswith("ERROR") and "agent_list" in out


@pytest.mark.asyncio
async def test_events_are_rendered_oldest_first(monkeypatch):
    reg.register(run_id="r", pid=1, provider="claude", name="w", task="t", cwd="/tmp")
    reg.append_event("r", AgentEvent(kind="tool", tool="Bash"))
    reg.append_event("r", AgentEvent(kind="text", text="finished up"))
    out = await srv.agent_events("r")
    assert out.index("using Bash") < out.index("finished up")


@pytest.mark.asyncio
async def test_stopping_an_unknown_run_is_an_actionable_error():
    out = await srv.agent_stop("nope")
    assert out.startswith("ERROR") and "agent_list" in out


@pytest.mark.asyncio
async def test_providers_report_availability_and_codexs_remaining_limit():
    """Codex was exercised end to end (`test_codex_is_verified_and_states_the_limit_that_remains`)
    so its caveat is no longer "unverified" — but ONE real limit survives (no per-tool flag for a
    native tool) and this listing must still say so; an untested adapter must not look as solid as
    a tested one, and a tested-but-limited one must not look unqualified either."""
    out = await srv.agent_providers()
    assert "claude" in out and "codex" in out
    assert "unverified" not in out.lower()
    assert "native tool" in out.lower()


# ── Agents are named after their agent FILE ─────────────────────────────────────────────────
# "the agents should have proper names right ? The name of the agent file ? Right ?" — the
# provider could already resolve one (`--agent <name>` reads ~/.claude/agents/<name>.md) and
# run_agent took the parameter, but the spawn TOOL never exposed it, so no caller could ask for
# one and every run fell back to being called after its provider.


@pytest.mark.asyncio
async def test_an_agent_can_be_spawned_by_its_definition_name(monkeypatch):
    seen = {}

    async def _fake_run(provider, task, **kw):
        seen.update(kw)

        class _H:
            run_id, name, pid = "r1", kw.get("name") or "?", 1

        return _H()

    monkeypatch.setattr("interact.server.tools_agents.run_agent", _fake_run)
    monkeypatch.setattr("interact.agents.providers.ClaudeCodeProvider.available", lambda self: True)
    await srv.agent_spawn("review it", agent="code-reviewer")
    assert seen["agent"] == "code-reviewer", "the definition must reach the CLI"


@pytest.mark.asyncio
async def test_mcp_validates_and_forwards_exact_revision_without_latest_lookup(monkeypatch):
    reference = AgentRevisionRef(id=uuid4(), revision=uuid4())
    seen = []

    async def launch(provider, task, **kwargs):
        seen.append(kwargs)

        class Handle:
            run_id, name, pid = "fixture-pinned", "Fixture pinned", 1

        return Handle()

    def forbidden_lookup(*args, **kwargs):
        pytest.fail("pinned MCP launch tried resolving latest local definition")

    monkeypatch.setattr(tools_agents, "run_agent", launch)
    monkeypatch.setattr("interact.agents.providers.ClaudeCodeProvider.available", lambda self: True)
    monkeypatch.setattr("interact.agents.providers.ClaudeCodeProvider.valid_definition", forbidden_lookup)
    result = await srv.mcp.call_tool("agent_spawn", {
        "task": "Bounded task", "agent": "fixture-worker", "agent_ref": reference.model_dump(mode="json"),
        "delegate": "ask_worker",
    })
    assert result
    assert seen[0]["agent_ref"] == reference and seen[0]["delegate"] == "ask_worker"


@pytest.mark.asyncio
async def test_the_run_is_NAMED_after_the_definition_not_the_provider(monkeypatch):
    """Without this the panel lists three runs all called 'claude' — the complaint itself."""
    seen = {}

    async def _fake_run(provider, task, **kw):
        seen.update(kw)

        class _H:
            run_id, name, pid = "r1", kw.get("name") or "?", 1

        return _H()

    monkeypatch.setattr("interact.server.tools_agents.run_agent", _fake_run)
    monkeypatch.setattr("interact.agents.providers.ClaudeCodeProvider.available", lambda self: True)
    await srv.agent_spawn("review it", agent="code-reviewer")
    assert seen["name"] == "code-reviewer"


@pytest.mark.asyncio
async def test_an_explicit_name_still_wins_over_the_definition(monkeypatch):
    seen = {}

    async def _fake_run(provider, task, **kw):
        seen.update(kw)

        class _H:
            run_id, name, pid = "r1", "x", 1

        return _H()

    monkeypatch.setattr("interact.server.tools_agents.run_agent", _fake_run)
    monkeypatch.setattr("interact.agents.providers.ClaudeCodeProvider.available", lambda self: True)
    await srv.agent_spawn("do it", agent="code-reviewer", name="second-opinion")
    assert seen["name"] == "second-opinion"


# --- A model must not be able to widen its own agents' privileges -----------------------------
#
# `agent_spawn` is called BY A MODEL, and a model's context routinely holds text it did not write:
# a fetched page, a file, an issue body. Exposing the autonomy setting there means an indirect
# injection can stand up an unrestricted agent with nobody watching — the tool docstring even
# advertises the ids. The human-driven paths (the CLI, the panel's picker) keep offering it,
# because a person choosing "no restrictions" for themselves is the whole point of the control.


@pytest.mark.asyncio
async def test_a_tool_caller_cannot_spawn_an_unrestricted_agent(monkeypatch):
    from interact.server import tools_agents

    monkeypatch.setattr("interact.agents.providers.ClaudeCodeProvider.available", lambda self: True)

    spawned = []

    async def _never(*a, **k):
        spawned.append(k)
        raise AssertionError("the spawn must be refused before it starts")

    monkeypatch.setattr(tools_agents, "run_agent", _never)
    fn = getattr(tools_agents.agent_spawn, "fn", tools_agents.agent_spawn)
    out = await fn(task="t", permission_mode="bypassPermissions")
    assert out.startswith("ERROR:"), out
    assert "bypassPermissions" in out
    assert not spawned, "an unrestricted agent was started by a tool call"


@pytest.mark.asyncio
async def test_a_restricted_mode_still_goes_through(monkeypatch):
    """The control is not disabled for models — only its unrestricted end is. Handing an agent
    'plan' is exactly the safe delegation this feature exists for."""
    from interact.server import tools_agents

    monkeypatch.setattr("interact.agents.providers.ClaudeCodeProvider.available", lambda self: True)

    got = {}

    class _Handle:
        run_id = "r-1"

    async def _spawn(prov, task, **k):
        got.update(k)
        return _Handle()

    monkeypatch.setattr(tools_agents, "run_agent", _spawn)
    fn = getattr(tools_agents.agent_spawn, "fn", tools_agents.agent_spawn)
    out = await fn(task="t", permission_mode="plan")
    assert not out.startswith("ERROR:"), out
    assert got["permission_mode"] == "plan"
