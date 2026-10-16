"""Durable, one-writer continuation queue for one agent run.

The vendor ``queue`` command is not a delivery boundary: it can acknowledge a message and then
lose it when the short-lived CLI process exits. This queue stores only delivery metadata; message
text remains in the recipient transcript. A detached dispatcher owns the provider process until
each queued turn finishes, or the long-lived launcher holding the run's next child does
(`galaius.agents.followup`).
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from galaius_core import AGENT_IMAGES_MAX, MEDIA_KEY
from pydantic import BaseModel, ConfigDict, Field

from galaius.agents import registry as reg
from galaius.agents.events import AgentEvent
from galaius.agents.providers import _CLIP, AgentProvider, _safe_process_detail, provider_for
from galaius.inbox_hook import HEADING, OPERATOR
from galaius.processes import process_started
from galaius.upgrade.store import Runtime, RuntimeStore

MAX_PENDING = 128
_POLL_SECONDS = 0.1
_DISPATCHER_RECOVERY_TIMEOUT = 30.0


class CorruptQueueStateError(RuntimeError):
    """The durable queue cannot be trusted and was left untouched."""


QueueState = Literal["pending", "running", "replied", "failed", "cancelled", "uncertain"]
#: What a queue file holds only while one of its items is still pending (its state, as written).
PENDING_MARK = '"pending"'
AttemptState = Literal["replied", "failed", "uncertain"]


class QueuedTurn(BaseModel, ABC):
    """What a queued turn does. Each kind owns how it is checked when queued, what its recipient's
    transcript records, what its turn sends, whether a turn already running may read it, and the event
    that accepts its attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: A turn already running may read it at its next step (the inbox hook), when its CLI reads mid-turn
    #: and the run is unfenced; otherwise it is a turn of its own, and what was queued after it waits behind it.
    mid_turn: ClassVar[bool]
    #: Its attempt's error when its turn ended without an event accepting it (`accepts`).
    unaccepted: ClassVar[str]

    @abstractmethod
    def refusal(self, text: str, provider: AgentProvider) -> str | None:
        """Why it cannot be queued for a run of `provider`; None when it can."""

    @abstractmethod
    def entry(self, text: str, origin: reg.AgentRun | None) -> str | None:
        """What its recipient's transcript records as sent to it by `origin` (None: the person); None: nothing."""

    @abstractmethod
    def prompt(self, run: reg.AgentRun, message_id: str, *, native_images: bool = False) -> str | None:
        """What its turn sends to `run`; None when its transcript entry is missing. The images it shows are named there
        as their files (`RunMedia.mentioned`) unless the turn carries them itself (`native_images`: `shown`)."""

    def shown(self, run: reg.AgentRun) -> tuple[Path, ...]:
        """The files of the images it shows, in order."""
        return ()

    @abstractmethod
    def accepts(self, event: AgentEvent) -> bool:
        """Whether `event`, of its attempt, accepts it."""


#: The longest task a spawn hands to an agent already in its conversation: a coordinator's brief, never clipped.
BRIEF_MAX = 64_000


class MessageTurn(QueuedTurn):
    """A message: recorded on its recipient's transcript, read by the turn running when its CLI can."""

    kind: Literal["message"] = "message"
    #: The images it shows, by their name among its run's media (`RunMedia`): its transcript entry names them too.
    media: tuple[Annotated[str, Field(pattern=MEDIA_KEY)], ...] = Field(default=(), max_length=AGENT_IMAGES_MAX)
    #: The longest text it takes: a follow-up is transcript-sized; a spawn's task handed to an agent already here
    #: (`spawn_line.Spawn.hand_over`) or a parent's agents' reports (`handback`) up to `BRIEF_MAX`.
    limit: int = Field(default=_CLIP, gt=0, le=BRIEF_MAX, exclude_if=lambda value: value == _CLIP)
    mid_turn = True
    unaccepted = "resumed turn completed without a reply; resend explicitly"

    def refusal(self, text: str, provider: AgentProvider) -> str | None:
        """The same transcript-sized input boundary on every sending surface."""
        if not text.strip() and not self.media:
            return "message must contain text or an image."
        return f"message exceeds the {self.limit}-character limit." if len(text) > self.limit else None

    def entry(self, text: str, origin: reg.AgentRun | None) -> str | None:
        return text if origin is None else origin.handoff_header() + text

    def prompt(self, run: reg.AgentRun, message_id: str, *, native_images: bool = False) -> str | None:
        message = reg.message_for(run.run_id, message_id)
        if message is None:
            return None
        return message.text if native_images else reg.RunMedia.mentioned(message.text, self.shown(run))

    def shown(self, run: reg.AgentRun) -> tuple[Path, ...]:
        return tuple(reg.RunMedia.path(run.run_id, name) for name in self.media)

    def accepts(self, event: AgentEvent) -> bool:
        return (event.kind == "text" or (event.kind == "done" and event.final_text)) and bool(event.text.strip())


