"""Server catalog consumers: authentication, validated snapshots and launch refresh."""

import asyncio
import hashlib
import json
import multiprocessing
import sys
from http.cookiejar import LWPCookieJar
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from galaius_core import AgentCatalogSnapshot, AgentRevision, AgentRevisionRef, PromptExecutionRef, PromptKey, PromptRevision

from galaius.agents.catalog import AgentCatalog, AgentInstructionSet, CatalogSnapshot
from galaius.agents import codex_policy_hook
from galaius.agents import registry as reg
from galaius.agents.catalog_connection import (
    CatalogAuthenticationError,
    CatalogConnection,
    CatalogConnectionError,
)
from galaius.agents.policy import Policy
from galaius.agents.providers import AgentProvider, ClaudeCodeProvider, CodexProvider
from unittest.mock import AsyncMock
from galaius.agents.run import ModelUnavailable, launch_continuation, run_agent
from galaius.config import UserConfig
from galaius.private_files import PRIVATE_FILES
from tests.support.private_files import loosen
from galaius.cli.app import app
from galaius.cli import app_commands


def snapshot(version=1, tools=None):
    prompts = tuple(PromptRevision(
        key=PromptKey(namespace="fixture", slug=slug), revision=uuid4(),
        content=f"{slug} instruction revision {version}",
        digest=hashlib.sha256(f"{slug} instruction revision {version}".encode()).hexdigest(),
        source_commit="a" * 40, created_at=datetime.now(UTC),
    ) for slug in ("primary", "first", "second", "skill"))
    refs = tuple(PromptExecutionRef(key=value.key, revision=value.revision, digest=value.digest, channel="stable") for value in prompts)
    worker = AgentRevision(
        id=uuid4(), revision=uuid4(), name="Fixture worker", role_key="fixture-worker",
        prompt=refs[0], paradigms=(refs[1], refs[2]), skill_paradigms=(refs[3],),
        criteria=f"price.in >= {version}", reasoning="high" if version == 1 else "low",
        criteria_weights="gui.screenspot=1", harness_tools=tools if tools is not None else (("Read",) if version == 1 else ("Write",)),
        resources=(), created_at=datetime.now(UTC),
    )
    lead = AgentRevision(
        id=uuid4(), revision=uuid4(), name="Fixture lead", role_key="fixture-lead", prompt=refs[0],
        criteria="price.in >= 0", resources=(), created_at=datetime.now(UTC),
        capabilities=({"kind": "delegate", "name": "ask_worker", "description": "Ask the fixture worker",
                       "agent": {"id": worker.id, "revision": worker.revision},
                       "input_schema": {"properties": {}, "required": ()}},),
    )
    worker = worker.model_copy(update={"reports_to": lead.id})
    return AgentCatalogSnapshot.create((lead, worker), prompts)


@pytest.fixture
def catalog_home(monkeypatch, tmp_path):
    monkeypatch.setattr(AgentProvider, "authenticated", AsyncMock(return_value=True))
    monkeypatch.setattr(UserConfig, "PATH", tmp_path / "config.env")
    monkeypatch.delenv("GALAIUS_PARENT_RUN_ID", raising=False)
    return tmp_path


#: Other processes racing this one: `fork` where the system has it, else `spawn` (Windows), whose
#: children import their target by name - hence the module-level targets below.
PROCESSES = multiprocessing.get_context("fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn")
#: How many race at once: a forked child shares its parent's memory; a spawned one re-imports
#: galaius in a process of its own (~150 MB each), and fifty of those exhaust a CI runner.
RACERS = 50 if PROCESSES.get_start_method() == "fork" else 8


def deny_catalog(connection_json: str, value, status: int, config: str) -> None:
    """Another process whose refresh the server refuses (`status`); `config` is `catalog_home`'s
    settings file (a spawned child does not inherit the parent's patch)."""
    UserConfig.PATH = Path(config)
    fresh = CatalogConnection.model_validate_json(connection_json)
    with pytest.raises(CatalogAuthenticationError):
        AgentCatalog.refresh(fresh, transport=catalog_transport(value, status))


def share_preview_login(connection_json: str, login_log: str, config: str) -> None:
    """Another process signing in to the preview: it logs each real login it performs."""
    UserConfig.PATH = Path(config)
    fresh = CatalogConnection.model_validate_json(connection_json)

    def respond(request):
        if request.method == "POST":
            with open(login_log, "a") as output:
                output.write("login\n")
            return httpx.Response(200, headers={"Set-Cookie": "session=fixture-cookie; Path=/; HttpOnly"}, json={})
        assert request.headers["Cookie"] == "session=fixture-cookie"
        return httpx.Response(200, json={})

    with fresh.connect(transport=httpx.MockTransport(respond)) as client:
        fresh.authenticate(client)
        fresh.authenticate(client)
        fresh.request(client, "GET", f"/v1/workspaces/{fresh.workspace_id}/agent-catalog")


def catalog_transport(value, status=200):
    def respond(request):
        if request.url.path == "/v1/auth/local-preview":
            return httpx.Response(200, headers={"Set-Cookie": "session=fixture-cookie; Path=/"}, json={})
        assert request.headers["Cookie"] == "session=fixture-cookie"
        return httpx.Response(status, content=value.model_dump_json())
    return httpx.MockTransport(respond)


def test_server_revisions_replace_prompt_policy_skills_and_team(catalog_home):
    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode="preview", workspace_id=uuid4())
    before = AgentCatalog.refresh(connection, transport=catalog_transport(snapshot(1)))
    after = AgentCatalog.refresh(connection, transport=catalog_transport(snapshot(2)))
    assert before.snapshot.cursor != after.snapshot.cursor
    assert Policy.from_catalog(before).criterion_for("fixture-worker") == "price.in >= 1"
    policy = Policy.from_catalog(after)
    assert policy.criterion_for("fixture-worker") == "price.in >= 2"
    assert policy.reasoning_for("fixture-worker") == "low"
    assert policy.tools_for("fixture-worker") == ["Write"]
    assert policy.weights_for("fixture-worker") == "gui.screenspot=1"
    text = after.definition("fixture-worker", "bounded task")
    assert text.index("primary instruction") < text.index("first instruction") < text.index("second instruction")
    assert "Skill: fixture/skill" in text and "skill instruction revision 2" not in text
    assert after.skill_path(after.snapshot.role("fixture-worker").skill_paradigms[0]).read_text().strip() == "skill instruction revision 2"
    assert "Reports to: Fixture lead (role: fixture-lead)" in text
    assert "Delegate capability ask_worker: Fixture worker (role: fixture-worker)" in after.definition("fixture-lead", "task")
    with pytest.raises(CatalogConnectionError, match="absent"):
        policy.criterion_for("unknown")


@pytest.mark.parametrize("refusal", [401, 403, 404])
def test_deleted_cache_resync_and_network_fallback_auth_revocation(catalog_home, capsys, refusal):
    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode="preview", workspace_id=uuid4())
    value = snapshot()
    AgentCatalog.refresh(connection, transport=catalog_transport(value))
    AgentCatalog.cache_path().unlink()
    restored = AgentCatalog.refresh(connection, transport=catalog_transport(value))
    assert restored.snapshot.cursor == value.cursor

    def offline(request):
        raise httpx.ConnectError("fixture network failure")

    stale = AgentCatalog.refresh(connection, allow_stale=True, transport=httpx.MockTransport(offline))
    assert stale.stale and stale.fetched_at == restored.fetched_at
    assert "STALE agent catalog" in capsys.readouterr().err
    with pytest.raises(CatalogAuthenticationError):
        AgentCatalog.refresh(connection, allow_stale=True, transport=catalog_transport(value, status=refusal))
    assert not AgentCatalog.cache_path().exists()
    with pytest.raises(CatalogConnectionError, match="no validated cache"):
        AgentCatalog.refresh(connection, allow_stale=True, transport=httpx.MockTransport(offline))


