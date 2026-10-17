"""The line agent runs wait in on this computer once as many work here as it allows (`Config.agent_runs_max`).

Agents fan out: three conversations each starting their own team put 20+ runs at work at once
(2026-10-13), the agents' shared memory cap (`galaius.agents.ceiling`) was reached and the kernel
killed builds, browsers and whole runs. A spawn (`galaius agents spawn`, the `agent_spawn` tool) is
never refused: past the limit its run id is handed back at once and it waits, in the order asked,
held by a detached waiter (`main`) in a scope of its own, so it outlives the process that asked. A
message to an ended run waits the same way before its resumed turn starts (`agent_queue.dispatch`).

Never counted nor held: the owner's own conversations — his editor sessions (no galaius process),
the runs he starts from the web (capped there: `WebLimited`). Every other run counts, whatever its
role: measured that day, a browser-driving critic held ~0.75 GB, a code reviewer ~0.37 GB, a builder
0.4-0.6 GB. One exception keeps a team from blocking on itself, as make's jobserver does: a run in a
place of its own and its first child at work share that place (one level only), so a lead waiting
for the critic it started never waits behind its own place; at most twice as many runs as places.

A run only waiting on its agents (`galaius agents wait`) takes no place meanwhile (`SpawnLine.aside`): 2026-10-17 every
place of 6 was held, half by runs each waiting on a child, a chain of hand-offs holding one per ancestor, while the
critic verdicts they waited for queued behind them. Its first child takes the place it gave back ahead of the line,
when that place is still free. Woken, it counts again at once, never waiting (a message for it is read at once): the
line is over the limit by the runs woken while it was full, and no spawn starts until it is below again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar, Literal, Self, TypeVar

from galaius_core import AgentRevisionRef
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from galaius.agents import agent_queue, messaging, relations
from galaius.agents import registry as reg
from galaius.agents.policy import Policy, PolicyError
from galaius.agents.providers import provider_for
from galaius.agents.vocabulary import TouchScope
from galaius.agents.run import ModelUnavailable, RunHandle, effective_permission_mode, load_policy, resolve_role, run_agent
from galaius.config import Config
from galaius.fence import FenceSpec
from galaius.machine_agents import WebRuns
from galaius.paths import UserPaths
from galaius.processes import process_exited, process_started
from galaius.upgrade.store import Runtime, RuntimeStore

#: How often a waiter, or a resume, looks whether its turn came.
POLL_SECONDS = 3.0
#: Said of a run waiting when every place is held by a team with a run waiting in line (`SpawnLine._stuck`).
STUCK = "chaque place est tenue par une équipe dont une run attend ici ; si elles s'attendent, arrêtez-en une"
#: Said of a run set aside (`SpawnLine.aside`) whose place is free, where its activity line would be (`agents list`).
#: Whom a run waits on when its wait names none (a deploy's), in its run list line.
ITS_AGENTS = "ses agents"
#: The runs a waiting run's line names at most, the rest counted (`Aside.said`): the line keeps its « depuis N min ».
WAITED_NAMED_MAX = 3
ASIDE = f"attend {ITS_AGENTS}, place rendue"
#: How long after stepping aside a run's agent may still write (the call it waits in, flushed) and stay aside (`Aside.stands_aside`).
ASIDE_GRACE_SECONDS = 15.0
#: How long a spawn that could not start stays readable (`agents list`, `agents events`) before it is forgotten.
FAILED_KEPT_SECONDS = 24 * 3600.0

logger = logging.getLogger(__name__)


class Spawn(BaseModel):
    """What one spawn starts: `run_agent`'s arguments of the same names (`provider` by name, `session_id` the
    conversation it belongs to), kept while it waits."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    task: str
    cwd: str
    provider: str | None = None
    agent: str | None = None
    name: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    profile: str | None = None
    image_paths: tuple[Path, ...] = ()
    agent_ref: AgentRevisionRef | None = None
    delegate: str | None = None
    parent_run_id: str | None = None
    session_id: str | None = None
    denied_tools: tuple[str, ...] = ()
    provider_modes: dict[str, str] = Field(default_factory=dict)
    quota_window: float | None = None
    fence: FenceSpec | None = None
    #: The run this spawn is for (`galaius agents spawn --for`): on the message handing it over, and in the
    #: `asked_by` of the run taking it, continued or new.
    for_run: str | None = None
    #: The run it takes over from (`galaius agents spawn --continues`: a hand-off), else the run of its role under its parent
    #: whose context filled (`_outgrown`): `relations.succeed`.
    continues: str | None = None
    #: One agent of its role in its conversation (`galaius agents spawn --single`): one at work is queued the task behind
    #: its turn rather than another started beside it, and every other one of the role is forgotten (`forget_peers`).
    single: bool = False
    #: Its fields `start` passes to `run_agent` other than by their own names.
    NOT_RUN_ARGUMENTS: ClassVar[frozenset[str]] = frozenset({"task", "provider", "session_id", "for_run", "single", "continues"})

    @classmethod
    def asked(cls, *, parent_run_id: str | None = None, **fields) -> Spawn:
        """A spawn as its caller asks it: its parent the run asking (`GALAIUS_PARENT_RUN_ID`) unless one is named."""
        return cls(parent_run_id=parent_run_id or os.environ.get("GALAIUS_PARENT_RUN_ID") or None, **fields)

    async def start(self, run_id: str, runner: Callable[..., Awaitable[RunHandle]] | None = None) -> RunHandle:
        """Start it as run `run_id` (the id its caller was given) through `runner` (`run_agent` when None), and give
        the child a moment to be alive before a caller about to exit lets go of it."""
        arguments = {name: getattr(self, name) for name in type(self).model_fields if name not in self.NOT_RUN_ARGUMENTS}
        predecessor = reg.get_run(self.continues) if self.continues is not None else self._outgrown()
        inherited = relations.inherited(predecessor, parent=self.parent_run_id) if predecessor is not None else {}
        # Recorded as it starts, so the card it starts on names whom it works for (`run.run_agent`).
        asked_by = tuple(dict.fromkeys(asker for asker in (self.for_run, *inherited) if asker and asker != self.parent_run_id))
        arguments |= {"name": self.name or self.agent, "provider_modes": self.provider_modes or None,
                      "asked_by": asked_by, "inherited_asks": inherited}
        with reg.session_context(self.session_id, parent_run_id=self.parent_run_id):
            handle = await (runner or run_agent)(provider_for(self.provider) if self.provider is not None else None, self.task, run_id=run_id, **arguments)
        if predecessor is not None:
            relations.succeed(predecessor.run_id, run_id)
        await asyncio.sleep(0.2)
        return handle

    def naming_refusal(self) -> str | None:
        """Why a run it names (`continues`, `for_run`) is not its caller's to name; None when each is. A run asking
        (`parent_run_id`) hands off only its own work or that of an ended run of the role in its conversation, and spawns
        for a run of its conversation, one it works for or one working for it: an agent steered by a page or a file it
        read never routes another conversation's reports to a run of its choosing. The person at a terminal names any."""
        if self.parent_run_id is None:
            return None
        if (caller := reg.get_run(self.parent_run_id)) is None:
            return f"the run asking ({self.parent_run_id}) has no record on this computer" if self.continues or self.for_run else None
        here = {run.run_id for run in reg.stored_tree(reg.conversation_of(caller.run_id))}
        if self.continues is not None and self.continues != caller.run_id:
            predecessor = reg.get_run(self.continues)
            role = self._role_name()
            if predecessor is None or predecessor.run_id not in here or predecessor.agent != role or self._busy(predecessor):
                return f"continues names {self.continues[:8]}: only your own run, or an ended {role or 'agent'} of your conversation, is yours to hand off"
        if self.for_run is not None and self.for_run not in here:
            target = reg.get_run(self.for_run)
            if target is None or (target.run_id not in caller.askers and caller.run_id not in target.askers):
                return f"for_run names {self.for_run[:8]}: outside your conversation, it neither asked you nor works for you"
        return None

    def _outgrown(self) -> reg.AgentRun | None:
        """The newest run of its role under its parent whose context filled (`relations.context_full`), not at work and
        not taken over yet: the one it takes over from."""
        role = self._role_name()
        return next((peer for peer in self._conversation()
                     if role and peer.agent == role and peer.parent_run_id == self.parent_run_id
                     and peer.successor is None and relations.context_full(peer) and not self._busy(peer)), None)

    def _role_name(self) -> str | None:
        """The role it would run as (`_role`), else the agent it names."""
        return resolved[1] if (resolved := self._role()) is not None else self.agent

    def _role(self) -> tuple[Policy, str, AgentRevisionRef | None] | None:
        """The policy, role and revision it would run as (`run.resolve_role`, as its start resolves them); None
        when it names no role or cannot be resolved (its start then refuses it itself)."""
        if self.agent is None and self.agent_ref is None and self.delegate is None:
            return None
        try:
            return resolve_role(load_policy(), self.agent, reference=self.agent_ref, parent=self.parent_run_id, delegate=self.delegate)
        except (ModelUnavailable, PolicyError, ValueError):
            return None

    def _misfit(self, run: reg.AgentRun, policy: Policy, revision: AgentRevisionRef | None) -> str | None:
        """Why `run` (same conversation, same role) cannot take this spawn's task as its next turn; None when it can.
        Busy (`_busy`) fits only a `single` spawn, queued behind its turn whatever its revision or context: one agent of
        the role at a time; the wake finding it idle and unfit starts a fresh one and forgets it.
        Its `name` and `model` are not compared: a label (the continued run keeps its own) and a request the role's
        policy overrides, at a start as at a continuation. Nor its role's revision: the turn handed to it runs on the
        spawn's (`agent_queue.MessageTurn.agent_ref`), within the permissions the run was started with.
        `queue_message(only_if_idle=True)` re-reads "is working" under the run's lock."""
        busy = self._busy(run)
        if busy and not self.single or not busy and run.status not in relations.REUSABLE:
            return "is working"
        if run.replaced_by is not None:
            return f"was refused at launch, relaunched as {run.replaced_by.run_id[:8]}"
        if run.successor is not None:
            return f"was taken over by {run.successor[:8]}"
        if not busy and not reg.model_spoke_in(run.run_id):
            return "never got a first turn"
        if not busy and revision is not None and (run.agent_ref is None or run.agent_ref.id != revision.id):
            return "runs another agent of that name"
        if not busy and relations.context_full(run):
            return f"has its context near full ({run.context_tokens}/{run.context_window})"
        if self.provider is not None and run.provider != self.provider:
            return f"runs on {run.provider}"
        # The run records the permission it got: never a wider run than this spawn would get, e.g. one a person started unrestricted.
        if run.permission_mode != effective_permission_mode(self.permission_mode, self.provider_modes, policy, run.provider):
            return f"was given {run.permission_mode or 'the default'} permissions"
        # Its session stays in its own folder: another one it reads, and writes there only when its permission reaches it.
        if not self._within(run) and provider_for(run.provider).touch_of(run.permission_mode) not in (TouchScope.READ_ONLY, TouchScope.FULL_ACCESS):
            return "works in another folder it may not write in"
        if set(self.denied_tools) != set(run.denied_tools or ()) or self.image_paths or self.profile or self.fence is not None:
            return "was started with other tools, images, profile or fence"
        return messaging.check_deliverable(run.run_id)[1]

    @staticmethod
    def _busy(run: reg.AgentRun) -> bool:
        """At work on a turn, or a turn queued for it."""
        return run.working() or any(item.state in {"pending", "running"} for item in agent_queue.items(run.run_id))

    def _peers(self, role: str) -> list[reg.AgentRun]:
        return [run for run in self._conversation() if run.agent == role]

    def forget_peers(self, keep: str) -> list[str]:
        """`single`: every other run of its role in its conversation, none busy (`_busy`), forgotten (`reg.forget`); their ids."""
        if not self.single or (resolved := self._role()) is None:
            return []
        return [run.run_id for run in self._peers(resolved[1]) if run.run_id != keep and not self._busy(run) and reg.forget(run.run_id)]

    def _within(self, run: reg.AgentRun) -> bool:
        """Whether this spawn's folder is `run`'s own or inside it."""
        return Path(self.cwd).resolve().is_relative_to(Path(run.cwd or ".").resolve())

    def _conversation(self) -> list[reg.AgentRun]:
        """The runs of this spawn's conversation: its caller's launch tree (`reg.conversation_of`) — a PC's web
        conversations share one session id —, else its session's; never its caller or a run above it."""
        if self.parent_run_id is None:
            return reg.list_runs(session_id=self.session_id) if self.session_id is not None else []
        above = reg.ancestors(self.parent_run_id)
        return [run for run in reg.trees(frozenset({above[-1]})) if run.run_id not in above]

    def _brief(self, run: reg.AgentRun) -> str:
        """The task as `run` takes it: a run of another folder is told this task's."""
        return self.task if Path(run.cwd or ".").resolve() == Path(self.cwd).resolve() else \
            f"Work in {self.cwd} for this task (your session started in {run.cwd}).\n\n{self.task}"

    def hand_over(self) -> Continued | StartNew:
        """Give the task to the agent of its conversation already holding its role, between turns, as its next turn
        (`messaging.deliver_message`: its own session, everything it did still in context) instead of starting
        another (owner 2026-10-15: no « backend engineer three » while engineer one sits idle in the conversation).
        Its caller's own child first, then the latest to finish; one whose turn would not start is passed over unless
        its agent may have read the task (then kept, the doubt said: never run twice). `single`: one at work first, the
        task queued behind its turn. A run started on another revision of the role takes it on the spawn's revision.
        Every same-role run busy or unfit: start new, and why. Blocks up to `messaging.wait_for_start`'s timeout per run tried."""
        if (resolved := self._role()) is None:
            return StartNew()
        policy, role, revision = resolved
        peers = self._peers(role)
        misfits = {run.run_id: self._misfit(run, policy, revision) for run in peers}
        why = [f"{run_id[:8]} {said}" for run_id, said in misfits.items() if said]
        fit = [run for run in peers if misfits[run.run_id] is None]
        turn = agent_queue.MessageTurn(limit=agent_queue.BRIEF_MAX, agent_ref=revision)
        # `single`: a fit one at work takes it as its next turn, never another started beside it.
        for run in sorted((run for run in fit if self._busy(run)), key=lambda run: run.started_at or 0, reverse=True):
            delivery = messaging.deliver_message(run.run_id, self._brief(run), only_if_idle=False, turn=turn,
                                                 for_run=self.for_run)
            if delivery.state != "error":
                return Continued(run=run, delivery=delivery)
            why.append(f"{run.run_id[:8]} {delivery.text}")
        fit = [run for run in fit if not self._busy(run)]
        for run in sorted(fit, key=lambda run: (run.parent_run_id == self.parent_run_id, run.finished_at or run.started_at), reverse=True):
            delivery = messaging.deliver_message(run.run_id, self._brief(run), only_if_idle=True, turn=turn,
                                                 for_run=self.for_run)
            refused = delivery.text if delivery.state == "error" else messaging.wait_for_start(delivery)
            if refused is None or messaging.read_by_agent(delivery):
                reg.take_over_refused(run, parent=self.parent_run_id, at=time.time())  # the launch of its role refused before it
                return Continued(run=run, delivery=delivery, doubt=refused or "")
            why.append(f"{run.run_id[:8]} {refused}")
        return StartNew(why=f"{role} already here, not continued: {'; '.join(why)}" if peers else "")