class CompactionTurn(QueuedTurn):
    """A compaction: its CLI's own command (`AgentProvider.compact_prompt`) as a turn of its own; nobody wrote
    it, so nothing is recorded as sent; its `compacted` event accepts it."""

    kind: Literal["compact"] = "compact"
    mid_turn = False
    unaccepted = "the provider did not compact the conversation"

    def refusal(self, text: str, provider: AgentProvider) -> str | None:
        return self.refused_for(provider.name)

    @staticmethod
    def refused_for(provider: str) -> str | None:
        """Why a run of `provider` cannot compact by its CLI's own command (`AgentProvider.compact_prompt`); None
        when it can: the rule a queued compaction obeys, and every surface saying before a press whether one can."""
        try:
            command = provider_for(provider).compact_prompt
        except ValueError as unknown:
            return str(unknown)
        return f"the {provider!r} CLI cannot compact its conversation." if command is None else None

    def entry(self, text: str, origin: reg.AgentRun | None) -> str | None:
        return None

    def prompt(self, run: reg.AgentRun, message_id: str, *, native_images: bool = False) -> str | None:
        return provider_for(run.provider).compact_prompt

    def accepts(self, event: AgentEvent) -> bool:
        return event.kind == "compacted"


QueuedTurnValue = Annotated[MessageTurn | CompactionTurn, Field(discriminator="kind")]


class QueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    message_id: str = Field(min_length=1, max_length=160)
    sender: str = Field(min_length=1, max_length=160)
    turn: QueuedTurnValue = MessageTurn()
    state: QueueState = "pending"
    enqueued_at: float
    started_at: float | None = None
    finished_at: float | None = None
    raw_index: int | None = None
    attempt_token: str | None = Field(default=None, min_length=1, max_length=80)
    error: str = ""

    @property
    def writer(self) -> str:
        """Who sent it, to the run it was sent to: the person, another run, or the launcher itself
        (`registry.LIMIT_RESUME_SENDER`)."""
        if self.sender == "operator":
            return OPERATOR
        origin = reg.get_run(self.sender)
        return f"another agent, {origin.name} (run {self.sender[:8]})" if origin is not None else f"Galaius on this computer ({self.sender})"


def path(run_id: str):
    return reg.agents_dir() / f"{reg._safe_run_id(run_id)}.queue.json"


def _state(run_id: str) -> dict:
    payload = reg._read_private(path(run_id))
    if payload is None:
        return {"version": 1, "items": [], "dispatcher_pid": None}
    try:
        state = json.loads(payload)
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorruptQueueStateError(
            "durable agent queue is corrupt; refusing to claim delivery"
        ) from error
    if not isinstance(state, dict) or state.get("version", 1) != 1:
        raise CorruptQueueStateError("durable agent queue has an unsupported state")
    if not isinstance(state.get("items", []), list):
        raise CorruptQueueStateError("durable agent queue items are corrupt")
    try:
        _items(state)
    except CorruptQueueStateError:
        raise
    pid = state.get("dispatcher_pid")
    token = state.get("dispatcher_token")
    if pid is not None and (type(pid) is not int or pid <= 0):
        raise CorruptQueueStateError("durable agent queue dispatcher identity is corrupt")
    if token is not None and (not isinstance(token, str) or not token or len(token) > 80):
        raise CorruptQueueStateError("durable agent queue dispatcher token is corrupt")
    started = state.get("dispatcher_started")
    if started is not None and (type(started) is not int or started < 0):
        raise CorruptQueueStateError("durable agent queue dispatcher start time is corrupt")
    return state