@pytest.mark.parametrize("damage", ["cursor", "content", "role", "missing-prompt"])
def test_invalid_server_snapshot_never_replaces_last_valid_cache(catalog_home, damage):
    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode="preview", workspace_id=uuid4())
    value = snapshot()
    AgentCatalog.refresh(connection, transport=catalog_transport(value))
    previous = AgentCatalog.cache_path().read_bytes()
    invalid = value.model_dump(mode="json")
    if damage == "cursor":
        invalid["cursor"] = "0" * 64
    elif damage == "content":
        invalid["paradigms"][0]["content"] = "wrong digest"
    elif damage == "role":
        invalid["agents"][0]["role_key"] = "../../outside"
    else:
        invalid["paradigms"] = []
    with connection.connect(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=invalid))) as client:
        with pytest.raises(ValidationError):
            CatalogSnapshot.model_validate_json(connection.request(client, "GET", "/catalog"))
    with pytest.raises(CatalogConnectionError, match="invalid server"):
        AgentCatalog.refresh(connection, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=invalid)))
    assert AgentCatalog.cache_path().read_bytes() == previous


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", [ClaudeCodeProvider, CodexProvider])
async def test_common_launcher_uses_one_catalog_for_prompt_and_policy(catalog_home, monkeypatch, provider_type):
    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode="preview", workspace_id=uuid4())
    captured = []
    original = provider_type.command

    def command(self, task, **kwargs):
        captured.append((task, kwargs, original(self, task, **kwargs)))
        return [sys.executable, "-c", "print('{}')"]

    monkeypatch.setattr(provider_type, "available", lambda self: True)
    monkeypatch.setattr(provider_type, "command", command)
    monkeypatch.setattr("galaius.agents.run.resolve_model", lambda rule, env, **kwargs: ({}, "fixture-model"))
    # The ranked list is stubbed at its entry point for the same reason the resolver is: the
    # fixture role weights `gui.screenspot`, which no catalog row scores, so a real ranking finds
    # nothing — and a real launch is not what these tests measure.
    monkeypatch.setattr("galaius.agents.run.rank_candidates", lambda rule, env, *, providers, weights="", role=None: (
        reg.LaunchCandidate(provider=providers[0].name, model=rule, rank=0),))
    for version in (1, 2):
        catalog = AgentCatalog.refresh(connection, transport=catalog_transport(snapshot(version, tools=() if provider_type is CodexProvider else None)))
        monkeypatch.setattr("galaius.agents.run.load_policy", lambda: Policy.from_catalog(catalog))
        handle = await run_agent(provider_type(), "bounded task", cwd=str(catalog_home), agent="fixture-worker", mesh=False)
        await asyncio.wait_for(handle.wait(), 10)
        assert handle.criterion == f"price.in >= {version}"
    prompt = lambda entry: entry[1].get("agent_prompt", entry[0])
    assert "primary instruction revision 1" in prompt(captured[0])
    assert "primary instruction revision 2" in prompt(captured[1])
    assert captured[1][1]["reasoning"] == "low"
    assert captured[1][1]["allowed_tools"] == ([] if provider_type is CodexProvider else ["Write"])
    if provider_type is ClaudeCodeProvider:
        assert captured[1][1]["agent"] == "fixture-worker"
        assert "--agents" in captured[1][2] and "--allowedTools" not in captured[1][2]
    else:
        assert captured[1][1]["agent"] == "fixture-worker" and "--agent" not in captured[1][2]
        actual_task = captured[1][2][-1]  # the task is last, after "--"
        assert actual_task.count("AGENT_ROLE:") == 1
        assert actual_task.count("Delegated task:") == 1
        assert "bounded task" in actual_task


@pytest.mark.parametrize("endpoint", [
    "https://example.com", "http://127.0.0.1.example.com", "http://localhost.example.com",
    "http://user@localhost", "http://localhost/path", "http://localhost?credential=value",
])
def test_preview_only_accepts_explicit_loopback_origins(endpoint):
    with pytest.raises(ValidationError):
        CatalogConnection(endpoint=endpoint, auth_mode="preview")


