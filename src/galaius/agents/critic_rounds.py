"""How many rounds each critic role took on one maker's work, and the refusal of one more past the cap
(`Config.agent_critic_cap`).

A week to 2026-10-20: 1,121 visual-critic runs, 274 of them in 71 subjects reviewed again and again, the three-round
cap written only as prompt text. A round is one ask of a critic role for a maker: a run of that role started for it
(`galaius agents spawn --for`), or a task message sent to one for it (`--for`, or the maker itself writing to a critic
already reviewing its work). Several critics asked in one turn of the asker (a blind panel) are one round. Each round is
written down as it is given (`#critic-rounds.state`), whichever run of the role takes it, so a fresh instance inherits
the rounds spent; the maker's work is the maker and the runs it handed off to (`relations.live`): a hand-off never
resets the count. An agent asking for one more past the cap is refused, in one line; the person never is, and his
`--for` rounds count all the same. The asker is read from the process asking (`messaging.asking_run`), which an agent
cannot pass off as the person; the ledger is a file of the same user's, a guard against loops, not against an agent
set on editing it.
"""

from __future__ import annotations

import json
import time
from typing import Self

from pydantic import BaseModel, ConfigDict, ValidationError

from galaius.agents import agent_queue, relations
from galaius.agents import registry as reg
from galaius.config import Config
from galaius.config.settings import CriticCap


class Round(BaseModel):
    """One ask of critic `role` for maker `maker`, taken by run `run_id` at `at`; asked by run `asker` in its turn
    `turn` (None: the person)."""

    model_config = ConfigDict(frozen=True)

    role: str
    maker: str
    run_id: str
    at: float
    asker: str | None = None
    turn: str | None = None

    def ask(self) -> tuple[str, str]:
        """What makes it one round: the asker's turn (a panel asked at once is one), else itself (the person's ask)."""
        return (self.asker, self.turn) if self.asker is not None and self.turn is not None else ("", f"{self.run_id}@{self.at}")


class CriticRounds(BaseModel):
    """The rounds critic roles took, against `cap`."""

    model_config = ConfigDict(frozen=True)

    cap: CriticCap

    @classmethod
    def here(cls) -> Self:
        return cls(cap=Config().agent_critic_cap)

    def _ledger(self) -> list[Round]:
        with reg.shared_state("critic-rounds") as (raw, _write, _keep):
            return _rounds(raw)

    def spent(self, role: str, maker: str) -> list[Round]:
        """The rounds critic `role` took on `maker`'s work, oldest first, one per ask (`Round.ask`): those for any run whose
        hand-off chain ends where `maker`'s does."""
        live, ends = relations.live(maker), {}
        mine = [entry for entry in self._ledger() if entry.role == role
                and ends.setdefault(entry.maker, relations.live(entry.maker)) == live]
        return list({entry.ask(): entry for entry in reversed(mine)}.values())[::-1]

    def maker_of(self, critic: reg.AgentRun, *, for_run: str | None, sender: str | None) -> str | None:
        """The maker a task message to run `critic` is for: `for_run`, else `sender` when `critic` already reviews its work
        (a maker writing to its own critic); None when it is for no maker (a lead's note, the person's message)."""
        if critic.agent not in self.cap.roles:
            return None
        if for_run is not None:
            return for_run
        if sender is None:
            return None
        live = relations.live(sender)
        return sender if any(entry.run_id == critic.run_id and relations.live(entry.maker) == live for entry in self._ledger()) else None

    def refusal(self, role: str | None, maker: str | None, *, asker: str | None, turn: str | None) -> str | None:
        """Why agent run `asker` may not ask critic `role` for one more round on `maker`'s work in its turn `turn`; None when
        it may, when the role judges nothing, when it is for no maker, when the person asks (`asker` None), or when this
        turn's round is already counted (another critic of the same panel)."""
        if asker is None or role is None or maker is None or role not in self.cap.roles:
            return None
        spent = self.spent(role, maker)
        if len(spent) < self.cap.rounds or (asker, turn) in {entry.ask() for entry in spent}:
            return None
        runs = ", ".join(dict.fromkeys(entry.run_id[:8] for entry in spent))
        return (f"{role} already took {len(spent)} rounds on {maker[:8]}'s work (runs {runs}; cap {self.cap.rounds}): no "
                f"further {role} round is started for it by an agent. Fix what its last verdict lists, or ask the person for "
                "another round.")

    def note(self, role: str | None, maker: str | None, run_id: str, *, asker: str | None, turn: str | None, at: float | None = None) -> None:
        """Write down that run `run_id` of critic `role` took an ask for `maker` (nothing when the role judges nothing or it
        is for no maker)."""
        if role is None or maker is None or role not in self.cap.roles:
            return
        moment = time.time() if at is None else at
        with reg.shared_state("critic-rounds") as (raw, write, _keep):
            rounds = [entry for entry in _rounds(raw) if moment - entry.at <= self.cap.kept_days * 86400]
            rounds.append(Round(role=role, maker=maker, run_id=run_id, at=moment, asker=asker, turn=turn))
            write(json.dumps([entry.model_dump() for entry in rounds]).encode())


def turn_of(asker: str | None) -> str | None:
    """Which turn of run `asker` asks: the queue item that began it (its newest one no longer pending: each turn after its
    first begins with one), else its first; None for the person. A message still waiting is no turn yet. Read without its
    record lock (the queue file is replaced whole): a caller holding other runs' locks (`messaging.queue_message`) never
    takes a third."""
    if asker is None:
        return None
    begun = [item for item in agent_queue.items_locked(asker) if item.state != "pending"]
    return begun[-1].id if begun else "first"


def _rounds(raw: bytes | None) -> list[Round]:
    """The ledger as written; an unreadable one, or entry, reads as none (a lost count never refuses anyone)."""
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        return []
    found = []
    for entry in entries if isinstance(entries, list) else []:
        try:
            found.append(Round.model_validate(entry))
        except ValidationError:
            continue
    return found
