"""The local endpoint of this runner: the one place a process of this PC asks for something to happen on another of its owner's PCs.

The runner holds the channel (and so the only live credential); the CLI and the shared MCP server hand it a call on a socket private to this user
(`PeerBroker`) and get the server's reply. The broker never grants: it adds `by_verified` (true only when the connecting process itself sits in the
run scope of the run it names), refuses a process inside a fenced run (a fenced run has no peer tools), bounds one frame and one read, and contains every
error per connection (a malformed local call never takes the runner down)."""

import asyncio
import json
import logging
import os
import socket
import struct
import sys
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from galaius_core import PeerCallAny, PeerRefusal, PeerReply, RunRef
from galaius_core.machine.peer.rules import BROKER_CONNECTIONS, BROKER_FRAME_MAX, BROKER_READ_SECONDS
from uuid import UUID, uuid4

from galaius.account.caller import Caller
from galaius.agents import registry as reg
from galaius.host.paths import UserPaths
from galaius.host.processes import process_started
from galaius.machine.web_runs import WebRuns
from galaius.machine.peer.link import PeerLink, PeerLinkDown, PeerLinkSlow

logger = logging.getLogger(__name__)
_CALL = TypeAdapter(PeerCallAny)
CREDENTIALS = struct.Struct("3i")


def refused(code: str, message: str) -> dict:
    return {"refusal": PeerRefusal(code=code, message=message).model_dump(mode="json", exclude_none=True)}


class PeerBroker:
    #: POSIX only: the pid and uid of the process on the other end are read with SO_PEERCRED; a Windows pipe is not built (a Windows PC is a target, not a source).
    available = sys.platform != "win32"

    @staticmethod
    def socket_path(machine_file: Path) -> Path:
        """Beside the machine file, in the user's own folder (mode 0700): `peer.sock`."""
        return machine_file.with_name("peer.sock")

    def __init__(self, link: PeerLink, path: Path) -> None:
        self.link, self.path = link, path
        self._connections = asyncio.Semaphore(BROKER_CONNECTIONS)

    async def serve(self) -> None:
        """Listens until cancelled. The socket lives in a folder of this user's only (mode 0700) and is itself 0600."""
        if not self.available:
            await asyncio.Event().wait()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._handle, path=str(self.path), limit=BROKER_FRAME_MAX)
        os.chmod(self.path, 0o600)
        try:
            async with server:
                await server.serve_forever()
        finally:
            self.path.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._connections.locked():
            writer.close()
            return
        async with self._connections:
            try:
                answer = await self._answer(reader, writer)
            except Exception:       # never the runner's end: a bad local call is that call's refusal
                logger.exception("a local peer call failed")
                answer = refused("invalid_request", "the runner could not answer this call")
            try:
                writer.write(json.dumps(answer).encode() + b"\n")
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                writer.close()

    async def _answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> dict:
        pid, uid, _gid = CREDENTIALS.unpack(writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, CREDENTIALS.size))
        if uid != os.getuid():
            return refused("permission_denied", "another user's process")
        try:
            raw = await asyncio.wait_for(reader.readline(), BROKER_READ_SECONDS)
        except (TimeoutError, ValueError, asyncio.LimitOverrunError):
            return refused("invalid_request", "the call is too large or too slow")
        try:
            body = json.loads(raw)
            call = _CALL.validate_python(body["call"])
            claimed = RunRef.model_validate(body["by"]) if body.get("by") else None
        except (ValueError, KeyError, ValidationError, TypeError):
            return refused("invalid_request", "the call is not a peer call")
        verified, refusal = self.resolved(pid, claimed)
        if refusal is not None:
            return refusal
        try:
            reply = await self.link.call(call.model_copy(update={"by": claimed, "by_verified": verified}))
        except PeerLinkDown as down:
            return refused("pc_unreachable", str(down))
        except PeerLinkSlow as slow:
            return refused("pc_slow", str(slow))
        return {"reply": reply.model_dump(mode="json", exclude_none=True)}

    @staticmethod
    def resolved(pid: int, claimed: RunRef | None) -> tuple[bool, dict | None]:
        """(`by_verified`, a refusal). The process's own run scope decides: inside a FENCED run it is refused; inside the very run it names it is verified;
        anywhere else (the person's terminal, the shared MCP server acting for a run) the name is a claim. The scope is read between two looks at the
        process's start time, so a pid that changed hands meanwhile is not taken for the first one."""
        before = process_started(pid)
        scope = Caller.scope_run(pid)
        if process_started(pid) != before:
            return False, refused("permission_denied", "that process ended")
        if scope and (placed := WebRuns.in_folder(UserPaths.config()).entry(UUID(str(scope)))) is not None and placed.placed_by is not None:
            return False, refused("permission_denied", "a run another PC placed here has no peer tools")
        record = reg.get_run(scope) if scope else None
        if record is not None and record.fence is not None:
            return False, refused("permission_denied", "a fenced run has no peer tools")
        return bool(scope and claimed is not None and scope == str(claimed.run_id)), None