@pytest.mark.parametrize("endpoint", ["http://localhost:8767", "http://127.0.0.1:8767", "http://[::1]:8767"])
def test_preview_bootstrap_keeps_session_separate_from_connection(endpoint, tmp_path, catalog_home):
    workspace = uuid4()
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        assert request.headers["Origin"] == endpoint
        if request.url.path == "/v1/auth/local-preview":
            return httpx.Response(200, headers={"Set-Cookie": "session=fixture-cookie; Path=/; HttpOnly"}, json={})
        assert request.headers["Cookie"] == "session=fixture-cookie"
        return httpx.Response(200, json={
            "account": {"account_id": str(uuid4()), "email": "fixture@example.com", "locale": "en", "verified": True},
            "workspaces": [{"workspace_id": str(workspace), "name": "Fixture", "role": "owner"}],
            "current_workspace_id": str(workspace), "csrf_token": "fixture-csrf",
            "session_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        })

    connection = CatalogConnection(endpoint=endpoint, auth_mode="preview")
    with connection.connect(transport=httpx.MockTransport(respond)) as client:
        resolved = connection.authenticate(client)
    assert resolved.workspace_id == workspace
    target = tmp_path / "connection.json"
    resolved.save(target)
    assert CatalogConnection.load(target) == resolved
    assert "fixture-cookie" not in target.read_text()
    assert "fixture-csrf" not in target.read_text()
    assert requests == [("POST", "/v1/auth/local-preview"), ("GET", "/v1/bootstrap")]


@pytest.mark.parametrize("status", [401, 403, 302, 500])
def test_auth_and_redirect_failures_are_not_network_fallbacks(status, catalog_home):
    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode="preview")
    expected = CatalogAuthenticationError if status in {401, 403} else CatalogConnectionError
    with connection.connect(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
        with pytest.raises(expected):
            connection.authenticate(client)


def test_token_mode_reuses_protected_file_reader_without_bootstrap(tmp_path):
    token_path = tmp_path / "token"
    token_path.write_text("synthetic-token")
    token_path.chmod(0o600)
    connection = CatalogConnection(
        endpoint="https://example.com", auth_mode="token", token_file=token_path,
        workspace_id=uuid4(),
    )
    requests = []

    def respond(request):
        requests.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        return httpx.Response(200, json={})

    with connection.connect(transport=httpx.MockTransport(respond)) as client:
        assert connection.authenticate(client) == connection
        connection.request(client, "GET", f"/v1/workspaces/{connection.workspace_id}/agent-catalog")
    assert len(requests) == 1
    target = tmp_path / "connection.json"
    connection.save(target)
    assert "synthetic-token" not in target.read_text()


@pytest.mark.parametrize("saved", ["preview", "machine"])
def test_a_linked_pc_reads_the_catalog_with_its_own_link_and_follows_a_relink(linked_pc, saved):
    """The loopback preview sign-in is gone from every server: a PC whose saved connection still
    names it, or names its link, reads with its machine token; a re-link (new token, new company)
    is followed with no step; an unlinked PC is told to link again, in words."""
    from galaius.machines import MachineRunner

    CatalogConnection(endpoint="http://127.0.0.1:8767", workspace_id=uuid4(), auth_mode=saved).save()
    connection = CatalogConnection.load()
    assert (connection.auth_mode, connection.workspace_id) == ("machine", linked_pc.workspace_id)
    catalog = AgentCatalog.refresh(connection, transport=machine_transport(snapshot(1), linked_pc.token.get_secret_value()))
    assert catalog.snapshot.agents
    relinked = linked_pc.model_copy(update={"workspace_id": uuid4(), "token": SecretStr("iwm_" + "b" * 48)})
    MachineRunner().save(relinked)
    connection = CatalogConnection.load()
    assert connection.workspace_id == relinked.workspace_id
    AgentCatalog.refresh(connection, transport=machine_transport(snapshot(2), "iwm_" + "b" * 48))
    with pytest.raises(CatalogAuthenticationError, match="galaius login"):
        AgentCatalog.refresh(connection, transport=machine_transport(snapshot(2), "iwm_" + "b" * 48, status=401))
    MachineRunner().config_path.unlink()
    assert CatalogConnection.load() is None if saved == "machine" else CatalogConnection.load().auth_mode == "preview"
    with pytest.raises(CatalogAuthenticationError, match="galaius login"):
        AgentCatalog.refresh(connection)  # a machine connection held in hand: the link is gone


def test_a_linked_pc_whose_key_is_revoked_reads_the_catalog_through_its_link(linked_pc, catalog_home):
    """A PC given a key (`galaius login`) keeps starting agents after that key is revoked: the
    refused key falls back to the PC's own link on the same server; another server's key does not."""
    key = catalog_home / "key"
    PRIVATE_FILES.write_secret(key, "iwk_" + "r" * 40)
    def server(request):
        if request.headers["Authorization"] == "Bearer iwk_" + "r" * 40:
            return httpx.Response(401)
        assert request.headers["Authorization"] == f"Bearer {linked_pc.token.get_secret_value()}"
        return httpx.Response(200, content=snapshot(1).model_dump_json())
    revoked = CatalogConnection(endpoint="http://127.0.0.1:8767", workspace_id=uuid4(), auth_mode="token", token_file=key)
    catalog = AgentCatalog.refresh(revoked, transport=httpx.MockTransport(server))
    assert (catalog.connection.auth_mode, catalog.connection.workspace_id) == ("machine", linked_pc.workspace_id)
    elsewhere = CatalogConnection(endpoint="https://other.example", workspace_id=uuid4(), auth_mode="token", token_file=key)
    with pytest.raises(CatalogAuthenticationError):
        AgentCatalog.refresh(elsewhere, transport=httpx.MockTransport(server))


def test_a_linked_pc_keeps_no_catalog_file_so_nothing_rewrites_what_running_processes_read(linked_pc, capsys):
    """The PC link is the only record of a machine connection: syncing removes a stale file instead of
    writing a mode older running galaius processes cannot read; with no file the link is used."""
    CatalogConnection(endpoint="http://127.0.0.1:8767", workspace_id=uuid4(), auth_mode="preview").save()
    connection = CatalogConnection.load()
    assert connection.auth_mode == "machine"
    connection.save()
    assert not CatalogConnection.path().exists()
    assert CatalogConnection.load() == connection


@pytest.mark.parametrize(("payload", "said"), [
    ('{"endpoint":"http://127.0.0.1:8767","workspace_id":"%s","auth_mode":"passkey"}' % uuid4(), "written by a newer galaius; restart this process"),
    ('{"auth_mode":"token"}', "invalid catalog connection configuration"),
    ("not json", "invalid catalog connection configuration"),
])
def test_an_unreadable_catalog_file_names_why_and_never_breaks_local_settings(catalog_home, payload, said):
    from galaius.server_tool_settings import ServerToolSettings

    CatalogConnection.replace_text(CatalogConnection.path(), payload)
    with pytest.raises(CatalogConnectionError, match=said):
        CatalogConnection.load()
    UserConfig.PATH.write_text("GALAIUS_EXAMPLE=kept\n")
    assert ServerToolSettings.configured() is None
    assert UserConfig.read()["GALAIUS_EXAMPLE"] == "kept"


def test_corrupt_configuration_never_becomes_unconfigured(tmp_path):
    target = tmp_path / "connection.json"
    assert CatalogConnection.load(target) is None
    target.write_text('{"auth_mode":"preview"}')
    with pytest.raises(CatalogConnectionError):
        CatalogConnection.load(target)
    connection = CatalogConnection(endpoint="http://localhost:8767", auth_mode="preview")
    with pytest.raises(CatalogConnectionError):
        connection.save(target)
    assert json.loads(target.read_text()) == {"auth_mode": "preview"}


def machine_transport(value, token: str, status=200):
    def respond(request):
        assert request.headers["Authorization"] == f"Bearer {token}"
        return httpx.Response(status, content=value.model_dump_json())
    return httpx.MockTransport(respond)


@pytest.fixture
def linked_pc(catalog_home, monkeypatch):
    """This PC linked to a server: its machine link carries server, company and machine token."""
    from galaius.machines import MachineConfig, MachineRunner

    monkeypatch.setenv("XDG_CONFIG_HOME", str(catalog_home / "xdg"))
    link = MachineConfig(server_url="http://127.0.0.1:8767", workspace_id=uuid4(), machine_id=uuid4(), token="iwm_" + "a" * 48,
                         permission_ceiling="read_only", working_directory=catalog_home)
    MachineRunner().save(link)
    return link


def test_cli_sync_persists_connection_and_rebuilds_deleted_cache(linked_pc, catalog_home, monkeypatch, capsys):
    workspace = linked_pc.workspace_id
    values = [snapshot(1), snapshot(2)]
    current = [values[0]]
    token = [linked_pc.token.get_secret_value()]
    connect = CatalogConnection.connect
    monkeypatch.setattr(CatalogConnection, "connect", lambda self, **kwargs: connect(self, transport=machine_transport(current[0], token[0])))
    with pytest.raises(SystemExit) as first:
        app(["agents", "sync", "--machine"])
    assert first.value.code == 0
    assert json.loads(capsys.readouterr().out)["cursor"] == values[0].cursor
    assert CatalogConnection.load().workspace_id == workspace
    current[0] = values[1]
    with pytest.raises(SystemExit) as second:
        app(["agents", "sync"])
    assert second.value.code == 0
    assert json.loads(capsys.readouterr().out)["cursor"] == values[1].cursor
    AgentCatalog.cache_path().unlink()
    with pytest.raises(SystemExit) as rebuilt:
        app(["agents", "sync"])
    assert rebuilt.value.code == 0
    assert json.loads(capsys.readouterr().out)["cursor"] == values[1].cursor
    assert ClaudeCodeProvider().agent_definitions() == ["fixture-lead", "fixture-worker"]
    assert CodexProvider().valid_definition("fixture-worker")
    assert not CodexProvider().valid_definition("../../fixture-worker")
    definition_path = CodexProvider().definition_path("fixture-worker")
    assert definition_path.is_relative_to(catalog_home)
    assert "primary instruction revision 2" in definition_path.read_text()


def test_configured_policy_ignores_local_role_rules_and_preserves_provider_switches(catalog_home, monkeypatch):
    value = AgentCatalog(
        connection=CatalogConnection(endpoint="http://localhost:8767", auth_mode="preview", workspace_id=uuid4()),
        snapshot=CatalogSnapshot.model_validate(snapshot().model_dump()), fetched_at=datetime.now(UTC),
    )
    (catalog_home / "agents.json").write_text(json.dumps({
        "agents": {"fixture-worker": "@missing-local-profile"},
        "reasoning": {"fixture-worker": "low"}, "providers": {"codex": False},
    }))
    monkeypatch.setattr(AgentCatalog, "active", classmethod(lambda cls: value))
    policy = Policy.load()
    assert policy.criterion_for("fixture-worker") == "price.in >= 1"
    assert policy.reasoning_for("fixture-worker") == "high"
    assert not policy.provider_active("codex")
    with pytest.raises(ValueError, match="Server catalog"):
        policy.assign_paradigm("fixture-worker", "local-only", "skill")


def test_native_hook_composes_server_role_without_local_authoring(catalog_home, monkeypatch):
    catalog = AgentCatalog(
        connection=CatalogConnection(endpoint="http://localhost:8767", auth_mode="preview", workspace_id=uuid4()),
        snapshot=CatalogSnapshot.model_validate(snapshot(2).model_dump()), fetched_at=datetime.now(UTC),
    )
    monkeypatch.setattr(AgentCatalog, "active", classmethod(lambda cls: catalog))
    monkeypatch.setattr(codex_policy_hook, "_select", lambda policy, role: (
        "fixture-model", policy.reasoning_for(role), policy.criterion_for(role), "benchmark evidence unrecorded",
    ))
    output = codex_policy_hook.handle({"hook_event_name": "PreToolUse", "tool_input": {
        "message": "AGENT_ROLE: fixture-worker\nPerform the bounded task", "agent_type": "default",
    }})
    updated = output["hookSpecificOutput"]["updatedInput"]
    assert updated["reasoning_effort"] == "low"
    assert "primary instruction revision 2" in updated["message"]
    assert "Skill: fixture/skill" in updated["message"]
    assert "Perform the bounded task" in updated["message"]
    assert "Catalog cursor:" in codex_policy_hook.handle({"hook_event_name": "SessionStart"})["hookSpecificOutput"]["additionalContext"]


def test_launch_uses_body_only_and_skill_paths_keep_pinned_content(catalog_home):
    original = snapshot()
    instructions = []
    references = {}
    for prompt in original.paradigms:
        content = f"---\r\nname: Metadata only\r\nmodel: ignored-pin\r\n---\r\n{prompt.content}\r\n"
        revised = PromptRevision(**{
            **prompt.model_dump(), "content": content,
            "digest": hashlib.sha256(content.encode()).hexdigest(),
        })
        instructions.append(revised)
        references[prompt.revision] = PromptExecutionRef(
            key=revised.key, revision=revised.revision, digest=revised.digest, channel="stable",
        )
    agents = tuple(agent.model_copy(update={
        "prompt": references[agent.prompt.revision],
        "paradigms": tuple(references[value.revision] for value in agent.paradigms),
        "skill_paradigms": tuple(references[value.revision] for value in agent.skill_paradigms),
    }) for agent in original.agents)
    connection = CatalogConnection(endpoint="http://localhost:8767", auth_mode="preview", workspace_id=uuid4())
    catalog = AgentCatalog.refresh(connection, transport=catalog_transport(AgentCatalogSnapshot.create(agents, tuple(instructions))))
    launch = catalog.definition("fixture-worker", "Do the bounded task")
    assert "Metadata only" not in launch and "ignored-pin" not in launch
    assert "primary instruction revision 1" in launch
    assert "first instruction revision 1" in launch and "second instruction revision 1" in launch
    assert "skill instruction revision 1" not in launch
    path = catalog.skill_path(catalog.snapshot.role("fixture-worker").skill_paradigms[0])
    assert str(path) in launch
    skill_content = path.read_bytes()
    assert b"skill instruction revision 1" in skill_content
    assert hashlib.sha256(skill_content).hexdigest() == catalog.snapshot.role("fixture-worker").skill_paradigms[0].digest
    replacement = AgentCatalog.refresh(connection, transport=catalog_transport(snapshot(2)))
    replacement.definition("fixture-worker", "Next task")
    assert path.read_bytes() == skill_content
    path.unlink()
    catalog.definition("fixture-worker", "Resume old snapshot")
    assert path.read_bytes() == skill_content


def test_team_context_keeps_pinned_delegate_revision_when_head_advances(catalog_home):
    initial = snapshot()
    lead = next(value for value in initial.agents if value.role_key == "fixture-lead")
    worker = next(value for value in initial.agents if value.role_key == "fixture-worker")
    revised_worker = worker.model_copy(update={"revision": uuid4(), "parent_revision": worker.revision})
    value = CatalogSnapshot.model_validate_json(AgentCatalogSnapshot.create((lead, revised_worker), initial.paradigms).model_dump_json())
    text = value.definition("fixture-lead", {})
    assert f"Server capability revision: {worker.revision}" in text
    assert str(revised_worker.revision) not in text
    assert value.role("fixture-lead").capabilities[0].agent.revision == worker.revision
    historical_only = CatalogSnapshot.model_validate_json(AgentCatalogSnapshot.create((lead,), initial.paradigms).model_dump_json())
    unavailable = historical_only.definition("fixture-lead", {})
    assert str(worker.id) in unavailable and str(worker.revision) in unavailable
    assert "active role unavailable" in unavailable
    with pytest.raises(CatalogConnectionError, match="absent"):
        historical_only.role("fixture-worker")


@pytest.mark.parametrize(("content", "body"), [
    ("Plain instruction", "Plain instruction"),
    ("---\n---\nEmpty metadata", "Empty metadata"),
    ("---  \nname: ignored\n--- \nBody\n\n---\nKeep body separator", "Body\n\n---\nKeep body separator"),
    ("\ufeff---\r\nname: ignored\r\n---\r\nBody", "Body"),
])
def test_instruction_body_strips_only_leading_frontmatter(content, body):
    prompt = PromptRevision(key=PromptKey(namespace="fixture", slug="body"), revision=uuid4(),
                            content=content, digest=hashlib.sha256(content.encode()).hexdigest(),
                            source_commit="a" * 40, created_at=datetime.now(UTC))
    assert AgentInstructionSet.instruction_body(prompt) == body


@pytest.fixture
def advanced_catalogs(catalog_home):
    previous = snapshot(1)
    lead = next(value for value in previous.agents if value.role_key == "fixture-lead")
    worker = next(value for value in previous.agents if value.role_key == "fixture-worker")
    update = snapshot(2)
    newer = next(value for value in update.agents if value.role_key == "fixture-worker").model_copy(update={
        "id": worker.id, "parent_revision": worker.revision, "reports_to": lead.id,
    })
    newer_lead = lead.model_copy(update={
        "revision": uuid4(), "parent_revision": lead.revision, "prompt": newer.prompt,
        "capabilities": (lead.capabilities[0].model_copy(update={
            "agent": AgentRevisionRef(id=newer.id, revision=newer.revision),
        }),),
    })
    connection = CatalogConnection(endpoint="http://localhost:8767", auth_mode="preview", workspace_id=uuid4())
    old = AgentCatalog(connection=connection, snapshot=CatalogSnapshot.model_validate_json(previous.model_dump_json()), fetched_at=datetime.now(UTC))
    current = AgentCatalog(connection=connection, snapshot=CatalogSnapshot.model_validate_json(
        AgentCatalogSnapshot.create((newer_lead, newer), update.paradigms).model_dump_json()), fetched_at=datetime.now(UTC))
    return old, current, AgentRevisionRef(id=worker.id, revision=worker.revision), AgentRevisionRef(id=lead.id, revision=lead.revision)


@pytest.fixture
def historical_http(advanced_catalogs, monkeypatch):
    old, current, _, _ = advanced_catalogs
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/v1/auth/local-preview":
            return httpx.Response(200, headers={"Set-Cookie": "session=fixture-cookie; Path=/"}, json={})
        assert request.headers.get("Cookie") == "session=fixture-cookie" or request.headers.get("Authorization") == "Bearer fixture-token"
        if request.url.path.endswith("/agent-catalog"):
            return httpx.Response(200, content=current.snapshot.model_dump_json())
        for catalog in (old, current):
            for agent in catalog.snapshot.agents:
                if request.url.path.endswith(f"/agents/{agent.id}/revisions/{agent.revision}"):
                    return httpx.Response(200, content=agent.model_dump_json())
            for prompt in catalog.snapshot.paradigms:
                if request.url.path.endswith(f"/prompts/{prompt.key.namespace}/{prompt.key.slug}") and request.url.params.get("digest") == prompt.digest:
                    return httpx.Response(200, content=prompt.model_dump_json())
        return httpx.Response(404)

    connect = CatalogConnection.connect
    monkeypatch.setattr(CatalogConnection, "connect", lambda self, **kwargs: connect(self, **{"transport": httpx.MockTransport(respond), **kwargs}))
    CatalogConnection.replace_text(AgentCatalog.cache_path(), current.model_dump_json())
    return requests


def test_delegation_pin_never_falls_back_to_advanced_head(advanced_catalogs, historical_http):
    old, current, worker_ref, lead_ref = advanced_catalogs
    pinned, role = Policy.from_catalog(current).for_launch("fixture-worker", parent=lead_ref)
    assert pinned.catalog.revision(worker_ref).revision == worker_ref.revision
    assert pinned.criterion_for(role) == "price.in >= 1"
    assert pinned.catalog.snapshot == current.snapshot
    assert pinned.catalog.snapshot.cursor == current.snapshot.cursor
    assert sum(request.method == "POST" for request in historical_http) == 1
    assert AgentCatalog.model_validate_json(AgentCatalog.cache_path().read_bytes()).snapshot == current.snapshot
    assert pinned.catalog.model_dump_json() == current.model_dump_json()
    unpinned, role = Policy.from_catalog(current).for_launch("fixture-worker")
    assert unpinned.criterion_for(role) == "price.in >= 2"
    selected, role = Policy.from_catalog(old).for_launch(None, parent=lead_ref, capability="ask_worker")
    assert role == "fixture-worker" and selected.catalog.revision(worker_ref).revision == worker_ref.revision
    newer = current.snapshot.role("fixture-worker")
    with pytest.raises(ValueError, match="conflicts"):
        Policy.from_catalog(current).for_launch("fixture-worker", parent=lead_ref,
            reference=AgentRevisionRef(id=newer.id, revision=newer.revision))
    with pytest.raises(ValueError, match="no delegate"):
        Policy.from_catalog(current).for_launch(None, parent=lead_ref, capability="missing")
    with pytest.raises(ValueError, match="configured server catalog"):
        Policy().for_launch("fixture-worker", parent=lead_ref)


@pytest.mark.parametrize("damage", ["agent-id", "agent-revision", "criteria", "prompt-key", "prompt-revision", "prompt-digest", "prompt-content"])
def test_historical_response_must_match_every_exact_reference(advanced_catalogs, monkeypatch, damage):
    old, current, worker_ref, _ = advanced_catalogs
    agent = old.snapshot.revision(worker_ref)
    before = current.model_dump_json()
    CatalogConnection.replace_text(AgentCatalog.cache_path(), before)

    def respond(request):
        if request.url.path == "/v1/auth/local-preview":
            return httpx.Response(200, json={})
        if "/agents/" in request.url.path:
            value = agent.model_dump(mode="json")
            if damage == "agent-id":
                value["id"] = str(uuid4())
            elif damage == "agent-revision":
                value["revision"] = str(uuid4())
            elif damage == "criteria":
                value["criteria"] = "invalid! criterion"
        else:
            value = next(prompt for prompt in old.snapshot.paradigms if prompt.digest == request.url.params["digest"]).model_dump(mode="json")
            if damage == "prompt-key":
                value["key"]["slug"] = "wrong-prompt"
            elif damage == "prompt-revision":
                value["revision"] = str(uuid4())
            elif damage == "prompt-digest":
                value["digest"] = "0" * 64
            elif damage == "prompt-content":
                value["content"] = "wrong content"
        return httpx.Response(200, json=value)

    connect = CatalogConnection.connect
    monkeypatch.setattr(CatalogConnection, "connect", lambda self: connect(self, transport=httpx.MockTransport(respond)))
    with pytest.raises(CatalogConnectionError):
        current.at_revision(worker_ref)
    assert AgentCatalog.cache_path().read_text() == before
    assert not (AgentCatalog.cache_path().parent / "agent-catalog-definitions").exists()


@pytest.mark.parametrize("status", [401, 403, 404])
@pytest.mark.parametrize("endpoint", ["agent", "prompt"])
def test_exact_read_refusal_invalidates_cache_before_later_outage(advanced_catalogs, monkeypatch, status, endpoint):
    old, current, worker_ref, _ = advanced_catalogs
    CatalogConnection.replace_text(AgentCatalog.cache_path(), current.model_dump_json())

    def respond(request):
        if request.url.path == "/v1/auth/local-preview":
            return httpx.Response(200, json={})
        if endpoint == "prompt" and "/agents/" in request.url.path:
            return httpx.Response(200, content=old.snapshot.revision(worker_ref).model_dump_json())
        return httpx.Response(status)

    connect = CatalogConnection.connect
    monkeypatch.setattr(CatalogConnection, "connect", lambda self: connect(self, transport=httpx.MockTransport(respond)))
    with pytest.raises(CatalogAuthenticationError):
        current.at_revision(worker_ref)
    assert not AgentCatalog.cache_path().exists()

    def offline(request):
        raise httpx.ConnectError("fixture outage")

    monkeypatch.setattr(CatalogConnection, "connect", lambda self, **kwargs: connect(self, transport=httpx.MockTransport(offline)))
    with pytest.raises(CatalogConnectionError, match="no validated cache"):
        AgentCatalog.refresh(current.connection, allow_stale=True)
    with pytest.raises(CatalogAuthenticationError, match="invalidated"):
        current.at_revision(worker_ref)


def test_network_cannot_substitute_latest_for_uncached_pin(advanced_catalogs, monkeypatch, capsys):
    old, current, worker_ref, _ = advanced_catalogs
    CatalogConnection.replace_text(AgentCatalog.cache_path(), current.model_dump_json())

    def offline(request):
        raise httpx.ConnectError("fixture outage")

    connect = CatalogConnection.connect
    monkeypatch.setattr(CatalogConnection, "connect", lambda self, **kwargs: connect(self, transport=httpx.MockTransport(offline)))
    stale = AgentCatalog.refresh(current.connection, allow_stale=True)
    assert stale.stale and "STALE" in capsys.readouterr().err
    with pytest.raises(CatalogConnectionError, match="latest revision is not a fallback"):
        stale.at_revision(worker_ref)
    current_worker = current.snapshot.role("fixture-worker")
    assert stale.at_revision(AgentRevisionRef(id=current_worker.id, revision=current_worker.revision)).stale


def test_historical_token_reads_reuse_standard_auth(advanced_catalogs, historical_http, catalog_home):
    _, current, worker_ref, _ = advanced_catalogs
    token = catalog_home / "fixture-token"
    token.write_text("fixture-token")
    token.chmod(0o600)
    connection = current.connection.model_copy(update={"auth_mode": "token", "token_file": token})
    selected = current.model_copy(update={"connection": connection}).at_revision(worker_ref)
    assert selected.role("fixture-worker").criteria == "price.in >= 1"
    assert all(request.method == "GET" and request.headers["Authorization"] == "Bearer fixture-token" for request in historical_http)


def test_head_instruction_validation_precedes_lazy_file_exports(catalog_home):
    value = snapshot()
    worker = next(agent for agent in value.agents if agent.role_key == "fixture-worker")
    original = next(prompt for prompt in value.paradigms if prompt.revision == worker.prompt.revision)
    broken = PromptRevision.model_validate({**original.model_dump(), "content": "---\nbroken header", "digest": hashlib.sha256(b"---\nbroken header").hexdigest()})
    reference = worker.prompt.model_copy(update={"digest": broken.digest})
    agents = tuple(agent.model_copy(update={"prompt": reference}) for agent in value.agents)
    prompts = tuple(broken if prompt.revision == original.revision else prompt for prompt in value.paradigms)
    catalog = AgentCatalog(connection=CatalogConnection(endpoint="http://localhost:8767", auth_mode="preview", workspace_id=uuid4()),
        snapshot=CatalogSnapshot.model_validate_json(AgentCatalogSnapshot.create(agents, prompts).model_dump_json()), fetched_at=datetime.now(UTC))
    with pytest.raises(ValidationError, match="unterminated frontmatter"):
        catalog.definition("fixture-worker", "Bounded task")
    assert not (catalog_home / "agent-catalog-skills").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [False, True])
async def test_missing_parent_revision_cannot_silently_use_latest_policy(advanced_catalogs, catalog_home, monkeypatch, recorded):
    _, current, _, _ = advanced_catalogs
    monkeypatch.setattr("galaius.agents.run.load_policy", lambda: Policy.from_catalog(current))
    monkeypatch.setattr(CodexProvider, "available", lambda self: True)
    if recorded:
        reg.register(run_id="fixture-parent", pid=None, provider="codex", name="Fixture parent", cwd=str(catalog_home))
    with pytest.raises(ModelUnavailable, match="Parent run has no recorded server revision"):
        await run_agent(CodexProvider(), "Bounded task", agent="fixture-worker", parent_run_id="fixture-parent", cwd=str(catalog_home))


@pytest.mark.asyncio
@pytest.mark.parametrize("by_capability", [False, True])
async def test_local_delegation_and_resume_run_exact_parent_pin(advanced_catalogs, historical_http, catalog_home, monkeypatch, by_capability):
    old, current, worker_ref, lead_ref = advanced_catalogs
    monkeypatch.setattr("galaius.agents.run.load_policy", lambda: Policy.from_catalog(current))
    parent = reg.register(run_id=str(uuid4()), pid=None, provider="codex", name="Fixture lead",
                          agent="fixture-lead", agent_ref=lead_ref, cwd=str(catalog_home))
    captured = []
    original_command = ClaudeCodeProvider.command

    def command(self, task, **kwargs):
        captured.append((task, kwargs, original_command(self, task, **kwargs)))
        return [sys.executable, "-c", "import json, os; print(json.dumps({'run': os.environ.get('GALAIUS_RUN_ID'), 'parent': os.environ.get('GALAIUS_PARENT_RUN_ID')}))"]

    monkeypatch.setattr(ClaudeCodeProvider, "available", lambda self: True)
    monkeypatch.setattr(ClaudeCodeProvider, "command", command)
    monkeypatch.setattr(ClaudeCodeProvider, "resume_command", lambda self, session_id, task, **kwargs: command(self, task, cwd=str(catalog_home), run_id="resume", **kwargs))
    monkeypatch.setattr("galaius.agents.run.resolve_model", lambda rule, env, **kwargs: ({}, f"fixture-model-{rule.rsplit(' ', 1)[-1]}"))
    # The ranked list is stubbed at its entry point for the same reason the resolver is: the
    # fixture role weights `gui.screenspot`, which no catalog row scores, so a real ranking finds
    # nothing — and a real launch is not what these tests measure.
    monkeypatch.setattr("galaius.agents.run.rank_candidates", lambda rule, env, *, providers, weights="", role=None: (
        reg.LaunchCandidate(provider=providers[0].name, model=rule, rank=0),))
    handle = await run_agent(ClaudeCodeProvider(), "Pinned task", agent=None if by_capability else "fixture-worker",
        delegate="ask_worker" if by_capability else None, parent_run_id=parent.run_id, cwd=str(catalog_home), mesh=False)
    await asyncio.wait_for(handle.wait(), 10)
    saved = reg.get_run(handle.run_id)
    assert saved.agent_ref == worker_ref
    assert saved.name == "fixture-worker"
    assert saved.model == "fixture-model-1" and saved.reasoning == "high"
    assert "primary instruction revision 1" in captured[0][1]["agent_prompt"]
    assert "primary instruction revision 2" not in captured[0][1]["agent_prompt"]
    assert captured[0][1]["allowed_tools"] == ["Read"]
    assert str(worker_ref.revision) in saved.definition_path
    assert "primary instruction revision 1" in Path(saved.definition_path).read_text()
    resumed = launch_continuation(ClaudeCodeProvider(), saved, "fixture-session", "Continue pinned work",
        model="wrong-new-model", criterion="price.in >= 2", reasoning="low", raw_index=0)
    assert resumed.wait() == 0
    resumed_environment = json.loads(reg.raw_events_path(saved.run_id).read_text().splitlines()[-1])
    assert resumed_environment == {"run": saved.run_id, "parent": saved.run_id}
    assert "primary instruction revision 1" in captured[1][1]["agent_prompt"]
    assert captured[1][1]["model"] == "fixture-model-1" and captured[1][1]["reasoning"] == "high"
    assert any(request.url.path.endswith(f"/agents/{worker_ref.id}/revisions/{worker_ref.revision}") for request in historical_http)
    assert any(request.url.path.endswith(f"/agents/{lead_ref.id}/revisions/{lead_ref.revision}") for request in historical_http)
    assert any(request.url.params.get("digest") == old.snapshot.role("fixture-worker").paradigms[0].digest for request in historical_http)


@pytest.mark.parametrize("command", ["spawn", "run"])
def test_cli_parses_exact_reference_and_delegate_parent(catalog_home, monkeypatch, command):
    identity, revision = uuid4(), uuid4()
    calls = []

    async def finished():
        return 0

    async def launch(provider, task, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(run_id="fixture-run", process=SimpleNamespace(returncode=0), wait=finished)

    monkeypatch.setattr(app_commands, "_agent_runner", lambda: launch)
    monkeypatch.setattr(app_commands, "run_agent", launch)
    with pytest.raises(SystemExit) as result:
        app(["agents", command, "Bounded task", "--agent-id", str(identity), "--agent-revision", str(revision),
             "--delegate", "ask_worker", "--parent-run-id", "fixture-parent"])
    assert result.value.code == 0
    assert calls[0]["agent_ref"] == AgentRevisionRef(id=identity, revision=revision)
    assert calls[0]["name"] is None  # The common resolver names the selected server role.
    assert calls[0]["delegate"] == "ask_worker" and calls[0]["parent_run_id"] == "fixture-parent"


@pytest.mark.parametrize("flag", ["--agent-id", "--agent-revision"])
def test_cli_refuses_half_a_revision_pair_before_launch(catalog_home, monkeypatch, capsys, flag):
    async def forbidden(*args, **kwargs):
        pytest.fail("incomplete revision reached launch")

    monkeypatch.setattr(app_commands, "_agent_runner", lambda: forbidden)
    with pytest.raises(SystemExit) as result:
        app(["agents", "spawn", "Bounded task", flag, str(uuid4())])
    assert result.value.code == 2
    assert "both --agent-id and --agent-revision" in capsys.readouterr().err


def test_preview_session_reused_for_fifty_independent_connections(catalog_home):
    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode="preview", workspace_id=uuid4())
    value = snapshot()
    calls = []
    transport = catalog_transport(value)

    def respond(request):
        calls.append(request.url.path)
        return transport.handle_request(request)

    for _ in range(50):
        fresh = CatalogConnection.model_validate_json(connection.model_dump_json())
        AgentCatalog.refresh(fresh, transport=httpx.MockTransport(respond))
    assert calls.count("/v1/auth/local-preview") == 1


def test_concurrent_processes_share_one_preview_login(catalog_home):
    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode="preview", workspace_id=uuid4())
    login_log = catalog_home / "login-count"

    children = [PROCESSES.Process(target=share_preview_login, args=(connection.model_dump_json(), str(login_log), str(UserConfig.PATH))) for _ in range(RACERS)]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(timeout=120)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
    assert login_log.read_text().splitlines() == ["login"]
    PRIVATE_FILES.check(connection.session_path())
    PRIVATE_FILES.check(connection.session_path().parent)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_preview_refusal_discards_session_and_requires_fresh_login(catalog_home, status):
    connection = CatalogConnection(endpoint="http://localhost:8767", auth_mode="preview", workspace_id=uuid4())
    value = snapshot()
    calls = []
    transport = catalog_transport(value)

    def respond(request):
        calls.append(request.method)
        return transport.handle_request(request)

    AgentCatalog.refresh(connection, transport=httpx.MockTransport(respond))
    with pytest.raises(CatalogAuthenticationError):
        AgentCatalog.refresh(connection, allow_stale=True, transport=catalog_transport(value, status))
    assert not connection.session_path().exists()
    assert not AgentCatalog.cache_path().exists()
    AgentCatalog.refresh(connection, transport=httpx.MockTransport(respond))
    assert calls == ["POST", "GET", "POST", "GET"]


