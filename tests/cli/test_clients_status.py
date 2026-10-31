"""Registration and read-only binding detection for supported MCP hosts."""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import galaius.cli.clients as clients
from galaius.cli import app_commands
from galaius.cli.clients import ClientTarget, JsonClient, MCPServer, Scope, TomlClient


@pytest.mark.parametrize("alias", ["github", "copilot"])
def test_vscode_aliases_resolve(alias):
    """In VS Code the MCP host is GitHub Copilot, so `galaius install github` (and the legacy
    `copilot`) must resolve to the vscode target."""
    target = ClientTarget.by_id(alias)
    assert target is not None and target.id == "vscode"


def test_json_client_detects_registration(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"galaius": {"command": "galaius"}, "other": {}}}))
    client = JsonClient(id="t-json", label="T", doc_url="x", top_key="mcpServers", user_path=cfg)

    assert client.registrations(tmp_path) == [f"user ({cfg})"]
    assert client.registrations(tmp_path, name="absent") == []


def test_json_client_handles_missing_and_malformed(tmp_path):
    client = JsonClient(id="t-json2", label="T", doc_url="x", top_key="servers",
                        user_path=tmp_path / "nope.json")
    assert client.registrations(tmp_path) == []  # file absent

    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json, // comment}")
    client2 = JsonClient(id="t-json3", label="T", doc_url="x", top_key="servers", user_path=bad)
    assert client2.registrations(tmp_path) == []  # malformed → not crashing


def test_vscode_user_scope_prefers_code_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(clients.shutil, "which", lambda c: "/usr/bin/code" if c == "code" else None)
    result = ClientTarget.by_id("vscode").install(MCPServer.resolve(), Scope.user, tmp_path, dry_run=True)
    assert "--add-mcp" in result.target  # global via the code CLI, not a per-project file


def test_vscode_project_scope_writes_and_detects(tmp_path):
    vscode = ClientTarget.by_id("vscode")
    vscode.install(MCPServer.resolve(), Scope.project, tmp_path, dry_run=False)
    assert (tmp_path / ".vscode" / "mcp.json").exists()
    assert any("project" in r for r in vscode.registrations(tmp_path))