def _write_state(run_id: str, state: dict) -> None:
    reg._replace_private(path(run_id), json.dumps(state, ensure_ascii=False).encode())


def _items(state: dict) -> list[QueueItem]:
    try:
        return [QueueItem.model_validate(raw) for raw in state.get("items", [])]
    except (TypeError, ValueError) as error:
        raise CorruptQueueStateError("durable agent queue item is corrupt") from error


def can_enqueue_locked(run_id: str) -> bool:
    """Check capacity while the recipient lock is held, before writing its transcript."""
    state = _state(run_id)
    return sum(item.state in {"pending", "running"} for item in _items(state)) < MAX_PENDING


def enqueue_locked(run_id: str, *, message_id: str, sender: str, turn: QueuedTurn = MessageTurn()) -> QueueItem | None:
    """Append one item while the caller owns ``registry.record_lock(run_id)``."""
    state = _state(run_id)
    items = _items(state)
    if any(item.message_id == message_id for item in items):
        return next(item for item in items if item.message_id == message_id)
    if sum(item.state in {"pending", "running"} for item in items) >= MAX_PENDING:
        return None
    item = QueueItem(
        id=uuid.uuid4().hex,
        message_id=message_id,
        sender=sender,
        turn=turn,
        enqueued_at=time.time(),
    )
    state["version"] = 1
    state["items"] = [*state.get("items", []), item.model_dump()]
    _write_state(run_id, state)
    return item


def enqueue(run_id: str, *, message_id: str, sender: str, turn: QueuedTurn = MessageTurn()) -> QueueItem | None:
    with reg.record_lock(run_id):
        return enqueue_locked(run_id, message_id=message_id, sender=sender, turn=turn)


def _replace_item_locked(run_id: str, item_id: str, **updates) -> QueueItem | None:
    state = _state(run_id)
    items = _items(state)
    selected: QueueItem | None = None
    rendered = []
    for item in items:
        if item.id == item_id:
            item = item.model_copy(update=updates)
            selected = item
        rendered.append(item.model_dump())
    if selected is None:
        return None
    state["items"] = rendered
    _write_state(run_id, state)
    return selected


def claim_next_locked(run_id: str) -> QueueItem | None:
    """Claim the oldest pending item while the caller owns the run lock."""
    state = _state(run_id)
    item = next((item for item in _items(state) if item.state == "pending"), None)
    if item is None:
        return None
    return _replace_item_locked(
        run_id, item.id, state="running", started_at=time.time(),
        raw_index=reg.raw_line_count(run_id), attempt_token=uuid.uuid4().hex, error="",
    )


def first_active_locked(run_id: str) -> QueueItem | None:
    """The run's first item waiting or being delivered, while the caller owns the run lock."""
    return next((item for item in _items(_state(run_id)) if item.state in {"pending", "running"}), None)


def claim_locked(run_id: str, item_id: str) -> QueueItem | None:
    """Claim `item_id` while the caller owns the run lock, when it is the first item waiting or
    running (one ahead of it is delivered first)."""
    first = first_active_locked(run_id)
    if first is None or first.id != item_id or first.state != "pending":
        return None
    return _replace_item_locked(run_id, item_id, state="running", started_at=time.time(),
                                raw_index=reg.raw_line_count(run_id), attempt_token=uuid.uuid4().hex, error="")


def release_locked(run_id: str, item_id: str) -> QueueItem | None:
    """Put a claimed item whose child never received it back in the queue, for the dispatcher."""
    return _replace_item_locked(run_id, item_id, state="pending", started_at=None, raw_index=None, attempt_token=None, error="")