def test_expired_cookie_relogs_and_connections_do_not_share_sessions(catalog_home):
    connection = CatalogConnection(endpoint="http://localhost:8767", auth_mode="preview", workspace_id=uuid4())
    value = snapshot()
    calls = []
    transport = catalog_transport(value)

    def respond(request):
        calls.append(request.method)
        return transport.handle_request(request)

    observed = httpx.MockTransport(respond)
    AgentCatalog.refresh(connection, transport=observed)
    jar = LWPCookieJar(connection.session_path())
    jar.load(ignore_discard=True)
    for cookie in jar:
        cookie.expires = 1
    jar.save(ignore_discard=True, ignore_expires=True)
    AgentCatalog.refresh(connection, transport=observed)
    other_workspace = connection.model_copy(update={"workspace_id": uuid4()})
    AgentCatalog.refresh(other_workspace, transport=observed)
    other_origin = connection.model_copy(update={"endpoint": "http://localhost:8768"})
    AgentCatalog.refresh(other_origin, transport=observed)
    assert calls.count("POST") == 4


@pytest.mark.parametrize("damage", ["public-file", "public-directory", "symlink", "corrupt"])
def test_private_session_cache_rejects_unsafe_files_without_printing_cookies(catalog_home, damage, recwarn, capsys):
    connection = CatalogConnection(endpoint="http://localhost:8767", auth_mode="preview", workspace_id=uuid4())
    value = snapshot()
    AgentCatalog.refresh(connection, transport=catalog_transport(value))
    path = connection.session_path()
    if damage == "public-file":
        loosen(path)
    elif damage == "public-directory":
        loosen(path.parent)
    elif damage == "symlink":
        saved = path.with_suffix(".saved")
        path.rename(saved)
        path.symlink_to(saved)
    else:
        path.write_text('#LWP-Cookies-2.0\nSet-Cookie3: session=fixture-cookie; version="fixture-cookie"\n')
        AgentCatalog.refresh(connection, transport=catalog_transport(value))
        assert not recwarn
        assert "fixture-cookie" not in capsys.readouterr().err
        return
    with pytest.raises(CatalogConnectionError, match="must be private"):
        AgentCatalog.refresh(connection, transport=catalog_transport(value))


