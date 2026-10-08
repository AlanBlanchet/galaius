"""External tools an agent run on this PC calls through its Galaius server: the gateway.

An agent connected on the server to a remote MCP server (Context7, Notion, …) gets that server's
tools on this PC under ``mcp__galaius__ext__<server>__<tool>``: galaius's own MCP server lists
them (``ToolGateway.listing``) and forwards each call to the server (``ToolGateway.call``), which
runs it with the sign-in it keeps — no key of the company's ever reaches this PC. The server checks
every call again (the PC's owner may run the agent, the server is approved and unchanged, the tool
is switched on for that agent).

Only a LINKED PC (`galaius login`) has a gateway: it is the PC's own machine token that asks. The
listing is cached beside the agent catalog (``agent-tools-<server+company>.json``, its own file: the catalog's
cache is read by older galaius processes that refuse a field they do not know), so a projection
and a start read it with no network, and a server that does not answer leaves the last list."""

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any, Self

import anyio
import httpx
from mcp import types
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from galaius.agents import registry
from galaius.agents.catalog_connection import CatalogConnection, CatalogConnectionError

#: What every gateway tool's name starts with (the harness puts galaius's `mcp__galaius__` before it).
GATEWAY_PREFIX = "ext__"


class GatewayTool(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=rf"^{GATEWAY_PREFIX}[A-Za-z0-9_-]{{1,60}}$")
    server_id: str
    tool: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    #: Seconds the server gives one call (a download + transcription takes minutes); None: the default.
    timeout: float | None = Field(default=None, gt=0, le=3600)


class AgentToolList(BaseModel):
    """What the server answers `GET /v1/machines/agent-tools`: per role, its gateway tools."""

    model_config = ConfigDict(frozen=True)

    revision: str = ""
    agents: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    tools: tuple[GatewayTool, ...] = ()

    def names(self, role: str) -> tuple[str, ...]:
        """`role`'s gateway tools, as galaius's MCP server serves them (`ext__…`)."""
        return self.agents.get(role, ())

    def granted(self, role: str, tools: list[str] | tuple[str, ...], prefix: str) -> list[str]:
        """`role`'s harness tools with its gateway tools added under `prefix` (galaius's own MCP
        name). A role with no list is unrestricted and already reaches every one: it stays so."""
        return [*tools, *(f"{prefix}{name}" for name in self.names(role) if f"{prefix}{name}" not in tools)] if tools else list(tools)

    def tool(self, name: str) -> GatewayTool | None:
        return next((tool for tool in self.tools if tool.name == name), None)


class GatewayCallFailed(Exception):
    """A call the server refused or could not run: its words, for the agent to read."""


