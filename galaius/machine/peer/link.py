"""This runner's calls to its server on its live channel: a call goes out as a `peer` message, its `PeerReply` comes back under the same request id.

A call has a failure budget: a link that is down answers at once (`PeerLinkDown`), one answer is awaited for `PEER_ANSWER_SECONDS` (a start: `PEER_START_SECONDS`),
and a call that timed out is asked once more with the same idempotency key, so a retry never places a second run."""

import asyncio
import json
from uuid import UUID, uuid4

from galaius_core import PeerCallAny, PeerEnvelope, PeerReply, PeerStart
from galaius_core.machine.peer.rules import PEER_ANSWER_SECONDS, PEER_START_SECONDS


class PeerLinkDown(Exception):
    """The channel is not open: nothing was sent."""


class PeerLinkSlow(Exception):
    """The server did not answer in time, twice: the request may have been placed (a start's key finds it again)."""


class PeerLink:
    def __init__(self) -> None:
        self._socket = None
        self._waiting: dict[UUID, asyncio.Future[PeerReply]] = {}

    def attach(self, socket) -> None:
        self._socket = socket

    def detach(self) -> None:
        """The channel closed: every call waiting for its answer is over (the server drops an answer for a connection that is gone)."""
        self._socket = None
        for waiting in self._waiting.values():
            if not waiting.done():
                waiting.set_exception(PeerLinkDown("the connection to the server closed"))
        self._waiting.clear()

    @property
    def up(self) -> bool:
        return self._socket is not None

    def resolve(self, reply: object) -> None:
        """A `peer_reply` of the server; one nobody waits for (late, or a retry's first answer) is dropped."""
        answer = PeerReply.model_validate(reply)
        waiting = self._waiting.pop(answer.request_id, None)
        if waiting is not None and not waiting.done():
            waiting.set_result(answer)

    async def call(self, call: PeerCallAny) -> PeerReply:
        seconds = PEER_START_SECONDS if isinstance(call, PeerStart) else PEER_ANSWER_SECONDS
        for attempt in range(2):
            socket = self._socket
            if socket is None:
                raise PeerLinkDown("this PC is not connected to the server")
            envelope = PeerEnvelope(request_id=uuid4(), call=call)
            waiting = self._waiting[envelope.request_id] = asyncio.get_running_loop().create_future()
            await socket.send(json.dumps({"type": "peer", "envelope": envelope.model_dump(mode="json")}))
            try:
                return await asyncio.wait_for(asyncio.shield(waiting), seconds + (call.wait_s if hasattr(call, "wait_s") else 0))
            except TimeoutError:
                self._waiting.pop(envelope.request_id, None)
                if attempt:
                    raise PeerLinkSlow("the server did not answer in time") from None
        raise PeerLinkSlow("the server did not answer in time")
