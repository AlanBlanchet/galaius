"""An Interact server double for `interact login`, the machine connection and `interact logout`,
speaking the real wire models (`interact_core`), for proving the whole path on a computer with no
real server (the Windows CI job). The owner's approval on /link is `POST /test/approve`; a Script
step dispatched as the server does (signed with the machine's key, the owner's approval taken as
given) is `POST /test/run`; what the server saw is `GET /test/state`.

    uv run python tests/support/account_server.py --port 8765
"""

import argparse
import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from uuid import UUID, uuid4

import uvicorn
from interact_core import (DeviceLoginIssued, DeviceLoginStart, DeviceLoginStarted, MachineCommand, MachineCommandResult, MachineRef, MachineSummary,
                           ReleaseInfo, ScriptImplementation, WorkflowKey, WorkflowRevisionRef)
from interact_core.device_login import DeviceLoginWorkspace, UserCode
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from interact.machines import MachineRunner


class Pending(BaseModel):
    """One `interact login` waiting for its owner."""

    model_config = ConfigDict(frozen=True)
    device_code: str
    user_code: str
    start: DeviceLoginStart
    approved: bool = False


class Machine(BaseModel):
    """A computer the owner let in: its credentials, and what its connection said."""

    id: UUID = Field(default_factory=uuid4)
    name: str
    token: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    key_id: UUID = Field(default_factory=uuid4)
    key: str = Field(default_factory=lambda: "ik_" + secrets.token_urlsafe(32))
    online: bool = False
    connections: int = 0
    features: tuple[str, ...] = ()
    revoked: bool = False
    results: dict[str, MachineCommandResult] = {}

    def summary(self) -> MachineSummary:
        return MachineSummary(id=self.id, name=self.name, state="revoked" if self.revoked else "online" if self.online else "offline",
                              last_seen_at=datetime.now(UTC))


