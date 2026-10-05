"""The gateway's PC side: a call the server refuses, or a server that does not answer, is an
error result the agent reads — never a hang, never a crash of interact's MCP server."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from interact.agents.catalog_connection import CatalogConnection
from interact.agents.tool_gateway import AgentToolList, GatewayTool, ServedGateway, ToolGateway


class FakeServer:
    """`POST /v1/machines/agent-tools/call` answering `status` with `body`, after `delay` seconds."""

    def __init__(self, status: int, body: dict, delay: float = 0.0) -> None:
        self.calls: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return None

            def do_POST(self) -> None:
                fake.calls.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
                threading.Event().wait(delay)
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def gateway(self, tmp_path) -> ToolGateway:
        token = tmp_path / "token"
        token.write_text("iwk_test")
        token.chmod(0o600)
        return ToolGateway(connection=CatalogConnection(endpoint=f"http://127.0.0.1:{self.server.server_address[1]}", workspace_id=uuid4(),
                                                        auth_mode="token", token_file=token), call_timeout=1.0)


LISTING = AgentToolList(revision="r", agents={"researcher": ("ext__Context7__query-docs",)},
                        tools=(GatewayTool(name="ext__Context7__query-docs", server_id=str(uuid4()), tool="query-docs"),))


@pytest.mark.parametrize(("status", "body", "delay", "said"), [
    (200, {"content": [{"type": "text", "text": "React hooks: useState …"}]}, 0.0, "React hooks: useState …"),
    (403, {"code": "not_bound", "error": "the agent researcher is not connected to ext__Context7__query-docs"}, 0.0, ToolError("not connected")),
    (502, {"code": "unreachable", "error": "the MCP server answered 503"}, 0.0, ToolError("answered 503")),
    (200, {}, 2.0, ToolError("did not answer within 1 s")),              # bounded: never a hang
])
def test_a_gateway_call_answers_the_server_s_words_or_a_plain_error(tmp_path, monkeypatch, status, body, delay, said):
    fake = FakeServer(status, body, delay)
    monkeypatch.setattr(ToolGateway, "linked", classmethod(lambda cls: fake.gateway(tmp_path)))
    monkeypatch.setenv("INTERACT_RUN_ID", "run-without-record")
    served = ServedGateway(listing=LISTING)
    call = served.call("ext__Context7__query-docs", {"query": "react hooks"})
    if isinstance(said, ToolError):
        with pytest.raises(ToolError, match=str(said)):
            asyncio.run(call)
    else:
        assert asyncio.run(call)[0].text == said
    assert fake.calls[0] == {"name": "ext__Context7__query-docs", "arguments": {"query": "react hooks"}, "role_key": None, "run_id": "run-without-record"}
    fake.server.shutdown()