class Continued(BaseModel):
    """A spawn's task given to the agent of its conversation already holding the role (`Spawn.hand_over`)."""

    model_config = ConfigDict(frozen=True)

    run: reg.AgentRun
    delivery: messaging.Delivery
    #: Its turn did not start cleanly, yet its agent may have read the task (`messaging.read_by_agent`): said, never run twice.
    doubt: str = ""

    def sentence(self) -> str:
        doubt = f" Its turn may not have gone through: {self.doubt}; agent_events tells, resend to it if not." if self.doubt else ""
        return (f"Continued [{self.run.name}] — run_id={self.run.run_id}: the {self.run.agent} already in this conversation takes "
                f"the task as its next turn, with everything it did still in context ({self.delivery.text}).{doubt} "
                "Pass fresh to start a new one instead.")


class StartNew(BaseModel):
    """A spawn no agent of its conversation took (`Spawn.hand_over`): `why`, "" when none of its role was here."""

    model_config = ConfigDict(frozen=True)

    why: str = ""

    def note(self) -> str:
        """Said after the new run's own message: why the agents of its role here did not take it."""
        return f"\n{self.why}" if self.why else ""


class Held(BaseModel):
    """An entry of a shared list (`_listed`) that lasts while the process holding it does."""

    model_config = ConfigDict(extra="ignore")

    #: The process holding it and when that process started: an entry whose holder is gone is nobody's.
    holder: int
    holder_started: int | None

    def held(self) -> bool:
        return self.holder_started is not None and process_started(self.holder) == self.holder_started and not process_exited(self.holder)

    @classmethod
    def read(cls, raw: object) -> Self | None:
        """One stored entry; one this build cannot read is `unreadable`'s."""
        try:
            return cls.model_validate(raw)
        except ValidationError as error:
            return cls.unreadable(raw, error)

    @classmethod
    def unreadable(cls, raw: object, error: Exception) -> Self | None:
        """What an entry this build cannot read becomes: dropped."""
        return None


