"""Registry of MCP host clients and how to register the ``interact`` server with each.

``interact install <client>`` writes (or runs) the right registration for a client
so it launches ``interact mcp`` as a stdio MCP server. The clients differ only in a
small number of serializer shapes, so each concrete behaviour is one class:

* :class:`JsonClient`  — a JSON object keyed by ``mcpServers`` / ``servers`` /
  ``context_servers`` (Cursor, VS Code, GitHub Copilot, Windsurf, Zed, Claude Desktop)
* :class:`TomlClient`  — Codex' ``[mcp_servers.<name>]`` TOML, preferring ``codex mcp add``
* :class:`CliClient`   — delegated entirely to the client's own CLI (Claude Code)

Registration formats are verified against each client's official docs (``doc_url`` per
instance) — a curated table, not invented. Refresh against the docs when a client changes
its format.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel


class Scope(StrEnum):
    user = "user"
    project = "project"


class MCPServer(BaseModel):
    """The stdio server registration to install into a client's config."""

    name: str = "interact"
    command: str = "interact"
    args: list[str] = ["mcp"]
    env: dict[str, str] = {}

    @classmethod
    def resolve(
        cls,
        dev_from: Path | None = None,
        *,
        portable: bool = False,
        project: Path | None = None,
    ) -> "MCPServer":
        """Build the registration, resolving an absolute command for portability.

        GUI clients (Claude Desktop) may not inherit the shell ``PATH``, so an
        absolute path to the installed ``interact`` is used when available. Pass
        ``dev_from`` to emit a ``uvx --from <repo> interact mcp`` command instead
        (running the server from a local checkout).
        """
        if dev_from is not None:
            source = dev_from
            if portable:
                if project is None:
                    raise ValueError("A project root is required for a portable development source")
                root = project.resolve()
                candidate = (
                    (root / dev_from).resolve()
                    if not dev_from.is_absolute()
                    else dev_from.resolve()
                )
                try:
                    source = candidate.relative_to(root)
                except ValueError as exc:
                    raise ValueError(
                        "Project-scoped --dev-from must stay inside the target project so its "
                        "persisted path is shareable"
                    ) from exc
                if not candidate.exists():
                    raise ValueError(f"Project-scoped --dev-from does not exist: {source}")
            return cls(command="uvx", args=["--from", str(source), "interact", "mcp"])
        if portable:
            return cls()
        binary = shutil.which("interact")
        return cls(command=binary or "interact", args=["mcp"])

    def require_available(self) -> None:
        command = Path(self.command)
        if command.is_absolute():
            available = command.is_file() and os.access(command, os.X_OK)
        else:
            available = shutil.which(self.command) is not None
        if available:
            return
        raise FileNotFoundError(
            f"MCP executable {self.command!r} is unavailable. Install Interact, then rerun "
            "`interact install codex`. No fallback command, scope, or provider was tried."
        )

    def require_portable(self) -> None:
        values = [self.command, *self.args]
        absolute = next((value for value in values if Path(value).is_absolute()), None)
        if absolute is None:
            return
        raise ValueError(
            f"Project-scoped MCP launch value {absolute!r} is an absolute path. "
            "Use the portable installed Interact command instead."
        )

    def json_entry(self, include_type: bool, extra: dict) -> dict:
        """Server object for ``mcpServers`` / ``servers`` / ``context_servers`` configs."""
        entry: dict = {}
        if include_type:
            entry["type"] = "stdio"
        entry["command"] = self.command
        entry["args"] = list(self.args)
        if self.env:
            entry["env"] = dict(self.env)
        entry.update(extra)
        return entry


class InstallResult(BaseModel):
    client: str
    action: str  # "wrote" | "ran" | "manual" | "skipped"
    target: str  # path or command line
    detail: str = ""


