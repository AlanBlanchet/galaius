"""What a process of this PC (the CLI, the shared MCP server, the turn card) uses to reach another of its owner's PCs: one call to this PC's own runner.

It never signs in and never reads a credential: the runner holds the link, this asks it on its local socket (`PeerBroker`). A refusal is a `PeerError` with the
server's code and sentence; a runner that is not running is `PeerUnavailable` saying that, never « sign in »."""

import hashlib
import json
import os
import socket
import time
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from galaius_core import (PcFleet, PeerAnswer, PeerCallAny, PeerEvents, PeerMessage, PeerProjects, PeerProjectsCall, PeerRefusal, PeerSent, PeerStart, PeerStarted, PeerStop, PeerStopped,
                          PeerFleetCall, PeerWindow, RunRef)
from galaius_core.machine.peer.rules import PEER_START_SECONDS

from galaius.account.caller import Caller
from galaius.agents import registry as reg
from galaius.host.paths import UserPaths
from galaius.machine.peer.broker import PeerBroker

_ANSWER = TypeAdapter(PeerAnswer)


class PeerError(Exception):
    """The server refused the call: its typed `code` and its sentence."""

    def __init__(self, refusal: PeerRefusal) -> None:
        super().__init__(f"{refusal.message} ({refusal.code}{f', retry in {refusal.retry_after_s} s' if refusal.retry_after_s else ''})")
        self.refusal = refusal


class PeerUnavailable(Exception):
    """This PC's runner does not answer on its local socket: it is not running (or this system has no such socket)."""


class PeerClient:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or PeerBroker.socket_path(UserPaths.config() / "machine.json")

    @staticmethod
    def asker() -> RunRef | None:
        """The run this process works for, as it names it (`GALAIUS_RUN_ID` of its run scope) with its conversation; None: a person's own terminal."""
        try:
            run = Caller.current().environment.get("GALAIUS_RUN_ID")
            return RunRef(run_id=UUID(run), conversation_id=UUID(reg.conversation_of(run))) if run else None
        except (ValueError, LookupError, KeyError):
            return None

    def ask(self, call: PeerCallAny, seconds: float = PEER_START_SECONDS + 10) -> PeerAnswer:
        body = json.dumps({"call": call.model_dump(mode="json", exclude_none=True), "by": (by.model_dump(mode="json") if (by := self.asker()) else None)}).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as link:
                link.settimeout(seconds)
                link.connect(str(self.path))
                link.sendall(body)
                said = b""
                while not said.endswith(b"\n"):
                    chunk = link.recv(65536)
                    if not chunk:
                        break
                    said += chunk
        except (FileNotFoundError, ConnectionRefusedError, AttributeError, OSError) as error:
            raise PeerUnavailable("this PC's galaius is not running (start it: galaius machine service restart)" if isinstance(error, (FileNotFoundError, ConnectionRefusedError))
                                  else f"this PC's galaius did not answer ({error})") from error
        answer = json.loads(said or b"{}")
        # A refusal is the runner's own (`refusal`) or the server's (the reply's `refusal`): both are typed, both raise.
        said_no = answer.get("refusal") or answer.get("reply", {}).get("refusal")
        if said_no:
            raise PeerError(PeerRefusal.model_validate(said_no))
        return _ANSWER.validate_python(answer["reply"]["answer"])

    # ---- the verbs ----------------------------------------------------------------------------------------------------

    #: Seconds a request's key is remembered: a retry inside it is the same request and reuses the key, whatever instant it falls on; the same words after it
    #: are a new request.
    WINDOW: float = 600.0

    def key(self, *parts: object) -> str:
        """The idempotency key of a request: the key this PC already gave the same words from the same run within `WINDOW` seconds, else a new one. Kept in a
        small private file (a CLI call is one process: a retry is another), so a retry after a timeout finds what it already did, at any moment — never a
        second run."""
        asked = self.asker()
        material = hashlib.sha256("|".join(str(part) for part in (*parts, asked.run_id if asked else "")).encode()).hexdigest()
        path, now = self.path.with_name("peer-keys.json"), time.time()
        try:
            known = {name: entry for name, entry in json.loads(path.read_text()).items() if now - entry[1] <= self.WINDOW}
        except (OSError, ValueError, TypeError, IndexError):
            known = {}
        if material not in known:
            known[material] = [uuid4().hex, now]
            try:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                temporary = path.with_suffix(f".{os.getpid()}.tmp")
                descriptor = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "w") as handle:
                    json.dump(dict(sorted(known.items(), key=lambda item: item[1][1])[-200:]), handle)
                os.replace(temporary, path)
            except OSError:
                pass            # a key that could not be kept still works for this call; only a retry from another process would differ
        return known[material][0]

    def fleet(self) -> PcFleet:
        return self._of(PcFleet, PeerFleetCall())

    def projects(self) -> PeerProjects:
        return self._of(PeerProjects, PeerProjectsCall())

    def start(self, target: UUID, project: UUID, role: str, text: str, *, path: str = "", key: str | None = None, permission=None, reasoning=None) -> PeerStarted:
        return self._of(PeerStarted, PeerStart(key=key or self.key(target, project, role, path, text), target=target, project=project, path=path, role=role, text=text, permission=permission, reasoning=reasoning))

    def message(self, machine: UUID, run_id: UUID, text: str, key: str | None = None) -> PeerSent:
        return self._of(PeerSent, PeerMessage(key=key or self.key(machine, run_id, text), machine=machine, run_id=run_id, text=text))

    def events(self, machine: UUID, run_id: UUID, cursor: int | None = None, wait_s: float = 0.0) -> PeerWindow:
        return self._of(PeerWindow, PeerEvents(machine=machine, run_id=run_id, cursor=cursor, wait_s=wait_s), seconds=wait_s + 30)

    def stop(self, machine: UUID, run_id: UUID, key: str | None = None) -> PeerStopped:
        return self._of(PeerStopped, PeerStop(key=key or self.key(machine, run_id, 'stop'), machine=machine, run_id=run_id))

    def _of(self, kind, call: PeerCallAny, seconds: float = PEER_START_SECONDS + 10):
        answer = self.ask(call, seconds)
        if not isinstance(answer, kind):
            raise PeerError(PeerRefusal(code="invalid_request", message="the server answered something else than asked"))
        return answer