H = TypeVar("H", bound=Held)


@contextmanager
def _listed(name: str, kind: type[H]) -> Iterator[list[H]]:
    """Shared state `name` (`registry.shared_state`), a list of `kind`, for the block to change in place; written back
    after it, all under its lock. Never refuses: unreadable state reads as an empty list (kept aside), an unreadable
    entry as `kind.read` says."""
    with reg.shared_state(name) as (raw, replace, keep_aside):
        try:
            entries = json.loads(raw) if raw else []
        except ValueError as error:
            keep_aside()
            logger.warning("%s unreadable, kept aside and started over: %s", name, error)
            entries = []
        listed = [read for entry in (entries if isinstance(entries, list) else []) if (read := kind.read(entry)) is not None]
        before = [entry.model_dump(mode="json") for entry in listed]
        yield listed
        after = [entry.model_dump(mode="json") for entry in listed]
        if after != before:
            replace(json.dumps(after, ensure_ascii=False).encode())


class Place(Held):
    """One run in the line. `starting`: admitted, its process not on record yet; `waiting`: held by its waiter;
    `failed`: it will not start (`said` why). A resumed turn (`spawn` None) is only ever `starting`. Its holder: the
    process that asked, then its waiter; a resume's dispatcher (`SpawnLine._prune`)."""

    run_id: str
    asked_at: float
    state: Literal["starting", "waiting", "failed"]
    parent_run_id: str | None = None
    spawn: Spawn | None = None
    said: str = ""

    def asked(self) -> bool:
        """A spawn someone asked for (every place but a resumed turn's): one `agents list` shows until it starts."""
        return self.spawn is not None or self.state == "failed"

    def label(self) -> str:
        return (self.spawn.name or self.spawn.agent or "-") if self.spawn is not None else "-"

    @classmethod
    def unreadable(cls, raw: object, error: Exception) -> Place | None:
        """Kept as failed under its run id (None: it has none)."""
        run_id = raw.get("run_id") if isinstance(raw, dict) else None
        if not isinstance(run_id, str):
            return None
        asked = raw.get("asked_at")
        return cls(run_id=run_id, asked_at=asked if isinstance(asked, (int, float)) else time.time(), state="failed", holder=0, holder_started=None,
                   said=f"its place in line is unreadable by this galaius ({type(error).__name__}); spawn it again")


