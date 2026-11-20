"""Events a command sends to the server: every event of one command, from any path, is ordered."""

import json
from datetime import UTC, datetime
from uuid import UUID
from galaius_core import MachineEvent
from galaius.machine.connection.shared import Shared


class Events(Shared):
    """Numbers and sends the events of the commands in flight."""

    #: Next event sequence per command in flight.
    _sequences: dict[UUID, int]

    def _next_sequence(self, command_id: UUID) -> int:
        self._sequences[command_id] = self._sequences.get(command_id, 0) + 1
        return self._sequences[command_id]

    async def _event(self, socket, command_id: UUID, kind: str, payload: dict[str, object] | None = None) -> None:
        event = MachineEvent(command_id=command_id, sequence=self._next_sequence(command_id), kind=kind, timestamp=datetime.now(UTC), payload=payload or {})
        await socket.send(json.dumps({"type": "event", "event": event.model_dump(mode="json")}))
