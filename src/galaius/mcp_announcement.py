"""What `galaius mcp` says before any call — its handshake answer and its lists — as the package
snapshot `galaius/data/mcp_announcement.json` holds it (`galaius.server.core.announcement` writes
it; a test keeps it equal to the live server). Read by a process that never imports the server: the
upgrade relay answers a client's handshake and first listings from its worker's runtime's copy
while that worker still starts (`galaius.upgrade.supervisor.McpProtocol`), so an agent CLI that
waits only a moment for its MCP servers finds every galaius tool in its first tool list."""

from importlib.metadata import distributions
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from galaius.data import PackageData


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

    def initialize_result(self, requested: str) -> dict:
        """The `initialize` result the server gives a client asking for protocol `requested`."""
        return {
            "protocolVersion": requested if requested in self.protocol_versions else self.protocol_versions[-1],
            "capabilities": self.capabilities,
            "serverInfo": {"name": self.server_name, "version": self.server_version},
            "instructions": self.instructions,
        }


__all__ = ["McpAnnouncement"]