class ToolGateway(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    connection: CatalogConnection
    #: Seconds one listing read may take (a start never waits longer for it).
    list_timeout: float = 5.0
    #: Seconds one call may take, the server's own budget for the remote tool (30 s) included; a tool
    #: the server lists with a longer budget gets that plus `call_margin`.
    call_timeout: float = 90.0
    call_margin: float = 30.0

    @classmethod
    def linked(cls) -> Self | None:
        """This PC's gateway, None when the PC is not linked to a server."""
        connection = CatalogConnection.linked()
        return None if connection is None else cls(connection=connection)

    @classmethod
    def current(cls, *, online: bool) -> AgentToolList:
        """This PC's listing: read again when `online` (the server just answered for the catalog;
        at most once per `CatalogConnection.REUSE_SECONDS`), else the cached one; empty when the PC
        is not linked."""
        gateway = cls.linked()
        if gateway is None:
            return AgentToolList()
        return gateway.connection.recent("agent-tools", lambda _: gateway.refreshed()) if online else gateway.cached()

    @property
    def cache_path(self) -> Path:
        """One file per server and company: a PC re-linked elsewhere never reads another's tools."""
        key = hashlib.sha256(f"{self.connection.endpoint}\0{self.connection.workspace_id}".encode()).hexdigest()[:16]
        return CatalogConnection.path().with_name(f"agent-tools-{key}.json")

    def cached(self) -> AgentToolList:
        """The last listing this PC read from its server; empty when it never read one (or it is unreadable)."""
        try:
            return AgentToolList.model_validate_json(self.cache_path.read_bytes())
        except (OSError, ValidationError):
            return AgentToolList()

    def listing(self) -> AgentToolList:
        """The listing read now (`If-None-Match` the cached one: an unchanged list costs a 304),
        cached for the next reader. Raises CatalogConnectionError when the server cannot be read."""
        cached = self.cached()
        try:
            with self.connection.connect() as client:
                response = client.get("/v1/machines/agent-tools", headers={"if-none-match": f'"{cached.revision}"'} if cached.revision else {},
                                      timeout=self.list_timeout)
        except httpx.HTTPError as error:
            raise CatalogConnectionError(f"the server's tool list could not be read ({type(error).__name__})") from error
        if response.status_code == 304:
            return cached
        if response.status_code != 200:
            raise CatalogConnectionError(f"the server refused its tool list (HTTP {response.status_code})")
        try:
            value = AgentToolList.model_validate_json(response.content)
        except ValidationError as error:
            raise CatalogConnectionError("the server's tool list is not one this galaius reads") from error
        CatalogConnection.replace_text(self.cache_path, value.model_dump_json())
        return value

    def refreshed(self) -> AgentToolList:
        """`listing`, or the cached one when the server cannot be read now."""
        try:
            return self.listing()
        except CatalogConnectionError:
            return self.cached()

    def call(self, name: str, arguments: dict[str, Any], *, role: str | None, run_id: str | None, budget: float | None = None) -> str:
        """Run gateway tool `name` on the server; its text answer. GatewayCallFailed says why not.
        `budget`: the seconds the server lists for that tool."""
        body = {"name": name, "arguments": arguments, "role_key": role, "run_id": run_id}
        timeout = max(self.call_timeout, (budget or 0.0) + self.call_margin)
        try:
            with self.connection.connect() as client:
                response = client.post("/v1/machines/agent-tools/call", json=body, timeout=timeout)
        except httpx.TimeoutException as error:
            raise GatewayCallFailed(f"the Galaius server did not answer within {timeout:.0f} s") from error
        except (httpx.HTTPError, CatalogConnectionError) as error:
            raise GatewayCallFailed(f"the Galaius server cannot be reached from this PC right now ({error})") from error
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        if not isinstance(answer, dict):
            answer = {}
        if response.status_code != 200:
            words = answer.get("error") if isinstance(answer, dict) else None
            raise GatewayCallFailed(str(words or f"the Galaius server refused the call (HTTP {response.status_code})"))
        return "\n".join(str(part.get("text", "")) for part in answer.get("content", ()) if isinstance(part, dict))

    @staticmethod
    def calling_role(listing: AgentToolList, name: str) -> tuple[str | None, str | None]:
        """(role, run id) a call from this process is made for: the role of the agent run this
        galaius serves (`GALAIUS_RUN_ID`, set by the launcher), when that role is given `name`;
        None otherwise — the owner's own session, or a sub-agent the run started (the server then
        checks the call against every agent the PC's owner may run)."""
        run_id = os.environ.get("GALAIUS_RUN_ID") or os.environ.get("GALAIUS_PARENT_RUN_ID")
        run = registry.get_run(run_id) if run_id else None
        role = run.agent if run is not None else None
        return (role if role is not None and name in listing.names(role) else None), run_id


class ServedGateway(BaseModel):
    """The gateway tools galaius's MCP server serves in this process: the cached listing at start
    (written by the sync or start that launched this run), read again every `interval` seconds."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    listing: AgentToolList = Field(default_factory=lambda: ToolGateway.current(online=False))
    interval: float = 120.0

    def tools(self) -> list[types.Tool]:
        return [types.Tool(name=tool.name, description=tool.description, inputSchema=tool.input_schema) for tool in self.listing.tools]

    def serves(self, name: str) -> bool:
        return name.startswith(GATEWAY_PREFIX)

    async def call(self, name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        """Forward one call; a refusal or an unreachable server raises ToolError with its words (the
        agent reads an error result, never a hang: `ToolGateway.call_timeout` bounds it)."""
        gateway = ToolGateway.linked()
        if gateway is None:
            raise ToolError("this PC is not linked to a Galaius server (run `galaius login`): its external tools are unavailable")
        role, run_id = ToolGateway.calling_role(self.listing, name)
        listed = self.listing.tool(name)
        try:
            text = await anyio.to_thread.run_sync(lambda: gateway.call(name, arguments, role=role, run_id=run_id, budget=listed.timeout if listed else None))
        except GatewayCallFailed as error:
            raise ToolError(str(error)) from error
        return [types.TextContent(type="text", text=text)]

    async def watch(self, alive) -> None:
        """Read the listing again every `interval` seconds while `alive()` (a `_SideLoop` body)."""
        waited = 0.0
        while alive():
            await asyncio.sleep(1.0)
            waited += 1.0
            if waited >= self.interval:
                waited = 0.0
                gateway = ToolGateway.linked()
                if gateway is not None:
                    self.listing = await anyio.to_thread.run_sync(gateway.refreshed)
