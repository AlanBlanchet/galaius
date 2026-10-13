"""Every galaius tool declares a category and publishes it in MCP `_meta`; the packaged snapshot
(`galaius/data/mcp_tools.json`, what the web server groups an agent's tools by) is the live list.

The announcement snapshot (`galaius/data/mcp_announcement.json`, what the upgrade relay answers a
client's handshake and first listings from) is what the live server says on the wire.

Regenerate both after changing a tool or the instructions:
    python -c "import json; from galaius.server import core; from galaius.data import PackageData; PackageData.path(PackageData.MCP_TOOLS).write_text(json.dumps(core.tool_catalog(), indent=1) + '\\n'); PackageData.path(PackageData.MCP_ANNOUNCEMENT).write_text(json.dumps(core.announcement(), indent=1, ensure_ascii=False) + '\\n')"
"""

import asyncio
import json
from importlib.metadata import version
from types import SimpleNamespace
from typing import get_args

import anyio
import pytest
from mcp import types
from mcp.server.session import ServerSession
from mcp.shared.message import SessionMessage

from galaius import __version__
from galaius.agents.tool_gateway import AgentToolList, GatewayTool, ToolGateway
from galaius.data import PackageData
from galaius.mcp_announcement import McpAnnouncement
from galaius.server import core


def test_every_tool_publishes_its_category_and_the_snapshot_is_current():
    listed = asyncio.run(core.mcp.list_tools())
    assert listed and all((tool.meta or {}).get("category") in get_args(core.ToolCategory) for tool in listed)
    assert json.loads(PackageData.read(PackageData.MCP_TOOLS)) == core.tool_catalog()


async def _initialize_answer(requested: str) -> dict:
    """The live server's own `initialize` result for a client asking protocol `requested`."""
    to_server, server_reads = anyio.create_memory_object_stream(1)
    server_writes, from_server = anyio.create_memory_object_stream(1)
    async with ServerSession(server_reads, server_writes, core.mcp._mcp_server.create_initialization_options()):
        request = types.JSONRPCRequest(jsonrpc="2.0", id=1, method="initialize", params={"protocolVersion": requested, "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
        await to_server.send(SessionMessage(types.JSONRPCMessage(request)))
        return (await from_server.receive()).message.root.result


@pytest.mark.parametrize("requested", ["2024-11-05", "2025-06-18", "1999-01-01"])
def test_the_announcement_snapshot_is_current_and_answers_as_the_server_does(requested: str) -> None:
    assert json.loads(PackageData.read(PackageData.MCP_ANNOUNCEMENT)) == core.announcement()
    announced = McpAnnouncement.read(PackageData.path(PackageData.MCP_ANNOUNCEMENT)).resolved(__version__, version("mcp"))
    assert announced.initialize_result(requested) == asyncio.run(_initialize_answer(requested))
    wire = [tool.model_dump(by_alias=True, mode="json", exclude_none=True) for tool in asyncio.run(core.mcp.list_tools())]
    assert announced.lists["tools/list"] == {"tools": wire}


def _listing(name: str) -> AgentToolList:
    return AgentToolList(revision=name, agents={"galaius": (name,)}, tools=(GatewayTool(name=name, server_id="s", tool="list_workflows", description="[galaius] List the workflows.",
                                                                                      input_schema={"type": "object", "properties": {}, "additionalProperties": False}),))


def test_the_relay_first_lists_the_gateway_tools_of_the_company_this_pc_is_linked_to(monkeypatch) -> None:
    """An agent CLI fixes its tools at start: the relay's first `tools/list` already holds the gateway
    tools the server will list once started — the linked company's, re-linked or unlinked included."""
    snapshot = McpAnnouncement.read(PackageData.path(PackageData.MCP_ANNOUNCEMENT))
    for linked in (_listing("ext__galaius__list_workflows"), _listing("ext__other__search"), None):
        monkeypatch.setattr(ToolGateway, "linked", classmethod(lambda cls, linked=linked: None if linked is None else SimpleNamespace(cached=lambda: linked)))
        served = ToolGateway.current(online=False)
        monkeypatch.setattr(core.mcp.gateway, "listing", served)
        wire = [tool.model_dump(by_alias=True, mode="json", exclude_none=True) for tool in asyncio.run(core.mcp.list_tools())]
        assert snapshot.with_gateway().lists["tools/list"] == {"tools": wire}
        assert [tool["name"] for tool in wire if tool["name"].startswith("ext__")] == ([] if linked is None else [linked.tools[0].name])
