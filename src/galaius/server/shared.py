"""One galaius MCP server for every MCP client of this computer and runtime.

Every agent CLI used to start its own `galaius mcp`: a whole server per client (hundreds of
megabytes each, several dozen on a busy computer). Now `galaius mcp` is a small relay
(`galaius.mcp_relay`) and this module is the one server behind it: it listens on a socket private to
this user (`endpoint`: one per runtime and agents folder) and serves each relay's connection as its
own MCP session, bound to the caller the relay's hello names (`galaius.caller.Caller`): its
environment, folder and process identify it, never this server's.

A connection's browser session (the one minted for it) is closed when it ends; named sessions and
the sandbox display are shared as on one server. Once another runtime is active, each connection
quiet and holding nothing a move would lose is told to move (`SWITCH`, with the new endpoint) and
runs nothing more; this server leaves once none is left. Any server leaves after a while with no
client (`SharedServerSettings`), and checks for releases while it runs: one check per computer,
not one per client."""

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import anyio
from mcp import types
from mcp.shared.message import SessionMessage
from pydantic import BaseModel, ConfigDict, ValidationError

from galaius.caller import Caller
from galaius.config import UserConfig
from galaius.mcp_endpoint import SharedEndpoint
from galaius.mcp_relay import HELLO, SWITCH, Endpoint
from galaius.processes import share_malloc_arenas
from galaius.server import sandbox
from galaius.server.core import CONNECTION, _auto_session_names, _lifespan, _sessions, mcp
from galaius.upgrade.store import Runtime, RuntimeStore

_log = logging.getLogger("galaius.shared")


class SharedServerSettings(BaseModel):
    """How long a shared server waits; `GALAIUS_SHARED_MCP_<FIELD>` overrides one (tests)."""

    model_config = ConfigDict(frozen=True)
    #: Seconds a server of the active runtime stays up with no client.
    idle_seconds: float = 600.0
    #: Seconds a server of an older runtime stays up with no client.
    outdated_seconds: float = 5.0
    #: Seconds between two looks at the runtime pointer, the clients and the release schedule.
    tick_seconds: float = 1.0
    #: Seconds a connection has to say hello.
    hello_seconds: float = 10.0
    #: What a release check claims before it runs (the check sets the real next one).
    check_claim_seconds: float = 120.0
    #: Seconds a newly active runtime's server has to answer before that runtime is rolled back from.
    start_seconds: float = 60.0

    @classmethod
    def from_environment(cls) -> "SharedServerSettings":
        prefix = "GALAIUS_SHARED_MCP_"
        return cls.model_validate({name.removeprefix(prefix).lower(): value for name, value in os.environ.items() if name.startswith(prefix)})