class Aside(Held):
    """Run `run_id` at work here, its place given back while it only waits on its agents (`SpawnLine.aside`), held by
    the process waiting."""

    run_id: str
    since: float
    #: The runs it waits on (`agents wait`); empty: a wait naming none (a deploy's).
    waits_on: list[str] = Field(default_factory=list)
    #: When its run began waiting: `since`, or an earlier wait's start when this one followed it within
    #: `SpawnLine.waiting_gap_seconds` (a loop of `agents wait` calls is one wait). None: an entry of an older galaius.
    waiting_since: float | None = None
    #: When its wait ended; None while it lasts. Kept `SpawnLine.waiting_gap_seconds` for a next wait to carry on.
    left_at: float | None = None

    def stands_aside(self, run: reg.AgentRun) -> bool:
        """Still aside: held, and its agent wrote nothing since (past `ASIDE_GRACE_SECONDS`): one that writes waits in the
        background and is at work."""
        return self.left_at is None and self.held() and (reg.vendor_said_at(run) or 0.0) <= self.since + ASIDE_GRACE_SECONDS

    def waited(self, now: float) -> float:
        """Seconds its run has waited, back-to-back waits counted as one."""
        return now - (self.waiting_since or self.since)

    def said(self, now: float) -> str:
        """Its run list line once flagged (`SpawnLine.waiting_said`): « attend 83da9bab depuis 14 min », naming the runs
        it waits on still to end (one not on record yet is still in line)."""
        pending = [reg.short_id(run_id) for run_id in self.waits_on if (run := reg.get_run(run_id)) is None or run.finished_at is None]
        more = f" +{len(pending) - WAITED_NAMED_MAX}" if len(pending) > WAITED_NAMED_MAX else ""
        return f"attend {', '.join(pending[:WAITED_NAMED_MAX]) + more or ITS_AGENTS} depuis {self.waited(now) / 60:.0f} min"


