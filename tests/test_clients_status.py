"""Read-only binding detection behind `interact status`."""

import json

import pytest

from interact.clients import ClientTarget, JsonClient, TomlClient


@pytest.mark.parametrize("alias", ["github", "copilot"])
def test_vscode_aliases_resolve(alias):
    """In VS Code the MCP host is GitHub Copilot, so `interact install github` (and the legacy
    `copilot`) must resolve to the vscode target."""
    target = ClientTarget.by_id(alias)
    assert target is not None and target.id == "vscode"


def test_json_client_detects_registration(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"interact": {"command": "interact"}, "other": {}}}))
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
    import interact.clients as clients
    from interact.clients import ClientTarget, MCPServer, Scope

    monkeypatch.setattr(clients.shutil, "which", lambda c: "/usr/bin/code" if c == "code" else None)
    result = ClientTarget.by_id("vscode").install(MCPServer.resolve(), Scope.user, tmp_path, dry_run=True)
    assert "--add-mcp" in result.target  # global via the code CLI, not a per-project file


def test_vscode_project_scope_writes_and_detects(tmp_path):
    from interact.clients import ClientTarget, MCPServer, Scope

    vscode = ClientTarget.by_id("vscode")
    vscode.install(MCPServer.resolve(), Scope.project, tmp_path, dry_run=False)
    assert (tmp_path / ".vscode" / "mcp.json").exists()
    assert any("project" in r for r in vscode.registrations(tmp_path))


def test_toml_client_detects_registration(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[mcp_servers.interact]\ncommand = "interact"\nargs = ["mcp"]\n')
    client = TomlClient(id="t-toml", label="T", doc_url="x", user_path=cfg)

    assert client.registrations(tmp_path) == [f"user ({cfg})"]
    assert client.registrations(tmp_path, name="absent") == []