def mark_locked(
    run_id: str, item_id: str, state: QueueState, *, error: str = "",
) -> QueueItem | None:
    """Update one item while the caller owns the recipient lock."""
    return _replace_item_locked(
        run_id, item_id, state=state, finished_at=time.time(), error=error[:500],
    )


def mark(run_id: str, item_id: str, state: QueueState, *, error: str = "") -> QueueItem | None:
    with reg.record_lock(run_id):
        return mark_locked(run_id, item_id, state, error=error)


def cancel_pending_locked(run_id: str) -> None:
    """Cancel all unstarted work. Called by ``registry.stop`` under the run lock."""
    state = _state(run_id)
    now = time.time()
    changed = False
    rendered = []
    for item in _items(state):
        if item.state in {"pending", "running"}:
            item = item.model_copy(update={"state": "cancelled", "finished_at": now})
            changed = True
        rendered.append(item.model_dump())
    if changed:
        state["items"] = rendered
        _write_state(run_id, state)


def reads_next_step(run: reg.AgentRun) -> bool:
    """Whether a turn running on `run` reads a mid-turn message at its next step (`inject`, through the inbox hook): its
    CLI reads mid-turn and the run is unfenced (the hook cannot claim a message from inside the fence)."""
    return run.fence is None and provider_for(run.provider).reads_mid_turn


def next_step_read(run: reg.AgentRun) -> QueueItem | None:
    """The message the turn running on `run` reads at its next step, in `inject`'s order: its first pending item, when
    that is a mid-turn kind and `reads_next_step`; None when what waits for it is read only once that turn ends."""
    first = next((item for item in items(run.run_id) if item.state == "pending"), None)
    return first if first is not None and first.turn.mid_turn and reads_next_step(run) else None


def items(run_id: str) -> list[QueueItem]:
    with reg.record_lock(run_id):
        return items_locked(run_id)


def item(run_id: str, item_id: str | None) -> QueueItem | None:
    with reg.record_lock(run_id):
        return item_locked(run_id, item_id)


def item_locked(run_id: str, item_id: str | None) -> QueueItem | None:
    """Item `item_id` of the run's queue while the caller owns the run lock (None: there is none)."""
    return next((queued for queued in _items(_state(run_id)) if queued.id == item_id), None)


def items_locked(run_id: str) -> list[QueueItem]:
    """The run's queue items while the caller owns the run lock."""
    return _items(_state(run_id))


def _set_dispatcher_locked(
    run_id: str, pid: int | None, token: str | None = None, started: int | None = None,
) -> None:
    state = _state(run_id)
    state["dispatcher_pid"] = pid
    state["dispatcher_token"] = token
    state["dispatcher_started"] = started
    _write_state(run_id, state)


def dispatcher_running_locked(run_id: str) -> bool:
    """Whether run `run_id`'s recorded dispatcher still runs, while the caller owns the run lock."""
    return _dispatcher_matches(_state(run_id))


def _dispatcher_matches(state: dict) -> bool:
    """Whether the dispatcher `state` records is still that very process: alive, and started when
    it was recorded to (a bare pid may since name an unrelated process). Where the system gives no
    start time (`process_started` is None) no dispatcher is ever trusted: one is started."""
    pid, token, started = (state.get(key) for key in ("dispatcher_pid", "dispatcher_token", "dispatcher_started"))
    return (
        isinstance(pid, int) and bool(token) and started is not None
        and reg._alive(pid) and process_started(pid) == started
    )


def ensure_dispatcher_locked(run_id: str, *, cwd: str = ".", environment: Mapping[str, str] | None = None) -> int:
    """Start exactly one detached dispatcher while the caller owns the run lock, in `environment`
    (this process's own when None)."""
    state = _state(run_id)
    if _dispatcher_matches(state):
        return state["dispatcher_pid"]
    token = uuid.uuid4().hex
    state["dispatcher_pid"] = None
    state["dispatcher_token"] = token
    _write_state(run_id, state)
    try:
        process = reg.start_participant("galaius.agents.agent_queue", "--dispatch", run_id, token, cwd=cwd, environment=environment)
    except BaseException:
        current = _state(run_id)
        if current.get("dispatcher_token") == token:
            _set_dispatcher_locked(run_id, None)
        raise
    _set_dispatcher_locked(run_id, process.pid, token, process_started(process.pid))
    return process.pid