class AccountServer(BaseModel):
    """One workspace, one owner, the machines they let in."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    workspace: DeviceLoginWorkspace = Field(default_factory=lambda: DeviceLoginWorkspace(id=uuid4(), name="Test workspace"))
    pending: dict[str, Pending] = {}
    machines: dict[UUID, Machine] = {}
    sockets: dict[UUID, WebSocket] = {}

    def app(self) -> Starlette:
        return Starlette(routes=[
            Route("/v1/version", self.version), Route("/v1/device/authorizations", self.authorize, methods=["POST"]),
            Route("/v1/device/token", self.token, methods=["POST"]), Route("/v1/device/logout", self.logout, methods=["POST"]),
            Route("/v1/workspaces/{workspace}/machines", self.list_machines), WebSocketRoute("/v1/machine-channel", self.channel),
            Route("/test/approve", self.approve, methods=["POST"]), Route("/test/run", self.run, methods=["POST"]), Route("/test/state", self.state)])

    async def version(self, _request: Request) -> Response:
        release = ReleaseInfo(version=version("interact"), core_version=version("interact-core"), changelog=())
        return Response(release.model_dump_json(), media_type="application/json")

    async def authorize(self, request: Request) -> Response:
        start = DeviceLoginStart.model_validate_json(await request.body())
        item = Pending(device_code=secrets.token_urlsafe(32), user_code=UserCode.new(), start=start)
        self.pending[item.device_code] = item
        link = f"{request.base_url}link"
        started = DeviceLoginStarted(device_code=item.device_code, user_code=item.user_code, verification_uri=link,
                                     verification_uri_complete=f"{link}?code={item.user_code}", expires_in=600, interval=1)
        return JSONResponse(started.revealed(), status_code=201)

    async def token(self, request: Request) -> Response:
        item = self.pending.get((await request.json())["device_code"])
        if item is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not item.approved:
            return JSONResponse({"error": "authorization_pending"}, status_code=400)
        del self.pending[item.device_code]
        machine = Machine(name=item.start.client_name)
        self.machines[machine.id] = machine
        issued = DeviceLoginIssued.model_validate({
            "workspace": self.workspace, "approved_by": "owner@example.org", "machine": machine.summary(), "machine_token": machine.token,
            "api_key": {"id": machine.key_id, "secret": machine.key, "scopes": ("read",)}})
        return JSONResponse(issued.revealed())

    async def approve(self, request: Request) -> Response:
        code = UserCode.normalize((await request.json())["user_code"])
        item = next((item for item in self.pending.values() if item.user_code == code), None)
        if item is None:
            return JSONResponse({"error": "no such code"}, status_code=404)
        self.pending[item.device_code] = item.model_copy(update={"approved": True})
        return JSONResponse({"approved": code, "platform": item.start.platform, "client": item.start.client_name})

    def _by_key(self, request: Request) -> Machine | None:
        presented = request.headers.get("authorization", "").removeprefix("Bearer ")
        return next((machine for machine in self.machines.values() if machine.key == presented and not machine.revoked), None)

    async def list_machines(self, request: Request) -> Response:
        if self._by_key(request) is None or request.path_params["workspace"] != str(self.workspace.id):
            return JSONResponse({"error": "authentication_failed"}, status_code=401)
        return JSONResponse([machine.summary().model_dump(mode="json") for machine in self.machines.values()])

    async def logout(self, request: Request) -> Response:
        machine = self._by_key(request)
        if machine is None:
            return JSONResponse({"error": "authentication_failed"}, status_code=401)
        machine.revoked = True
        socket = self.sockets.pop(machine.id, None)
        if socket is not None:
            await socket.send_json({"type": "revoked"})
        return JSONResponse({"api_key": str(machine.key_id), "machine": str(machine.id)})

    async def channel(self, socket: WebSocket) -> None:
        presented = socket.headers.get("authorization", "").removeprefix("Bearer ")
        machine = next((machine for machine in self.machines.values() if machine.token == presented and not machine.revoked), None)
        if machine is None:
            await socket.close(code=4401)
            return
        await socket.accept()
        hello = await socket.receive_json()
        machine.features, machine.online = tuple(hello["features"]), True
        machine.connections += 1
        self.sockets[machine.id] = socket
        try:
            while True:
                message = await socket.receive_json()  # heartbeats, events, results
                if message.get("type") == "result":
                    result = MachineCommandResult.model_validate(message["result"])
                    machine.results[str(result.command_id)] = result
        except WebSocketDisconnect:
            pass
        finally:
            machine.online = False
            if self.sockets.get(machine.id) is socket:
                del self.sockets[machine.id]

    async def run(self, request: Request) -> Response:
        """`{"language", "source"}` run as a Script step on the one connected machine; answers its
        result once the machine sends it (or 504 after 60 s)."""
        body = await request.json()
        machine = next(machine for machine in self.machines.values() if machine.id in self.sockets)
        unsigned = MachineCommand(
            id=uuid4(), nonce=uuid4(), machine=MachineRef(id=machine.id), workspace_id=self.workspace.id, run_id=uuid4(),
            workflow=WorkflowRevisionRef(key=WorkflowKey(id=uuid4()), revision=uuid4()), node_id=uuid4(),
            impl=ScriptImplementation.inline(body["language"], body["source"]), config={"source": body["source"]},
            expires_at=datetime.now(UTC) + timedelta(minutes=2), signature="0" * 64)
        command = unsigned.model_copy(update={"signature": MachineRunner.signature(machine.token, unsigned)})
        await self.sockets[machine.id].send_json({"type": "command", "command": command.model_dump(mode="json")})
        for _ in range(600):
            if str(command.id) in machine.results:
                return JSONResponse(machine.results[str(command.id)].model_dump(mode="json"))
            await asyncio.sleep(0.1)
        return JSONResponse({"error": "no result"}, status_code=504)

    async def state(self, _request: Request) -> Response:
        return JSONResponse({"pending": [item.user_code for item in self.pending.values() if not item.approved],
                             "machines": [machine.model_dump(mode="json", exclude={"token", "key", "results"}) for machine in self.machines.values()]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args()
    asyncio.run(uvicorn.Server(uvicorn.Config(AccountServer().app(), host="127.0.0.1", port=arguments.port, log_level="warning")).serve())