def test_toml_client_detects_registration(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[mcp_servers.galaius]\ncommand = "galaius"\nargs = ["mcp"]\n')
    client = TomlClient(id="t-toml", label="T", doc_url="x", user_path=cfg)

    assert client.registrations(tmp_path) == [f"user ({cfg})"]
    assert client.registrations(tmp_path, name="absent") == []


@pytest.mark.parametrize("scope", [Scope.user, Scope.project])
def test_codex_install_respects_the_selected_scope(monkeypatch, tmp_path, scope):
    """A project registration must never become a host-global mutation just because Codex exists.

    The project file also has to remain shareable: another developer supplies their own
    ``galaius`` executable through PATH instead of inheriting this machine's absolute path.
    """
    codex_binary = tmp_path / "bin" / "codex"
    galaius_binary = tmp_path / "private-bin" / "galaius"
    galaius_binary.parent.mkdir()
    galaius_binary.write_text("#!/bin/sh\n")
    galaius_binary.chmod(0o755)
    calls = []
    monkeypatch.setattr(
        clients.shutil,
        "which",
        lambda command: (
            str(codex_binary) if command == "codex"
            else str(galaius_binary) if command == "galaius"
            else None
        ),
    )
    monkeypatch.setattr(clients.subprocess, "run", lambda command, check: calls.append(command))

    project = tmp_path / "project with spaces"
    project.mkdir()
    config_path = project / ".codex" / "config.toml"
    config_path.parent.mkdir()
    stale = (
        '[features]\nexample = true\n\n'
        '[mcp_servers.galaius]\ncommand = "stale-galaius"\nargs = ["old"]\n'
    )
    config_path.write_text(stale)
    app_commands.install("codex", scope=scope, project=project)

    if scope == Scope.user:
        assert calls == [["codex", "mcp", "add", "galaius", "--", str(galaius_binary), "mcp"]]
        assert config_path.read_text() == stale
    else:
        assert calls == [], "project scope must not invoke the host-global `codex mcp add` command"
        document = tomllib.loads(config_path.read_text())
        assert document["features"] == {"example": True}
        assert document["mcp_servers"]["galaius"] == {"command": "galaius", "args": ["mcp"]}
        assert str(galaius_binary) not in config_path.read_text()
        first_install = config_path.read_bytes()
        app_commands.install("codex", scope=scope, project=project)
        assert config_path.read_bytes() == first_install
        assert config_path.read_text().count("[mcp_servers.galaius]") == 1

    original_config = config_path.read_bytes()
    original_calls = list(calls)
    missing_server = MCPServer(command=str(tmp_path / "missing"))
    codex_target = ClientTarget.by_id("codex")
    assert codex_target is not None
    with pytest.raises(FileNotFoundError, match="unavailable.*No fallback"):
        codex_target.install(missing_server, scope, project, dry_run=False)
    assert config_path.read_bytes() == original_config
    assert calls == original_calls


def test_codex_project_install_preserves_malformed_toml(monkeypatch, tmp_path):
    monkeypatch.setattr(
        clients.shutil,
        "which",
        lambda command: str(tmp_path / "bin" / command) if command in {"codex", "galaius"} else None,
    )
    calls = []
    monkeypatch.setattr(clients.subprocess, "run", lambda command, check: calls.append(command))
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir()
    malformed = b"[mcp_servers.galaius\ncommand = broken\n"
    config_path.write_bytes(malformed)

    with pytest.raises(ValueError, match="invalid TOML.*left unchanged"):
        app_commands.install("codex", scope=Scope.project, project=tmp_path)

    assert config_path.read_bytes() == malformed
    assert calls == []


@pytest.mark.parametrize("scope", [Scope.user, Scope.project])
def test_codex_cli_absent_uses_the_selected_config_scope(monkeypatch, tmp_path, scope):
    user_home = tmp_path / "home"
    project = tmp_path / "project"
    galaius_binary = tmp_path / "bin" / "galaius"
    user_home.mkdir()
    project.mkdir()
    galaius_binary.parent.mkdir()
    galaius_binary.write_text("#!/bin/sh\n")
    galaius_binary.chmod(0o755)
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setattr(
        clients.shutil,
        "which",
        lambda command: str(galaius_binary) if command == "galaius" else None,
    )

    app_commands.install("codex", scope=scope, project=project)

    config_path = (
        user_home / ".codex" / "config.toml"
        if scope == Scope.user
        else project / ".codex" / "config.toml"
    )
    registration = tomllib.loads(config_path.read_text())["mcp_servers"]["galaius"]
    expected_command = str(galaius_binary) if scope == Scope.user else "galaius"
    assert registration == {"command": expected_command, "args": ["mcp"]}


def test_codex_install_explains_activation(monkeypatch, tmp_path, capsys):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    galaius_binary = binary_dir / "galaius"
    galaius_binary.write_text("#!/bin/sh\n")
    galaius_binary.chmod(0o755)
    monkeypatch.setattr(clients.shutil, "which", lambda command: str(binary_dir / command))
    monkeypatch.setattr(clients.subprocess, "run", lambda command, check: None)

    app_commands.install("codex", scope=Scope.user, project=tmp_path)
    output = capsys.readouterr().out

    assert "codex mcp get galaius" in output
    assert "fresh Codex session" in output or "restart Codex" in output


@pytest.mark.asyncio
async def test_codex_user_install_is_discoverable_from_a_fresh_process_and_real_mcp_session(tmp_path):
    """The public installer must bridge an empty Codex host to Galaius's real stdio tool surface.

    This is the one external-seam integration test: it uses isolated homes, invokes no model or
    API, and starts only the server command that Codex persisted.
    """
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("the Codex CLI is required for the registration integration")
    galaius = str(Path(sys.executable).with_name("galaius"))
    assert Path(galaius).is_file(), "the current project environment must expose its Galaius CLI"

    consumer = tmp_path / "consumer"
    codex_home = tmp_path / "codex-home"
    user_home = tmp_path / "user-home"
    consumer.mkdir()
    codex_home.mkdir()
    user_home.mkdir()
    environment = {
        "CODEX_HOME": str(codex_home),
        "HOME": str(user_home),
        "USERPROFILE": str(user_home),
        "PATH": os.pathsep.join((str(Path(galaius).parent), os.environ["PATH"])),
        "LANG": "C.UTF-8",
    }

    empty = subprocess.run(
        [codex, "mcp", "list", "--json"], cwd=consumer, env=environment,
        capture_output=True, text=True, timeout=20,
    )
    assert empty.returncode == 0, empty.stderr
    assert json.loads(empty.stdout) == []
    missing = subprocess.run(
        [codex, "mcp", "get", "galaius", "--json"], cwd=consumer, env=environment,
        capture_output=True, text=True, timeout=20,
    )
    assert missing.returncode != 0
    assert "No MCP server named 'galaius' found" in missing.stderr

    installed = subprocess.run(
        [galaius, "install", "codex", "--scope", "user"], cwd=consumer, env=environment,
        capture_output=True, text=True, timeout=20,
    )
    assert installed.returncode == 0, installed.stderr

    listed = subprocess.run(
        [codex, "mcp", "list", "--json"], cwd=consumer, env=environment,
        capture_output=True, text=True, timeout=20,
    )
    assert listed.returncode == 0, listed.stderr
    assert [server["name"] for server in json.loads(listed.stdout)] == ["galaius"]
    fetched = subprocess.run(
        [codex, "mcp", "get", "galaius", "--json"], cwd=consumer, env=environment,
        capture_output=True, text=True, timeout=20,
    )
    assert fetched.returncode == 0, fetched.stderr
    registration = json.loads(fetched.stdout)["transport"]
    assert registration["command"] == galaius
    assert registration["args"] == ["mcp"]

    server_environment = {
        "CODEX_HOME": str(codex_home),
        "HOME": str(user_home),
        "USERPROFILE": str(user_home),
        "PATH": environment["PATH"],
        "LANG": environment["LANG"],
    }
    parameters = StdioServerParameters(
        command=registration["command"], args=registration["args"],
        env=server_environment, cwd=consumer,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    assert {"screenshot", "review_ui", "agent_spawn", "agent_list"} <= names