def ensure_dispatcher(run_id: str, *, cwd: str = ".") -> int:
    with reg.record_lock(run_id):
        return ensure_dispatcher_locked(run_id, cwd=cwd)


def inbox_hook(run_id: str) -> str:
    """The command a turn of `run_id` runs after each of its tool calls (`galaius.inbox_hook`): it
    hands the turn every message sent to it meanwhile (`inject`). It runs after EVERY tool call, so
    where a POSIX shell runs it, the shell itself looks for a pending item in the queue file and
    starts Python only when there is one: nothing waiting costs a `grep`, never an interpreter."""
    command = reg.hook_command("galaius.inbox_hook", run_id, str(path(run_id)))
    if sys.platform == "win32":
        return command
    return f"if grep -qs {shlex.quote(PENDING_MARK)} {shlex.quote(str(path(run_id)))}; then exec {command}; fi"


def inject(run_id: str) -> list[str]:
    """Every message still pending for `run_id`, handed to the turn running now, each opening with
    who wrote it (`galaius.inbox_hook.HEADING`): each claimed as an attempt of that turn, anchored here, so
    the turn's own reply settles it (`dispatch` classifies it once the turn ends, as it does an
    attempt whose dispatcher died). What it says is never sent a second time; a message whose
    transcript entry is missing fails as `dispatch` fails it."""
    said = []
    with reg.record_lock(run_id):
        run = reg.get_run(run_id)
        if run is None or not run.working():
            return []
        anchor = reg.raw_line_count(run_id)
        for item in _items(_state(run_id)):
            if item.state != "pending":
                continue
            if not item.turn.mid_turn:
                break  # a turn of its own once this one ends; what was sent after it waits behind it
            text = item.turn.prompt(run, item.message_id)
            if text is None:
                _replace_item_locked(run_id, item.id, state="failed", finished_at=time.time(), error="message transcript entry is missing")
                continue
            _replace_item_locked(run_id, item.id, state="running", started_at=time.time(), raw_index=anchor, attempt_token=uuid.uuid4().hex, error="")
            said.append((item, text))
    return [f"{HEADING.format(who=item.writer)}\n\n{text}" for item, text in said]


#: What a turn has read of the messages sent to it: handed to that turn, or a turn opened on it.
READ: frozenset[QueueState] = frozenset({"running", "replied", "uncertain"})


def read_ids(run_id: str) -> list[str]:
    """The message ids (`QueueItem.message_id`) its agent has read (`READ`), the latest read first."""
    read = [item for item in items(run_id) if item.state in READ]
    return [item.message_id for item in sorted(read, key=lambda item: item.started_at or item.enqueued_at, reverse=True)]


def _fresh_policy(run: reg.AgentRun):
    # Circular layers: messaging imports this module (enqueue), and run imports it too (the inbox
    # hook); imported only in the detached child.
    from galaius.agents.messaging import policy_for_continuation, provider_for

    provider = provider_for(run.provider)
    _, criterion, model, reasoning = policy_for_continuation(run, provider)
    return provider, criterion, model, reasoning


def _classify_attempt(run_id: str, item: QueueItem) -> tuple[AttemptState, str]:
    """Classify one persisted provider attempt without treating exit as acceptance (`QueuedTurn.accepts`)."""
    raw_index = item.raw_index
    if raw_index is None:
        return "uncertain", "resumed turn has no persisted attempt anchor; resend explicitly"
    events = reg.read_events(run_id)
    attempt_events = [
        event for event in events
        if event.raw_index is not None and event.raw_index >= raw_index
    ]
    accepted = any(item.turn.accepts(event) for event in attempt_events)
    terminal = next(
        (event for event in reversed(attempt_events) if event.kind in {"done", "error"}),
        None,
    )
    if terminal is not None and terminal.kind == "done" and accepted:
        return "replied", ""
    if terminal is not None:
        return "failed", item.turn.unaccepted
    return "uncertain", "resumed turn acceptance was not confirmed; resend explicitly"