class ClientTarget(BaseModel):
    """A known MCP host. Subclasses encode the client's config format.

    Every instance auto-registers by ``id``; :meth:`by_id` resolves aliases too.
    """

    id: str
    label: str
    doc_url: str
    user_path: Path | None = None  # absolute (``~`` allowed); None ⇒ no user scope
    project_path: str | None = None  # relative to the project root; None ⇒ no project scope
    note: str = ""

    _registry: ClassVar[dict[str, "ClientTarget"]] = {}
    # In VS Code the MCP host is GitHub Copilot, so "github"/"copilot" both resolve to the vscode target.
    _aliases: ClassVar[dict[str, str]] = {"copilot": "vscode", "github": "vscode"}

    def model_post_init(self, _context: object) -> None:
        ClientTarget._registry[self.id] = self

    @classmethod
    def by_id(cls, client_id: str) -> "ClientTarget | None":
        resolved = cls._aliases.get(client_id, client_id)
        return cls._registry.get(resolved)

    @classmethod
    def all(cls) -> list["ClientTarget"]:
        return [cls._registry[k] for k in sorted(cls._registry)]

    @classmethod
    def ids(cls) -> list[str]:
        return sorted([*cls._registry, *cls._aliases])

    def path_for(self, scope: Scope, project: Path) -> Path | None:
        if scope == Scope.user:
            return self.user_path.expanduser() if self.user_path else None
        return project / self.project_path if self.project_path else None

    def install(self, server: MCPServer, scope: Scope, project: Path, dry_run: bool) -> InstallResult:
        raise NotImplementedError

    def registrations(self, project: Path, name: str = "interact") -> list[str]:
        """Scopes where ``name`` is currently registered (read-only) — for `interact status`."""
        out = []
        for scope in Scope:
            path = self.path_for(scope, project)
            if path and path.exists() and self._has_server(path, name):
                out.append(f"{scope.value} ({path})")
        return out

    def _has_server(self, path: Path, name: str) -> bool:
        return False


class JsonClient(ClientTarget):
    """Clients configured by a JSON object of named servers.

    ``include_type`` adds the ``"type": "stdio"`` discriminator (required by VS Code
    / Copilot, harmless-and-recommended for Cursor, omitted where inferred). ``extra``
    is merged into each server entry (Zed wants ``source: "custom"``, ``enabled: true``).
    """

    top_key: str
    include_type: bool = False
    extra: dict = {}

    def install(self, server, scope, project, dry_run):
        path = self.path_for(scope, project)
        if path is None:
            return InstallResult(
                client=self.id, action="skipped", target=scope.value,
                detail=f"{self.label} has no {scope.value} scope",
            )
        entry = server.json_entry(self.include_type, self.extra)
        snippet = {self.top_key: {server.name: entry}}
        document: dict = {}
        if path.exists():
            try:
                document = json.loads(path.read_text())
            except ValueError:
                # JSONC / malformed (comments, trailing commas) — never clobber it.
                return InstallResult(
                    client=self.id, action="manual", target=str(path),
                    detail="Existing file isn't plain JSON — add this yourself:\n"
                    + json.dumps(snippet, indent=2),
                )
        document.setdefault(self.top_key, {})[server.name] = entry
        if dry_run:
            return InstallResult(client=self.id, action="manual", target=str(path),
                                 detail=json.dumps(document, indent=2))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n")
        return InstallResult(client=self.id, action="wrote", target=str(path))

    def _has_server(self, path, name):
        try:
            document = json.loads(path.read_text())
        except ValueError:
            return False
        return name in (document.get(self.top_key) or {})


