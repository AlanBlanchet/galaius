"""What the runtime's reflection asks did (`reflection.ReflectionWatch`) and what coordinators were shown of them
(`relations.card`): one registry-wide state file (`#reflection-cadence.state`), read by both, imported by both."""

from __future__ import annotations

import time
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from galaius.agents import registry as reg

#: How long a mark is kept: work weeks old is never asked about, nor shown, again.
KEPT_DAYS = 14
STATE = "reflection-cadence"


class Marks(BaseModel):
    """Per coordinator run, the time up to which its turns were reflected on (`reflected`); per reflection run the runtime
    asked, when (`asked`), the end of the turn whose durable rows went to the librarian (`forwarded`), and the end of the
    turn whose open rows a coordinator's card showed (`shown`)."""

    model_config = ConfigDict(extra="ignore")

    reflected: dict[str, float] = Field(default_factory=dict)
    asked: dict[str, float] = Field(default_factory=dict)
    forwarded: dict[str, float] = Field(default_factory=dict)
    shown: dict[str, float] = Field(default_factory=dict)

    @classmethod
    def read(cls, raw: bytes | None) -> Marks:
        """The marks as written; an unreadable file reads as none (the watcher's start bounds what it asks about)."""
        try:
            return cls.model_validate_json(raw) if raw else cls()
        except ValidationError:
            return cls()

    def with_(self, other: Marks) -> Marks:
        """These marks and `other`'s, `other`'s winning where both have one."""
        return Marks(**{name: getattr(self, name) | getattr(other, name) for name in type(self).model_fields})

    def pruned(self, now: float) -> Marks:
        """Without the marks older than `KEPT_DAYS`."""
        kept = now - KEPT_DAYS * 86400
        return Marks(**{name: {run: at for run, at in getattr(self, name).items() if at >= kept} for name in type(self).model_fields})


def read() -> Marks:
    with reg.shared_state(STATE) as (raw, _write, _keep):
        return Marks.read(raw)


def update(change: Callable[[Marks], Marks]) -> None:
    """The stored marks replaced by `change` of them, under the state's lock (a leaf: nothing else is locked inside it)."""
    with reg.shared_state(STATE) as (raw, write, _keep):
        write(change(Marks.read(raw)).pruned(time.time()).model_dump_json().encode())