class Load(BaseModel):
    """What works here against the limit: runs, the places they take (`AtWork.load`), and the places the runs set aside gave back."""

    model_config = ConfigDict(frozen=True)

    runs: int
    places: int
    limit: int
    aside: int = 0

    @property
    def full(self) -> bool:
        return self.places >= self.limit


class Admission(BaseModel):
    """A spawn's answer: its run id, and how many wait before it (None: it starts now)."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    ahead: int | None
    load: Load

    def sentence(self) -> str:
        """« en file : 2 avant elle (8 agents au travail sur ce PC, 6 places prises sur 6) »; "" when it starts now."""
        if self.ahead is None:
            return ""
        aside = f", {self.load.aside} rendue{'s' if self.load.aside > 1 else ''} par des runs qui attendent leurs agents" if self.load.aside else ""
        return (f"en file : {self.ahead} avant elle ({self.load.runs} agents au travail sur ce PC, "
                f"{self.load.places} places prises sur {self.load.limit}{aside})")


def _places_taken(taken: dict[str, str | None]) -> int:
    """The places `taken` (run id -> its parent) take: one per run, but a run in a place of its own (its parent not at
    work here) with a child at work shares that place with one of them."""
    return len(taken) - len({parent for parent in taken.values() if parent in taken and taken[parent] not in taken})


class AtWork(BaseModel):
    """One look at who takes a place here (`SpawnLine._at_work`): every run at work and every run starting (run id -> its
    parent), the owner's own runs and the runs set aside left out; and the runs set aside (run id -> its parent)."""

    model_config = ConfigDict(frozen=True)

    taken: dict[str, str | None]
    aside: dict[str, str | None] = Field(default_factory=dict)
    #: The entry each run set aside stands under (`Aside.stands_aside`).
    entries: dict[str, Aside] = Field(default_factory=dict)

    def giving(self) -> frozenset[str]:
        """The runs set aside whose place is free: each one back at work alone would take a place. One whose child works
        frees none (the child takes it over), nor one sharing its parent's place."""
        places = _places_taken(self.taken)
        return frozenset(run_id for run_id, parent in self.aside.items() if _places_taken(self.taken | {run_id: parent}) > places)

    def load(self, limit: int) -> Load:
        places = _places_taken(self.taken)
        return Load(runs=len(self.taken), places=places, limit=limit, aside=_places_taken(self.taken | self.aside) - places)

    def shares(self, parent: str | None) -> bool:
        """Whether a child of `parent` takes no place of its own: `parent` works here in a place of its own, no child yet."""
        return parent in self.taken and self.taken[parent] not in self.taken and parent not in self.taken.values()