class TomlClient(ClientTarget):
    """Codex — user registration through its CLI, project registration through trusted TOML."""

    cli: str = "codex"

    def install(self, server, scope, project, dry_run):
        server.require_available()
        if scope == Scope.project:
            server.require_portable()
        if scope == Scope.user and shutil.which(self.cli):
            return self._install_cli(server, dry_run)
        return self._install_config(server, scope, project, dry_run)

    def _install_cli(self, server, dry_run):
        cmd = [self.cli, "mcp", "add", server.name]
        for key, value in server.env.items():
            cmd += ["--env", f"{key}={value}"]
        cmd += ["--", server.command, *server.args]
        if dry_run:
            return InstallResult(client=self.id, action="manual", target=" ".join(cmd))
        subprocess.run(cmd, check=True)
        return InstallResult(client=self.id, action="ran", target=" ".join(cmd))

    def _install_config(self, server, scope, project, dry_run):
        path = self.path_for(scope, project)
        if path is None:
            return InstallResult(
                client=self.id, action="skipped", target=scope.value,
                detail=f"{self.label} has no {scope.value} scope",
            )
        original = path.read_text() if path.exists() else ""
        merged = self._merged_toml(original, server, path)
        if dry_run:
            return InstallResult(client=self.id, action="manual", target=str(path), detail=merged)
        if merged == original:
            return InstallResult(
                client=self.id, action="skipped", target=str(path),
                detail=f"[mcp_servers.{server.name}] already current",
            )
        self._write_atomic(path, merged)
        return InstallResult(client=self.id, action="wrote", target=str(path), detail="updated registration")

    def _has_server(self, path, name):
        try:
            document = tomllib.loads(path.read_text())
        except (tomllib.TOMLDecodeError, ValueError):
            return False
        return name in (document.get("mcp_servers") or {})

    def _merged_toml(self, original: str, server: MCPServer, path: Path) -> str:
        try:
            document = tomllib.loads(original)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Cannot update {path}: invalid TOML; file was left unchanged") from exc
        servers = document.get("mcp_servers")
        if servers is not None and not isinstance(servers, dict):
            raise ValueError(f"Cannot update {path}: mcp_servers is not a table; file was left unchanged")

        headers = list(re.finditer(
            r"(?m)^[ \t]*\[(?!\[)([^\]\r\n]+)\][ \t]*(?:#.*)?(?:\r?\n|$)",
            original,
        ))
        spans = []
        for index, header in enumerate(headers):
            if self._is_server_table(header.group(1).strip(), server.name):
                end = headers[index + 1].start() if index + 1 < len(headers) else len(original)
                spans.append((header.start(), end))
        existing = isinstance(servers, dict) and server.name in servers
        if existing and not spans:
            raise ValueError(
                f"Cannot update {path}: existing {server.name!r} registration is not a table; "
                "file was left unchanged"
            )

        pieces = []
        cursor = 0
        for start, end in spans:
            pieces.append(original[cursor:start])
            cursor = end
        pieces.append(original[cursor:])
        base = "".join(pieces).rstrip("\r\n")
        separator = "\n\n" if base else ""
        return base + separator + self._toml_block(server)

    @staticmethod
    def _is_server_table(section: str, name: str) -> bool:
        roots = (f"mcp_servers.{name}", f'mcp_servers."{name}"', f"mcp_servers.'{name}'")
        return any(section == root or section.startswith(root + ".") for root in roots)

    @staticmethod
    def _write_atomic(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode if path.exists() else None
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        try:
            if mode is not None:
                temporary.chmod(mode)
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _toml_block(server: MCPServer) -> str:
        args = ", ".join(json.dumps(arg) for arg in server.args)
        lines = [
            f"[mcp_servers.{server.name}]",
            f"command = {json.dumps(server.command)}",
            f"args = [{args}]",
        ]
        if server.env:
            lines.append("")
            lines.append(f"[mcp_servers.{server.name}.env]")
            lines += [f"{key} = {json.dumps(value)}" for key, value in server.env.items()]
        return "\n".join(lines) + "\n"


class CliClient(ClientTarget):
    """Claude Code — registration delegated to ``claude mcp add`` (handles scope + state)."""

    cli: str = "claude"

    def install(self, server, scope, project, dry_run):
        cmd = [self.cli, "mcp", "add", "--scope", scope.value]
        for key, value in server.env.items():
            cmd += ["--env", f"{key}={value}"]
        cmd += [server.name, "--", server.command, *server.args]
        line = " ".join(cmd)
        if not shutil.which(self.cli):
            return InstallResult(client=self.id, action="manual", target=line,
                                 detail=f"Install the {self.label} CLI, then run: {line}")
        if dry_run:
            return InstallResult(client=self.id, action="manual", target=line)
        subprocess.run(cmd, check=True)
        return InstallResult(client=self.id, action="ran", target=line)

    def registrations(self, project: Path, name: str = "interact") -> list[str]:
        """Claude Code stores both scopes in ~/.claude.json (user at top level, project
        nested under projects[abspath])."""
        path = Path("~/.claude.json").expanduser()
        if not path.exists():
            return []
        try:
            document = json.loads(path.read_text())
        except ValueError:
            return []
        out = []
        if name in (document.get("mcpServers") or {}):
            out.append(f"user ({path})")
        project_cfg = (document.get("projects") or {}).get(str(project.resolve()), {})
        if name in (project_cfg.get("mcpServers") or {}):
            out.append(f"project ({path})")
        return out


class VSCodeClient(JsonClient):
    """VS Code / GitHub Copilot. Project scope writes ``.vscode/mcp.json``; USER (global)
    scope prefers the documented ``code --add-mcp`` CLI — profile-aware and version-stable,
    since the user-profile ``mcp.json`` path is not safely hardcodable (it moves to
    ``User/profiles/<id>/`` for non-default profiles). Falls back to writing the
    default-profile user ``mcp.json`` when the ``code`` CLI isn't on PATH."""

    cli: str = "code"

    def install(self, server, scope, project, dry_run):
        if scope == Scope.user and shutil.which(self.cli):
            entry = {"name": server.name, "type": "stdio",
                     "command": server.command, "args": server.args}
            if server.env:
                entry["env"] = server.env
            cmd = [self.cli, "--add-mcp", json.dumps(entry)]
            if dry_run:
                return InstallResult(client=self.id, action="manual", target=" ".join(cmd))
            subprocess.run(cmd, check=True)
            return InstallResult(client=self.id, action="ran", target="code --add-mcp (user profile)")
        return super().install(server, scope, project, dry_run)


def _vscode_user_mcp_path() -> Path:
    """Default-profile user mcp.json (for the CLI-less fallback + status detection)."""
    if sys.platform == "darwin":
        base = Path("~/Library/Application Support/Code/User")
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", "~")) / "Code" / "User"
    else:
        base = Path("~/.config/Code/User")
    return base.expanduser() / "mcp.json"


def _claude_desktop_path() -> Path:
    if sys.platform == "darwin":
        return Path("~/Library/Application Support/Claude/claude_desktop_config.json").expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", "~")).expanduser() / "Claude" / "claude_desktop_config.json"
    return Path("~/.config/Claude/claude_desktop_config.json").expanduser()


# Registry — instantiating each target registers it via model_post_init.
CliClient(
    id="claude", label="Claude Code", doc_url="https://code.claude.com/docs/en/mcp",
    note="Persisted by `claude mcp add`; supports user and project scopes.",
)
JsonClient(
    id="cursor", label="Cursor", doc_url="https://cursor.com/docs/context/mcp",
    top_key="mcpServers", include_type=True,
    user_path=Path("~/.cursor/mcp.json"), project_path=".cursor/mcp.json",
)
TomlClient(
    id="codex", label="OpenAI Codex CLI", doc_url="https://developers.openai.com/codex/mcp",
    user_path=Path("~/.codex/config.toml"), project_path=".codex/config.toml",
    note="User scope uses `codex mcp add`; project scope loads only in a trusted project.",
)
VSCodeClient(
    id="vscode", label="VS Code / GitHub Copilot",
    doc_url="https://code.visualstudio.com/docs/copilot/reference/mcp-configuration",
    top_key="servers", include_type=True,
    user_path=_vscode_user_mcp_path(), project_path=".vscode/mcp.json",
    note="Global via `code --add-mcp`; Copilot reads the same config.",
)
JsonClient(
    id="windsurf", label="Windsurf", doc_url="https://docs.windsurf.com/windsurf/cascade/mcp",
    top_key="mcpServers", include_type=False,
    user_path=Path("~/.codeium/windsurf/mcp_config.json"),
    note="User scope only — Windsurf has no project-level MCP config.",
)
JsonClient(
    id="zed", label="Zed", doc_url="https://zed.dev/docs/ai/mcp",
    top_key="context_servers", include_type=False, extra={"source": "custom", "enabled": True},
    user_path=Path("~/.config/zed/settings.json"), project_path=".zed/settings.json",
    note="Merges into your settings.json; if that file has comments you'll get a manual snippet.",
)
JsonClient(
    id="claude-desktop", label="Claude Desktop",
    doc_url="https://modelcontextprotocol.io/quickstart/user",
    top_key="mcpServers", include_type=False, user_path=_claude_desktop_path(),
    note="Fully restart the app after installing. Linux is community-supported.",
)