def settle(run_id: str, item: QueueItem, code: int) -> None:
    """Record how the attempt `item` ended once its provider child exited with `code`: replied,
    failed or uncertain (never sent again); a child that died saying nothing fails the run's turn."""
    if code:
        detail = reg.read_stderr(run_id, 500)
        error = f"resumed provider exited {code}"
        if detail:
            error += f": {_safe_process_detail(detail)}"
        # Read before the lock: `read_events` locks the record itself. The provider's own
        # last word (a quota refusal, a vendor error) outranks the exit code; a child that
        # died saying nothing gets the exit code and its stderr as the run's last line.
        said = reg.last_event(run_id)
        stopped = code in (-signal.SIGTERM, 128 + signal.SIGTERM)
        with reg.record_lock(run_id):
            current = item_locked(run_id, item.id)
            if current is not None and current.state == "running":
                _replace_item_locked(
                    run_id, item.id, state="failed", finished_at=time.time(), error=error,
                )
            if not stopped and (said is None or said.kind != "error" or not said.text.strip()):
                reg.fail_turn_locked(run_id, error, exit_code=code)
    else:
        attempt_state, attempt_error = _classify_attempt(run_id, item)
        with reg.record_lock(run_id):
            current = item_locked(run_id, item.id)
            if current is not None and current.state == "running":
                _replace_item_locked(
                    run_id, item.id, state=attempt_state, finished_at=time.time(),
                    error=attempt_error,
                )


def dispatch(run_id: str, dispatcher_token: str | None = None) -> None:
    """Drain one run serially; safe to call again after a dispatcher crash."""
    own_pid = os.getpid()
    try:
        while True:
            recover: tuple[QueueItem, reg.AgentRun] | None = None
            pause = _POLL_SECONDS
            with reg.record_lock(run_id):
                state = _state(run_id)
                queued_items = _items(state)
                pending = next((item for item in queued_items if item.state == "pending"), None)
                running = next((item for item in queued_items if item.state == "running"), None)
                run = reg.get_run(run_id)
                if pending is None and running is None:
                    if (state.get("dispatcher_pid") == own_pid
                            and state.get("dispatcher_token") == dispatcher_token):
                        _set_dispatcher_locked(run_id, None)
                    return
                if run is None:
                    cancel_pending_locked(run_id)
                    return
                if run.working():
                    item = None
                elif running is not None:
                    # The previous dispatcher may have died after the provider child finished but
                    # before it could persist the item's terminal state. Inspect the transcript
                    # before replaying the message.
                    recover = (running, run)
                    item = None
                else:
                    # Circular layers: spawn_line imports run (its turns' inbox hook) and machine_agents (the web's runs),
                    # both of which import this module.
                    from galaius.agents.spawn_line import POLL_SECONDS, SpawnLine

                    # A resumed turn is a run at work like a spawn: it waits for a place on this computer.
                    with SpawnLine.here().resuming(run) as admitted:
                        pause = _POLL_SECONDS if admitted else POLL_SECONDS
                        item = claim_next_locked(run_id) if admitted else None
                        if item is not None:
                            prompt = item.turn.prompt(run, item.message_id)
                            if prompt is None:
                                _replace_item_locked(
                                    run_id, item.id, state="failed", finished_at=time.time(),
                                    error="message transcript entry is missing",
                                )
                                item = None
                            else:
                                try:
                                    provider, criterion, model, reasoning = _fresh_policy(run)
                                    # Circular layers: run imports this module (its turns' inbox hook).
                                    from galaius.agents.run import launch_continuation
                                    lifecycle = launch_continuation(
                                        provider, run, run.provider_session_id or run.run_id,
                                        prompt, model=model, criterion=criterion,
                                        reasoning=reasoning, raw_index=item.raw_index or 0,
                                        record_locked=True,
                                    )
                                except Exception as error:
                                    # The run itself reads failed, with the reason: a message to an
                                    # ended run whose resume cannot start otherwise leaves it "done"
                                    # and its sender watching a message "delivered" forever.
                                    detail = _safe_process_detail(str(error))
                                    _replace_item_locked(
                                        run_id, item.id, state="failed", finished_at=time.time(),
                                        error=detail,
                                    )
                                    reg.fail_turn_locked(run_id, f"resume refused: {detail}")
                                    item = None
            if recover is not None:
                recovered_item, _ = recover
                attempt_state, attempt_error = _classify_attempt(run_id, recovered_item)
                with reg.record_lock(run_id):
                    current = item_locked(run_id, recovered_item.id)
                    if current is not None and current.state == "running":
                        _replace_item_locked(
                            run_id, current.id, state=attempt_state,
                            finished_at=time.time(), error=attempt_error,
                        )
                continue
            if item is None:
                time.sleep(pause)
                continue

            settle(run_id, item, lifecycle.wait())
    finally:
        with reg.record_lock(run_id):
            state = _state(run_id)
            if (state.get("dispatcher_pid") == own_pid
                    and state.get("dispatcher_token") == dispatcher_token):
                _set_dispatcher_locked(run_id, None)