@pytest.mark.parametrize("status", [401, 403, 404])
@pytest.mark.parametrize("separate_process", [False, True])
def test_denial_prevents_older_refresh_restoring_stale_access(catalog_home, status, separate_process):
    connection = CatalogConnection(endpoint="http://localhost:8767", auth_mode="preview", workspace_id=uuid4())
    value = snapshot()
    AgentCatalog.refresh(connection, transport=catalog_transport(value))
    previous = AgentCatalog.cache_path().read_bytes()
    successful = catalog_transport(value)

    def race(request):
        if request.method == "GET":
            # A's successful response began before B denied access, but arrives last.
            response = successful.handle_request(request)
            if separate_process:
                child = PROCESSES.Process(target=deny_catalog, args=(connection.model_dump_json(), value, status, str(UserConfig.PATH)))
                child.start()
                try:
                    child.join(timeout=60)
                    assert child.exitcode == 0
                finally:
                    if child.is_alive():
                        child.terminate()
                        child.join(timeout=5)
            else:
                deny_catalog(connection.model_dump_json(), value, status, str(UserConfig.PATH))
            return response
        return successful.handle_request(request)

    with pytest.raises(CatalogAuthenticationError):
        AgentCatalog.refresh(connection, transport=httpx.MockTransport(race))
    assert not AgentCatalog.cache_path().exists()

    def offline(request):
        raise httpx.ConnectError("fixture outage")

    with pytest.raises(CatalogConnectionError):
        AgentCatalog.refresh(connection, allow_stale=True, transport=httpx.MockTransport(offline))
    AgentCatalog.cache_path().write_bytes(previous)
    with pytest.raises(CatalogAuthenticationError, match="invalidated"):
        AgentCatalog.refresh(connection, allow_stale=True, transport=httpx.MockTransport(offline))
    # A new online authorization may restore access, and only its snapshot may go stale.
    fresh = AgentCatalog.refresh(connection, transport=catalog_transport(snapshot(2)))
    stale = AgentCatalog.refresh(connection, allow_stale=True, transport=httpx.MockTransport(offline))
    assert stale.stale and stale.snapshot == fresh.snapshot