class SpawnLine(BaseModel):
    """This computer's line (`registry.shared_state("spawn-line")`)."""

    model_config = ConfigDict(frozen=True)

    limit: int = Field(ge=1)
    #: Minutes a run waits on its agents before the run list says which and since when (`waiting_said`).
    waiting_flag_minutes: float = Field(gt=0)
    #: Seconds between two waits of a run within which the second carries on the first (`Aside.waiting_since`).
    waiting_gap_seconds: float = Field(ge=0)

    @classmethod
    def here(cls) -> SpawnLine:
        config = Config()
        return cls(limit=config.agent_runs_max, waiting_flag_minutes=config.agent_waiting_flag_minutes,
                   waiting_gap_seconds=config.agent_waiting_gap_seconds)

    @contextmanager
    def _places(self) -> Iterator[list[Place]]:
        """The line, pruned (`_listed`). Taken before `_asides` whenever both are."""
        with _listed("spawn-line", Place) as places:
            places[:] = self._prune(places)
            yield places

    @contextmanager
    def _asides(self) -> Iterator[list[Aside]]:
        """The runs set aside, those whose holder is gone dropped, and the waits ended within `waiting_gap_seconds`. Its
        own state: a waiter of an older galaius rewrites the line, and would turn an entry it cannot read there into a
        failed spawn."""
        now = time.time()
        with _listed("spawn-aside", Aside) as asides:
            asides[:] = [aside for aside in asides if (now - aside.left_at <= self.waiting_gap_seconds if aside.left_at is not None else aside.held())]
            yield asides

    @staticmethod
    def _prune(places: list[Place]) -> list[Place]:
        """A place whose holder is gone: `starting` — dropped once its run is on record, else failed (its start was
        cut off); `waiting` — failed, quoting what its waiter wrote. A failed one is forgotten after `FAILED_KEPT_SECONDS`."""
        now, kept = time.time(), []
        for place in places:
            if place.state == "failed":
                if now - place.asked_at < FAILED_KEPT_SECONDS:
                    kept.append(place)
                elif reg.get_run(place.run_id) is None:
                    reg.stderr_path(place.run_id).unlink(missing_ok=True)
            elif place.held():
                kept.append(place)
            elif place.spawn is not None and reg.get_run(place.run_id) is None:
                said = reg.read_stderr(place.run_id, 300).strip()
                gone = "the process holding its place in line went away" if place.state == "waiting" else "its start was cut off"
                kept.append(place.model_copy(update={"state": "failed", "said": f"{gone}{': ' + said if said else ''}; spawn it again"}))
        return kept

    @staticmethod
    def _owners() -> frozenset[str]:
        """The runs the owner started himself from the web (`WebRuns`): never counted."""
        try:
            return frozenset(str(run.run_id) for run in WebRuns.in_folder(UserPaths.config()).read())
        except (OSError, ValueError):
            return frozenset()

    def _at_work(self, places: list[Place]) -> AtWork:
        """Who takes a place now, `places` (the line, read under its lock) adding the runs starting."""
        owners = self._owners()
        runs = [run for run in reg.running_runs() if run.kind == "process" and not run.foreign and run.run_id not in owners]
        with self._asides() as asides:
            listed = list(asides)
        entries = {run.run_id: entry for run in runs for entry in listed if entry.run_id == run.run_id and entry.stands_aside(run)}
        aside = {run.run_id: run.parent_run_id for run in runs if run.run_id in entries}
        taken = {run.run_id: run.parent_run_id for run in runs if run.run_id not in aside}
        return AtWork(taken=taken | {place.run_id: place.parent_run_id for place in places if place.state == "starting"}, aside=aside, entries=entries)

    def set_aside(self) -> frozenset[str]:
        """The runs at work here whose place is free while they wait on their agents (`aside`, `AtWork.giving`)."""
        return self._at_work([]).giving()

    def load(self, places: list[Place]) -> Load:
        return self._at_work(places).load(self.limit)

    def waiting_said(self) -> dict[str, str]:
        """What the run list says of each run waiting on its agents, in place of its last activity: past
        `waiting_flag_minutes`, which runs and since when (`Aside.said`, 2026-10-17 « still wasting time / inference /
        usage on stupid things »); before, `ASIDE` while its place is free."""
        at_work, now, flag = self._at_work([]), time.time(), self.waiting_flag_minutes * 60
        giving = at_work.giving()
        return {run_id: entry.said(now) if entry.waited(now) >= flag else ASIDE
                for run_id, entry in at_work.entries.items() if run_id in giving or entry.waited(now) >= flag}

    def _admits(self, places: list[Place], parent: str | None, *, first: bool) -> bool:
        """Whether a run of `parent` starts now: it shares its parent's place, or a place is free and it is `first` in line
        or its parent is set aside, its place given back (`AtWork.giving`): the child takes it ahead of the line."""
        at_work = self._at_work(places)
        return at_work.shares(parent) or ((first or parent in at_work.giving()) and not at_work.load(self.limit).full)

    @contextmanager
    def aside(self, run_id: str | None, waits_on: Sequence[str] = ()) -> Iterator[None]:
        """Run `run_id` (the run asking, `GALAIUS_RUN_ID`) only waits on its agents `waits_on` for the block (`agents wait`): its
        place is free meanwhile, a hand-off chain holding none per waiting ancestor. Leaving, it counts again at once,
        every entry of its own ended (a second wait of the same run included); a next wait within `waiting_gap_seconds`
        carries on its start. A run not at work here, or never counted (`_exempt`), takes the block as it is."""
        run = reg.get_run(run_id) if run_id else None
        if run is None or self._exempt(run) or not run.process_running():
            yield
            return
        me = os.getpid()
        now = time.time()
        with self._asides() as asides:
            earlier = [aside.waiting_since or aside.since for aside in asides if aside.run_id == run.run_id and aside.left_at is not None]
            asides[:] = [aside for aside in asides if aside.run_id != run.run_id or aside.left_at is None]
            asides.append(Aside(run_id=run.run_id, since=now, waits_on=list(waits_on), waiting_since=min(earlier, default=now),
                                holder=me, holder_started=process_started(me)))
        try:
            yield
        finally:
            with self._asides() as asides:
                left = time.time()
                asides[:] = [aside.model_copy(update={"left_at": left}) if aside.run_id == run.run_id and aside.left_at is None else aside
                             for aside in asides]

    def enter(self, spawn: Spawn) -> Admission:
        """Take a place for `spawn`: `starting` when it may start now (the caller starts it, then `leave`s), else
        `waiting` behind every spawn already waiting, held by a waiter started here."""
        run_id, me = str(uuid.uuid4()), os.getpid()
        with self._places() as places:
            ahead = sum(place.state == "waiting" for place in places)
            load = self.load(places)
            starts = self._admits(places, spawn.parent_run_id, first=ahead == 0)
            places.append(Place(run_id=run_id, asked_at=time.time(), state="starting" if starts else "waiting", holder=me,
                                holder_started=process_started(me), parent_run_id=spawn.parent_run_id, spawn=spawn))
        if not starts:
            self._hold(run_id, spawn, me)
        return Admission(run_id=run_id, ahead=None if starts else ahead, load=load)

    def _hold(self, run_id: str, spawn: Spawn, asker: int) -> None:
        """Start the waiter holding `run_id`'s place, writing to the run's own stderr file (`_prune` quotes it)."""
        try:
            with reg.open_stderr(run_id, append=True) as stderr:
                process = reg.start_participant("galaius.agents.spawn_line", run_id, cwd=spawn.cwd, contain=True, stderr=stderr)
        except OSError as error:
            self._settle(run_id, f"its waiter could not start: {error}")
            raise
        with self._places() as places:  # unless the waiter already took it over itself (`_turn`)
            for index, place in enumerate(places):
                if place.run_id == run_id and place.state == "waiting" and place.holder == asker:
                    places[index] = place.model_copy(update={"holder": process.pid, "holder_started": process_started(process.pid)})

    def leave(self, run_id: str) -> None:
        """Its run is on record (counted from there) or never started: the place is free."""
        with self._places() as places:
            places[:] = [place for place in places if place.run_id != run_id]

    def _settle(self, run_id: str, said: str) -> None:
        with self._places() as places:
            for index, place in enumerate(places):
                if place.run_id == run_id:
                    places[index] = place.model_copy(update={"state": "failed", "said": said[:500]})

    def _exempt(self, run: reg.AgentRun) -> bool:
        """The owner's own runs (`_owners`) and what is not a galaius process here: never held."""
        return run.kind != "process" or run.foreign or run.run_id in self._owners()

    @contextmanager
    def resuming(self, run: reg.AgentRun) -> Iterator[bool]:
        """Whether `run`'s next turn may start now (`agent_queue.dispatch`, which asks again every `POLL_SECONDS` until it
        may). Its place waits in line like a spawn's, held by the dispatcher asking, and starts in the same order (or at
        once when it shares its parent's place); while the block starts the turn, the place is taken."""
        if self._exempt(run):
            yield True
            return
        me = os.getpid()
        with self._places() as places:
            place = next((place for place in places if place.run_id == run.run_id), None)
            if place is None or place.state == "failed":
                if place is not None:
                    places.remove(place)
                place = Place(run_id=run.run_id, asked_at=time.time(), state="waiting", holder=me,
                              holder_started=process_started(me), parent_run_id=run.parent_run_id)
                places.append(place)
            index = places.index(place)
            head = next((other for other in places if other.state == "waiting"), None)
            admitted = place.state == "waiting" and self._admits(places, run.parent_run_id, first=head is place)
            if admitted:
                places[index] = place.model_copy(update={"state": "starting", "holder": me, "holder_started": process_started(me)})
        try:
            yield admitted
        finally:
            if admitted:
                self.leave(run.run_id)

    def waits(self, run_id: str) -> str | None:
        """Where `run_id`'s next turn stands when it waits for a place (`agents send`): its place's line, or, its dispatcher
        not having asked yet, the one it is about to join; None when it starts now (or the run is at work: what is sent
        reaches its turn running)."""
        run = reg.get_run(run_id)
        if run is None or self._exempt(run) or run.working():
            return None
        with self._places() as places:
            if (place := next((place for place in places if place.run_id == run_id), None)) is not None:
                return None if place.state == "starting" else self._said(places, place)
            waiting = sum(place.state == "waiting" for place in places)
            if self._admits(places, run.parent_run_id, first=waiting == 0):
                return None
            return Admission(run_id=run_id, ahead=waiting, load=self.load(places)).sentence()

    @staticmethod
    def _match(places: list[Place], prefix: str) -> Place | None:
        """The one spawn not started whose run id starts with `prefix` (`agents list` prints 8 characters); None when none or several."""
        found = [place for place in places if prefix and place.asked() and place.run_id.startswith(prefix)]
        return found[0] if len(found) == 1 else None

    def _said(self, places: list[Place], place: Place) -> str:
        if place.state == "failed":
            return f"not started: {place.said}"
        if place.state == "starting":
            return "starting"
        ahead = [other.run_id for other in places if other.state == "waiting"].index(place.run_id)
        said = Admission(run_id=place.run_id, ahead=ahead, load=self.load(places)).sentence()
        return f"{said} — {STUCK}" if self._stuck(places) else said

    def _stuck(self, places: list[Place]) -> bool:
        """Every place is held by a team one of whose runs waits in line: should each wait for that run, none frees."""
        at_work = self._at_work(places)
        taken = at_work.taken

        def team(run_id: str) -> str:
            """The run in a place of its own whose team `run_id` is in (up its parents while they work here)."""
            seen = set()
            while (parent := taken.get(run_id)) in taken and run_id not in seen:
                seen.add(run_id)
                run_id = parent
            return run_id

        holders = {team(run_id) for run_id in taken}
        waiting = {team(place.parent_run_id) for place in places if place.state == "waiting" and place.parent_run_id in taken}
        return at_work.load(self.limit).full and bool(holders) and holders <= waiting

    def cancel(self, prefix: str, *, within: frozenset[str] | None = None) -> str | None:
        """Take a spawn not started out of the line (`agents stop`): its run id, None when none matches (or its parent
        is not in `within`, the asking team, when given)."""
        with self._places() as places:
            found = self._match(places, prefix)
            if found is None or found.state == "starting" or (within is not None and found.parent_run_id not in within):
                return None
            places.remove(found)
        return found.run_id

    def describe(self, prefix: str, *, within: frozenset[str] | None = None) -> str | None:
        """One line on a spawn not started (`agents events` / `agent_events`), None when none matches (or its parent is
        not in `within`, the asking team, when given)."""
        with self._places() as places:
            found = self._match(places, prefix)
            if found is None or (within is not None and found.parent_run_id not in within):
                return None
            return self._said(places, found)

    def session(self, session_id: str | None) -> list[tuple[Place, str]]:
        """The spawns not started of conversation `session_id` (every one when None), each with its line (`describe`)."""
        with self._places() as places:
            return [(place, self._said(places, place)) for place in places if place.asked() and place.state != "starting"
                    and (session_id is None or (place.spawn is not None and place.spawn.session_id == session_id))]

    def _turn(self, run_id: str) -> Place | Literal["wait", "gone"]:
        """The waiter's look: `run_id`'s place, marked `starting`, when it may start now; else whether it waits or is gone
        (cancelled). The waiter holds the place from its first look on."""
        me = os.getpid()
        with self._places() as places:
            place = next((place for place in places if place.run_id == run_id), None)
            if place is None or place.state != "waiting" or place.spawn is None:
                return "gone"
            index = places.index(place)
            if place.holder != me:
                places[index] = place = place.model_copy(update={"holder": me, "holder_started": process_started(me)})
            head = next(other for other in places if other.state == "waiting")
            if not self._admits(places, place.parent_run_id, first=head.run_id == run_id):
                return "wait"
            places[index] = place = place.model_copy(update={"state": "starting"})
            return place

    def wait(self, run_id: str) -> None:
        """The waiter: once `run_id` may start, start it, then free its place; ends when the place is gone."""
        while (turn := self._turn(run_id)) == "wait":
            time.sleep(POLL_SECONDS)
        if turn == "gone":
            return
        assert turn.spawn is not None
        try:
            asyncio.run(turn.spawn.start(run_id))
        except Exception as error:  # every way a start fails: the place says why, the run id resolves to it
            self._settle(run_id, str(error) or type(error).__name__)
            return
        self.leave(run_id)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m galaius.agents.spawn_line RUN_ID")
    RuntimeStore.default().register(Runtime.own(), os.getpid())  # its runtime stays until it ends
    SpawnLine.here().wait(sys.argv[1])


if __name__ == "__main__":
    main()