async def wait_for_item(run_id: str, item_id: str) -> QueueItem | None:
    dispatcher_lost_at: float | None = None
    recovery_attempted = False
    while True:
        current = item(run_id, item_id)
        if current is None or current.state in {"replied", "failed", "cancelled", "uncertain"}:
            return current
        try:
            with reg.record_lock(run_id):
                alive = _dispatcher_matches(_state(run_id))
            if alive:
                dispatcher_lost_at = None
                recovery_attempted = False
            else:
                if dispatcher_lost_at is None:
                    dispatcher_lost_at = time.monotonic()
                    recovery_attempted = False
                if not recovery_attempted:
                    recovery_attempted = True
                    if not alive:
                        ensure_dispatcher(run_id)
        except CorruptQueueStateError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            with reg.record_lock(run_id):
                replacement = next(
                    (item for item in _items(_state(run_id)) if item.id == item_id), None,
                )
                if replacement is not None and replacement.state == "pending":
                    return mark_locked(
                        run_id, item_id, "failed",
                        error=f"dispatcher recovery failed: {str(error)[:400]}",
                    )
                if replacement is not None and replacement.state == "running":
                    return mark_locked(
                        run_id, item_id, "uncertain",
                        error="dispatcher recovery failed; resend explicitly",
                    )
                return replacement
        if (
            dispatcher_lost_at is not None
            and time.monotonic() - dispatcher_lost_at >= _DISPATCHER_RECOVERY_TIMEOUT
        ):
            with reg.record_lock(run_id):
                replacement = next(
                    (item for item in _items(_state(run_id)) if item.id == item_id), None,
                )
                if replacement is not None and replacement.state == "pending":
                    return mark_locked(
                        run_id, item_id, "failed",
                        error="dispatcher did not claim delivery; resend explicitly",
                    )
                if replacement is not None and replacement.state == "running":
                    return mark_locked(
                        run_id, item_id, "uncertain",
                        error="dispatcher did not finish delivery; resend explicitly",
                    )
                return replacement
        await asyncio.sleep(_POLL_SECONDS)


def main() -> None:
    if len(sys.argv) in {3, 4} and sys.argv[1] == "--dispatch":
        RuntimeStore.default().register(Runtime.own(), os.getpid())  # a run can outlive an upgrade: its runtime stays until it ends
        dispatch(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--inject":
        if said := inject(sys.argv[2]):
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n\n---\n\n".join(said)}}))
        return
    raise SystemExit("usage: python -m galaius.agents.agent_queue --dispatch RUN_ID | --inject RUN_ID")


if __name__ == "__main__":
    main()