@pytest.mark.parametrize("status", [401, 403, 404])
def test_denial_fences_session_publication(catalog_home, status):
    connection = CatalogConnection(endpoint="http://localhost:8767", auth_mode="preview", workspace_id=uuid4())

    def delayed_login(request):
        response = httpx.Response(200, headers={"Set-Cookie": "session=fixture-cookie; Path=/"}, json={})
        with connection.connect(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as other:
            with pytest.raises(CatalogAuthenticationError):
                connection.request(other, "GET", f"/v1/workspaces/{connection.workspace_id}/agent-catalog")
        return response

    with connection.connect(transport=httpx.MockTransport(delayed_login)) as client:
        with pytest.raises(CatalogAuthenticationError, match="invalidated"):
            connection.authenticate(client)
    assert not connection.session_path().exists()


@pytest.mark.parametrize("status", [401, 403, 404])
def test_denial_fences_historical_result(advanced_catalogs, historical_http, monkeypatch, status):
    _, current, worker_ref, _ = advanced_catalogs
    original = CatalogConnection.request

    def delayed(self, client, method, path):
        payload = original(self, client, method, path)
        if f"/agents/{worker_ref.id}/" in path:
            with self.connect(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as other:
                with pytest.raises(CatalogAuthenticationError):
                    original(self, other, "GET", f"/v1/workspaces/{self.workspace_id}/agent-catalog")
        return payload

    monkeypatch.setattr(CatalogConnection, "request", delayed)
    with pytest.raises(CatalogAuthenticationError, match="invalidated"):
        current.at_revision(worker_ref)
    assert not AgentCatalog.cache_path().exists()


def test_late_invalidated_response_preserves_new_authorized_cache(catalog_home):
    connection = CatalogConnection(endpoint="http://localhost:8767", auth_mode="preview", workspace_id=uuid4())
    old, new = snapshot(), snapshot(2)
    AgentCatalog.refresh(connection, transport=catalog_transport(old))

    def race(request):
        response = catalog_transport(old).handle_request(request)
        with pytest.raises(CatalogAuthenticationError):
            AgentCatalog.refresh(connection, transport=catalog_transport(old, 401))
        AgentCatalog.refresh(connection, transport=catalog_transport(new))
        return response

    with pytest.raises(CatalogAuthenticationError):
        AgentCatalog.refresh(connection, transport=httpx.MockTransport(race))
    assert AgentCatalog.model_validate_json(AgentCatalog.cache_path().read_bytes()).snapshot.cursor == new.cursor


@pytest.mark.parametrize("status", [401, 403, 404])
def test_token_denial_fences_inflight_fallback(catalog_home, status):
    token = catalog_home / "token"
    token.write_text("synthetic-token")
    token.chmod(0o600)
    connection = CatalogConnection(endpoint="https://example.test", auth_mode="token", token_file=token, workspace_id=uuid4())
    value = snapshot()
    success = httpx.MockTransport(lambda request: httpx.Response(200, content=value.model_dump_json()))
    AgentCatalog.refresh(connection, transport=success)

    def race(request):
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        with pytest.raises(CatalogAuthenticationError):
            AgentCatalog.refresh(connection, transport=httpx.MockTransport(lambda request: httpx.Response(status)))
        # Even a newer authorized cache cannot rescue an operation begun before denial.
        AgentCatalog.refresh(connection, transport=success)
        raise httpx.ConnectError("fixture outage")

    with pytest.raises(CatalogAuthenticationError, match="invalidated"):
        AgentCatalog.refresh(connection, allow_stale=True, transport=httpx.MockTransport(race))
    assert not connection.session_path().exists()


@pytest.mark.parametrize("status", [401, 403, 404])
def test_workspace_denial_fences_inflight_bootstrap_cookie_alias(catalog_home, status):
    selected = CatalogConnection(endpoint="http://localhost:8767", auth_mode="preview", workspace_id=uuid4())
    unselected = selected.model_copy(update={"workspace_id": None})
    bootstrap = {
        "account": {"account_id": str(uuid4()), "email": "fixture@example.test", "locale": "en", "verified": True},
        "workspaces": [{"workspace_id": str(selected.workspace_id), "name": "Fixture", "role": "owner"}],
        "current_workspace_id": str(selected.workspace_id), "csrf_token": "fixture-csrf",
        "session_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }

    def delayed(request):
        if request.method == "POST":
            return httpx.Response(200, headers={"Set-Cookie": "session=fixture-cookie; Path=/"}, json=bootstrap)
        response = httpx.Response(200, json=bootstrap)
        with selected.connect(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as other:
            with pytest.raises(CatalogAuthenticationError):
                selected.request(other, "GET", f"/v1/workspaces/{selected.workspace_id}/agent-catalog")
        return response

    with unselected.connect(transport=httpx.MockTransport(delayed)) as client:
        with pytest.raises(CatalogAuthenticationError, match="invalidated"):
            unselected.authenticate(client)
    assert not selected.session_path().exists()
    assert not unselected.session_path().exists()


# ───────────── Real HTTP server (formerly test_agent_catalog_http.py) ──────────────────────────
#
# Public consumer against an explicitly supplied account-server executable, gated on
# GALAIUS_TEST_ACCOUNT_CLI so a bare box never fails on the absent binary.


import hashlib as _hashlib
import os as _os
import socket as _socket
import subprocess as _subprocess
import time as _time
from datetime import UTC as _UTC, datetime as _datetime


@pytest.mark.skipif(not _os.environ.get("GALAIUS_TEST_ACCOUNT_CLI"), reason="set GALAIUS_TEST_ACCOUNT_CLI to exercise the real HTTP boundary")
def test_real_server_two_revisions_cli_sync_and_cache_recreation(tmp_path):
    public = Path(__file__).resolve().parents[1]
    root = tmp_path
    with _socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    env = {'PATH': _os.defpath, 'HOME': str(root), 'TMPDIR': str(root), 'PYTHONDONTWRITEBYTECODE': '1',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': _os.devnull,
           'PYTHONPATH': _os.pathsep.join(filter(None, (_os.environ.get('PYTHONPATH'), str(public / 'src')))),
           'GALAIUS_CSRF_SECRET_HEX': 'ab' * 32, 'GALAIUS_MAIL_KEY_HEX': 'cd' * 32,
           'OLLAMA_DISCOVERY': '0', 'BENCHMARK_SCORES': ''}
    with (root / 'server.log').open('w+') as log:
        process = _subprocess.Popen([_os.environ['GALAIUS_TEST_ACCOUNT_CLI'],
            '--database', str(root / 'accounts.sqlite3'), '--prompt-database', str(root / 'prompts.sqlite3'),
            '--prompt-authoring-root', str(root / 'authoring'), '--workflow-storage', str(root / 'artifacts'),
            '--origin', origin, '--port', str(port), '--smtp-host', '127.0.0.1', '--smtp-port', '9',
            '--mail-sender', 'fixture@example.test', '--local-http', '--local-preview', '--no-outbox-worker', '--migrate'],
            env=env, stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=origin, trust_env=False, timeout=5) as client:
                for _ in range(100):
                    try:
                        if client.get('/health').status_code == 200:
                            break
                    except httpx.ConnectError:
                        _time.sleep(.05)
                else:
                    raise RuntimeError('server did not become healthy')
                bootstrap = client.post('/v1/auth/local-preview', headers={'Origin': origin})
                bootstrap.raise_for_status()
                data = bootstrap.json()
                base = f'/v1/workspaces/{data["current_workspace_id"]}'
                headers = {'Origin': origin, 'X-CSRF-Token': data['csrf_token']}
                agent = None
                prompt = None
                original_agent = None
                cursors = []
                for version in (1, 2):
                    content = f'Fixture instructions revision {version}'
                    if prompt is None:
                        response = client.post(base + '/prompts', headers=headers, json={
                            'key': {'namespace': 'fixture', 'slug': 'instructions'},
                            'name': 'Fixture instructions', 'content': content})
                    else:
                        proposed = PromptRevision.model_validate({
                            **prompt, 'revision': str(uuid4()), 'content': content,
                            'parent_digest': prompt['digest'], 'digest': _hashlib.sha256(content.encode()).hexdigest(),
                        })
                        response = client.put(base + '/prompts/fixture/instructions', headers=headers,
                                              json=proposed.model_dump(mode='json'))
                    response.raise_for_status()
                    prompt = response.json()
                    revised = AgentRevision(id=agent.id if agent else uuid4(), revision=uuid4(),
                        parent_revision=agent.revision if agent else None, role_key='fixture-worker', name='Fixture worker',
                        prompt=PromptExecutionRef(key=prompt['key'], channel='stable', digest=prompt['digest'], revision=prompt['revision']),
                        criteria=f'price.in >= {version}', reasoning='high' if version == 1 else 'low',
                        harness_tools=('Read',) if version == 1 else ('Write',), resources=(), created_at=_datetime.now(_UTC))
                    saved = client.post(base + '/agents', headers=headers, json=revised.model_dump(mode='json'))
                    saved.raise_for_status()
                    agent = revised
                    if original_agent is None:
                        original_agent = revised
                    command = [str(Path(sys.executable).with_name('galaius')), 'agents', 'sync']
                    if version == 1:
                        command += ['--endpoint', origin, '--preview']
                    result = _subprocess.run(command, env=env, cwd=public, capture_output=True, text=True, timeout=15)
                    if result.returncode:
                        raise RuntimeError(result.stderr)
                    summary = json.loads(result.stdout)
                    cursors.append(summary['cursor'])
                    cached = json.loads((root / '.galaius/agent-catalog-cache.json').read_text())
                    assert cached['snapshot']['agents'][0]['reasoning'] == revised.reasoning
                    definitions = _subprocess.run([str(Path(sys.executable).with_name('galaius')), 'agents', 'definitions', 'codex'], env=env, cwd=public, capture_output=True, text=True, timeout=15)
                    assert definitions.returncode == 0, definitions.stderr
                    assert definitions.stdout.strip() == 'fixture-worker'
                assert cursors[0] != cursors[1]
                lead = AgentRevision(
                    id=uuid4(), revision=uuid4(), role_key='fixture-lead', name='Fixture lead',
                    prompt=agent.prompt, criteria='price.in >= 0', resources=(), created_at=_datetime.now(_UTC),
                    capabilities=({'kind': 'delegate', 'name': 'ask_worker', 'description': 'Ask the fixture worker',
                                   'agent': {'id': original_agent.id, 'revision': original_agent.revision},
                                   'input_schema': {'properties': {}, 'required': ()}},),
                )
                client.post(base + '/agents', headers=headers, json=lead.model_dump(mode='json')).raise_for_status()
                advanced_lead = lead.model_copy(update={
                    'revision': uuid4(), 'parent_revision': lead.revision,
                    'capabilities': (lead.capabilities[0].model_copy(update={
                        'agent': AgentRevisionRef(id=agent.id, revision=agent.revision),
                    }),),
                })
                client.post(base + '/agents', headers=headers, json=advanced_lead.model_dump(mode='json')).raise_for_status()
                (root / '.galaius/agent-catalog-cache.json').unlink()
                rebuilt = _subprocess.run(command, env=env, cwd=public, capture_output=True, text=True, timeout=15)
                assert rebuilt.returncode == 0, rebuilt.stderr
                current = client.get(base + '/agent-catalog').json()
                assert json.loads(rebuilt.stdout)['cursor'] == current['cursor']
                assert all(value['digest'] != original_agent.prompt.digest for value in current['paradigms'])
                consumer = _subprocess.run([sys.executable, '-c', _PINNED_CONSUMER,
                    str(lead.id), str(lead.revision), str(original_agent.id), str(original_agent.revision)],
                    env=env, cwd=public, capture_output=True, text=True, timeout=45)
                assert consumer.returncode == 0, consumer.stderr
                proof = json.loads(consumer.stdout)
                assert proof == {'pinned_launches': 3, 'latest_launches': 1, 'continuations': 1, 'head_cache_preserved': True}
                print(json.dumps({'real_backend': 'PASS', 'cli_sync_revisions': 2, 'cache_recreated': True,
                                  'vendor': 'disposable subprocess', **proof}))
        finally:
            process.terminate()
            process.wait(timeout=10)
            log.seek(0)
            diagnostics = log.read()
            if diagnostics:
                print(diagnostics)


_PINNED_CONSUMER = '''
import asyncio, json, sys
from pathlib import Path
from unittest.mock import patch
from galaius_core import AgentRevisionRef
from galaius.agents.catalog import AgentCatalog
from galaius.agents import registry as reg
from galaius.agents.providers import CodexProvider
from galaius.agents.run import run_agent, launch_continuation

lead = AgentRevisionRef(id=sys.argv[1], revision=sys.argv[2])
worker = AgentRevisionRef(id=sys.argv[3], revision=sys.argv[4])
parent = reg.register(run_id="fixture-parent", pid=None, provider="codex", name="Fixture parent",
                      agent="fixture-lead", agent_ref=lead, cwd=str(Path.cwd()))
before = AgentCatalog.model_validate_json(AgentCatalog.cache_path().read_bytes()).snapshot
commands = []
build = CodexProvider.command

def command(self, task, **kwargs):
    build(self, task, **kwargs)
    commands.append((task, kwargs))
    return [sys.executable, "-c", "print('{}')"]

def resume(self, session_id, task, **kwargs):
    return command(self, task, cwd=str(Path.cwd()), mcp_config=None, run_id="fixture-resume", **kwargs)

async def main():
    saved = None
    for options in (
        {"agent": "fixture-worker", "parent_run_id": parent.run_id},
        {"delegate": "ask_worker", "parent_run_id": parent.run_id},
        {"agent_ref": worker},
    ):
        handle = await run_agent(CodexProvider(), "Bounded fixture task", cwd=str(Path.cwd()), mesh=False, **options)
        await asyncio.wait_for(handle.wait(), 10)
        saved = reg.get_run(handle.run_id)
        assert saved.agent_ref == worker
        assert saved.model == "fixture-model-1" and saved.reasoning == "high"
        assert "Fixture instructions revision 1" in commands[-1][0]
        assert "Fixture instructions revision 2" not in commands[-1][0]
        assert commands[-1][1]["allowed_tools"] == ["Read"]
    continuation = launch_continuation(CodexProvider(), saved, "fixture-session", "Continue fixture task",
        model="wrong-head-model", criterion="price.in >= 2", reasoning="low", raw_index=0)
    assert continuation.wait() == 0
    assert "Fixture instructions revision 1" in commands[-1][0]
    assert commands[-1][1]["model"] == "fixture-model-1"
    handle = await run_agent(CodexProvider(), "Current fixture task", cwd=str(Path.cwd()), mesh=False, agent="fixture-worker")
    await asyncio.wait_for(handle.wait(), 10)
    assert reg.get_run(handle.run_id).agent_ref != worker
    assert "Fixture instructions revision 2" in commands[-1][0]
    assert commands[-1][1]["allowed_tools"] == ["Write"]
    assert AgentCatalog.model_validate_json(AgentCatalog.cache_path().read_bytes()).snapshot == before
    print(json.dumps({"pinned_launches": 3, "latest_launches": 1, "continuations": 1, "head_cache_preserved": True}))

with patch.object(CodexProvider, "available", return_value=True), patch.object(CodexProvider, "command", command), patch.object(CodexProvider, "resume_command", resume), patch("galaius.agents.run.resolve_model", lambda rule, env, **kwargs: ({}, "fixture-model-" + rule.rsplit(" ", 1)[-1])):
    asyncio.run(main())
'''


def test_a_stale_preview_session_gets_one_fresh_sign_in_not_a_refusal(catalog_home):
    """A cached preview cookie the server no longer honours (expired, signed out elsewhere) made
    every agent launch fail with HTTP 401 until someone deleted the session file by hand."""
    value = snapshot(1)
    issued = {"cookie": "first"}
    accepted = {"first"}

    def respond(request):
        if request.url.path == "/v1/auth/local-preview":
            return httpx.Response(200, headers={"Set-Cookie": f"session={issued['cookie']}; Path=/"}, json={})
        if request.headers.get("Cookie") not in {f"session={cookie}" for cookie in accepted}:
            return httpx.Response(401)
        return httpx.Response(200, content=value.model_dump_json())

    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode="preview", workspace_id=uuid4())
    AgentCatalog.refresh(connection, transport=httpx.MockTransport(respond))
    issued["cookie"], accepted = "second", {"second"}  # the first session was revoked

    refreshed = AgentCatalog.refresh(connection, transport=httpx.MockTransport(respond))

    assert refreshed.snapshot.cursor == value.cursor
    assert "second" in connection.session_path().read_text()