class Connection:
    """One relay's connection: its MCP session bound to its caller, and what of it is in flight
    (requests either way not answered yet), which decides when it may move."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.in_flight: set[str] = set()
        self.moving = False
        self.inbound: anyio.abc.ObjectSendStream | None = None

    def track(self, body: dict, inbound: bool) -> None:
        """Keep `in_flight` current with one message: a request opens its id on its side, an answer closes it."""
        if "id" not in body:
            return
        asking = "method" in body
        side = "client" if inbound == asking else "server"  # who asked: a request from the client, an answer to the server
        (self.in_flight.add if asking else self.in_flight.discard)(f"{side}:{json.dumps(body['id'])}")

    def quiet(self) -> bool:
        return not self.in_flight

    def holds(self) -> bool:
        """What a move would lose: its own browser session, or the sandbox display shared with others."""
        own = _auto_session_names.get(self)
        return (own is not None and _sessions.peek(own) is not None) or sandbox._sandbox is not None

    async def hello(self, seconds: float) -> Caller:
        raw = await asyncio.wait_for(self.reader.readline(), seconds)
        if not raw:
            raise EOFError  # a look whether this server answers (`SharedServer.follow_active`), not a client
        body = json.loads(raw)
        if body.get("method") != HELLO:
            raise ValueError(f"expected {HELLO}, got {body.get('method')!r}")
        return Caller.model_validate(body["params"])

    async def serve(self, settings: SharedServerSettings) -> None:
        caller = await self.hello(settings.hello_seconds)
        token = CONNECTION.set(self)
        try:
            with caller.bound():
                inbound, self_inbound = anyio.create_memory_object_stream[SessionMessage | Exception](0)
                outbound, self_outbound = anyio.create_memory_object_stream[SessionMessage](0)
                self.inbound = inbound
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(self._read, inbound)
                    tasks.start_soon(self._write, self_outbound)
                    await mcp._mcp_server.run(self_inbound, outbound, mcp._mcp_server.create_initialization_options())
                    tasks.cancel_scope.cancel()
        finally:
            CONNECTION.reset(token)
            if (own := _auto_session_names.pop(self, None)) is not None:
                await _sessions.close(own)
            self.writer.close()

    async def _read(self, inbound) -> None:
        async with inbound:
            while raw := await self.reader.readline():
                if self.moving:
                    continue  # told to move: what it sends now runs on the next server
                try:
                    message = types.JSONRPCMessage.model_validate_json(raw)
                except ValidationError as error:
                    await inbound.send(error)
                    continue
                self.track(message.model_dump(by_alias=True, exclude_none=True), inbound=True)
                await inbound.send(SessionMessage(message))

    async def _write(self, outbound) -> None:
        async with outbound:
            async for item in outbound:
                body = item.message.model_dump(by_alias=True, exclude_none=True)
                self.track(body, inbound=False)
                self.writer.write(json.dumps(body, separators=(",", ":")).encode() + b"\n")
                await self.writer.drain()

    async def move(self, endpoint: Endpoint) -> None:
        """Tell the relay to move to `endpoint` and run nothing more of it here: only while it is still quiet
        and holds nothing, decided with no await before `moving` (a request read meanwhile would run twice)."""
        if self.moving or not self.quiet() or self.holds():
            return
        self.moving = True
        self.writer.write(json.dumps({"jsonrpc": "2.0", "method": SWITCH, "params": endpoint.dumped()}).encode() + b"\n")
        await self.writer.drain()
        await self.inbound.aclose()  # its session ends: nothing it sent later runs here


class SharedServer:
    """The listening server: its connections, its runtime's place among the installed ones, its idleness."""

    def __init__(self, socket: Path, settings: SharedServerSettings) -> None:
        self.socket, self.settings = socket, settings
        self.connections: set[Connection] = set()
        self.store = RuntimeStore.default()
        self.idle_since = time.monotonic()
        self.checker = None
        #: The newer runtime whose server answered: quiet connections move there.
        self.verified: Path | None = None
        #: When each newer runtime's server last did not answer.
        self.refused: dict[Path, float] = {}

    @classmethod
    def main(cls, argv: list[str]) -> None:
        """Started by a relay in its allowlisted environment (`galaius.mcp_relay.STARTER`), it takes its settings and keys
        from the user's config (`UserConfig.apply`), never from a client."""
        UserConfig.apply(portable=False)
        share_malloc_arenas()  # tool calls of every client run in worker threads
        Caller.serve_many()
        os.chdir(Path.home())
        asyncio.run(cls(Path(argv[0]), SharedServerSettings.from_environment()).run())

    async def run(self) -> None:
        mcp._mcp_server.lifespan = _per_connection  # the server's lifespan runs once, around every connection
        async with _lifespan(mcp):
            Endpoint.private_folder(str(self.socket))
            self.socket.unlink(missing_ok=True)
            listener = await asyncio.start_unix_server(self._accept, path=str(self.socket))
            os.chmod(self.socket, 0o600)
            bound = self.socket.stat().st_ino
            _log.info("galaius MCP server for %s listening on %s", Runtime.own().label(), self.socket)
            try:
                while not self.done():
                    await asyncio.sleep(self.settings.tick_seconds)
                    self.checker = self.store.check_releases(self.checker, self.settings.check_claim_seconds)
                    await self.move_quiet()
            finally:
                listener.close()
                with suppress(OSError):
                    if self.socket.stat().st_ino == bound:  # never the socket a newer server bound since
                        self.socket.unlink()

    def outdated(self) -> bool:
        return self.store.active().path != Runtime.own().path

    def done(self) -> bool:
        if self.connections:
            self.idle_since = time.monotonic()
            return False
        return time.monotonic() - self.idle_since > (self.settings.outdated_seconds if self.outdated() else self.settings.idle_seconds)

    async def follow_active(self) -> Endpoint | None:
        """Where quiet connections move: the active runtime's shared server, once it answers; None while this
        server's runtime is the active one, or the active one has no shared server (an older build) or its
        server does not answer (asked again after `start_seconds`). Its clients then stay here: a server never
        moves the runtime pointer, the upgrade's own checks judge a build."""
        active, now = self.store.active(), time.monotonic()
        site = active.site_packages
        if active.path == Runtime.own().path or site is None or not (site / "galaius" / "server" / "shared.py").is_file():
            return None
        endpoint = SharedEndpoint.of(active)
        if self.verified != active.path:
            if now - self.refused.get(active.path, -self.settings.start_seconds) < self.settings.start_seconds:
                return None
            try:
                await asyncio.to_thread(lambda: endpoint.connect(self.settings.start_seconds).close())
            except OSError as error:
                self.refused[active.path] = now
                _log.warning("the active runtime's MCP server did not answer (%s): clients stay here", error)
                return None
            self.verified = active.path
        return endpoint

    async def move_quiet(self) -> None:
        if not self.connections or (endpoint := await self.follow_active()) is None:
            return  # no client to move: an outdated server simply leaves (`done`)
        for connection in list(self.connections):
            await connection.move(endpoint)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = Connection(reader, writer)
        self.connections.add(connection)
        try:
            await connection.serve(self.settings)
        except EOFError:
            pass
        except (OSError, ValueError, asyncio.TimeoutError) as error:
            _log.warning("a client connection ended: %s", error)
        finally:
            self.connections.discard(connection)


@asynccontextmanager
async def _per_connection(_server):
    yield {}


if __name__ == "__main__":
    SharedServer.main(sys.argv[1:])
