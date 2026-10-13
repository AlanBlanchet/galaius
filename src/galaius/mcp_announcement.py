"""What `galaius mcp` says before any call — its handshake answer and its lists — as the package
snapshot `galaius/data/mcp_announcement.json` holds it (`galaius.server.core.announcement` writes
it; a test keeps it equal to the live server). Read by a process that never imports the server: the
upgrade relay answers a client's handshake and first listings from its worker's runtime's copy
while that worker still starts (`galaius.upgrade.supervisor.McpProtocol`), so an agent CLI that
waits only a moment for its MCP servers finds every galaius tool in its first tool list."""

import os
from importlib.metadata import distributions
from pathlib import Path
from typing import Annotated, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from galaius.data import PackageData
from galaius.private_files import PRIVATE_FILES


#: What every gateway tool's name starts with (galaius's MCP server puts `mcp__galaius__` before it on a PC).
GATEWAY_PREFIX = "ext__"
#: A gateway tool's name: the prefix, then what providers accept within 64 characters with both prefixes.
GatewayToolName = Annotated[str, Field(pattern=rf"^{GATEWAY_PREFIX}[A-Za-z0-9_-]{{1,60}}$")]


class GatewayToolWire(BaseModel):
    """One gateway tool as galaius's MCP server puts it on the wire (`AgentToolList.wire`)."""

    model_config = ConfigDict(extra="allow", frozen=True)

    name: GatewayToolName
    description: str = ""
    inputSchema: dict


class GatewayServed(BaseModel):
    """The gateway tools of this PC's linked company, per role (`McpAnnouncement.gateway_path`): a
    galaius MCP server lists only those of the role its run plays (`role`), none outside a run — an
    agent holds its own tools, never another's (the main agent calls the one that holds them)."""

    model_config = ConfigDict(frozen=True)

    #: Set by the launcher on every run it starts (`galaius.agents.run`): the role the run plays.
    ROLE: ClassVar[str] = "GALAIUS_RUN_ROLE"

    agents: dict[str, tuple[GatewayToolName, ...]] = {}
    tools: tuple[GatewayToolWire, ...] = ()

    @classmethod
    def role(cls) -> str | None:
        """The role this process's run plays; None outside a run started by galaius (an editor session)."""
        return os.environ.get(cls.ROLE) or None

    @classmethod
    def environment(cls, environment: dict[str, str], role: str | None) -> dict[str, str]:
        """`environment` for a run playing `role` (None: none, even one inherited from a parent run)."""
        return {**{key: value for key, value in environment.items() if key != cls.ROLE}, **({cls.ROLE: role} if role else {})}

    def of(self, role: str | None) -> tuple[GatewayToolWire, ...]:
        names = set(self.agents.get(role, ())) if role else set()
        return tuple(tool for tool in self.tools if tool.name in names)



class McpAnnouncement(BaseModel):
    """The server's handshake answer and its lists (tools, prompts, resources) exactly as it puts
    them on the wire. The snapshot leaves out what an install decides (`resolved`): the versions."""

    model_config = ConfigDict(frozen=True)
    #: Where the instructions name the running galaius version.
    version_marker: ClassVar[str] = "{version}"
    #: Protocol versions the server speaks, the latest last (a client asking another gets the latest).
    protocol_versions: tuple[str, ...]
    server_name: str
    #: What its handshake names as its version (its MCP library's); None in the snapshot.
    server_version: str | None = None
    capabilities: dict
    #: The server's instructions; in the snapshot, `version_marker` where its version goes.
    instructions: str
    #: Each list request (`tools/list`, `prompts/list`…) with its whole result: a client asks for
    #: them all at once and waits for every answer before its first turn.
    lists: dict[str, dict]

    @classmethod
    def read(cls, path: Path) -> "McpAnnouncement | None":
        """The snapshot at `path`, or None: none there (a runtime from before snapshots) or unreadable."""
        try:
            return cls.model_validate_json(path.read_bytes())
        except (OSError, ValidationError):
            return None

    @classmethod
    def installed(cls, site: Path) -> "McpAnnouncement | None":
        """What the galaius installed in `site` (a runtime's site-packages) announces, or None: it
        ships no snapshot, or one of the two versions it names is not installed there."""
        found = cls.read(site / "galaius" / "data" / PackageData.MCP_ANNOUNCEMENT)
        galaius, library = (next(distributions(name=name, path=[str(site)]), None) for name in ("galaius", "mcp"))
        return None if found is None or galaius is None or library is None else found.resolved(galaius.version, library.version)

    def resolved(self, galaius_version: str, server_version: str) -> "McpAnnouncement":
        """The snapshot as an install running galaius `galaius_version` on MCP library `server_version` says it."""
        return self.model_copy(update={"server_version": server_version, "instructions": self.instructions.replace(self.version_marker, galaius_version)})

    @staticmethod
    def gateway_path() -> Path:
        """The external tools galaius's MCP server on this PC serves beside its own (`ext__…`), as the
        PC's linked company last gave them, in their wire shape: the relay adds them to its first tool
        list, since an agent CLI fixes its tools before a later `tools/list_changed` could add them. In
        `UserConfig.PATH`'s folder, named here so the relay never imports the config package (it
        answers within its first second)."""
        return Path.home() / ".galaius" / "mcp-gateway-tools.json"

    @classmethod
    def record_gateway(cls, served: GatewayServed) -> None:
        """Write the tools the next server start serves (`ToolGateway.current`, each time it answers:
        a re-linked or unlinked PC's file follows it). OSError when it cannot be written."""
        PRIVATE_FILES.replace_text(cls.gateway_path(), served.model_dump_json())

    def with_gateway(self, path: Path | None = None) -> Self:
        """The announcement with the gateway tools of this run's role (`GatewayServed`) after galaius's
        own, as the server lists them (`GalaiusMcp.list_tools`); unchanged when there are none or the
        file is not one this galaius wrote (unreadable, not private, another shape)."""
        try:
            tools = GatewayServed.model_validate_json(PRIVATE_FILES.read_text(path or self.gateway_path())).of(GatewayServed.role())
        except (OSError, ValueError):
            return self
        listed = self.lists.get("tools/list")
        if not tools or listed is None:
            return self
        return self.model_copy(update={"lists": {**self.lists, "tools/list": {**listed, "tools": [*listed["tools"], *(tool.model_dump(mode="json") for tool in tools)]}}})

    def initialize_result(self, requested: str) -> dict:
        """The `initialize` result the server gives a client asking for protocol `requested`."""
        return {
            "protocolVersion": requested if requested in self.protocol_versions else self.protocol_versions[-1],
            "capabilities": self.capabilities,
            "serverInfo": {"name": self.server_name, "version": self.server_version},
            "instructions": self.instructions,
        }


__all__ = ["GATEWAY_PREFIX", "GatewayServed", "GatewayToolName", "GatewayToolWire", "McpAnnouncement"]
