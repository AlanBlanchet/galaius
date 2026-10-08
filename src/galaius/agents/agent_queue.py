"""Durable, one-writer continuation queue for one agent run.

The vendor ``queue`` command is not a delivery boundary: it can acknowledge a message and then
lose it when the short-lived CLI process exits. This queue stores only delivery metadata; message
text remains in the recipient transcript. A detached dispatcher owns the provider process until
each queued turn finishes.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from galaius.agents import registry as reg
from galaius.agents.providers import _safe_process_detail
from galaius.processes import process_started
from galaius.upgrade.store import Runtime, RuntimeStore, active_interpreter

MAX_PENDING = 128
_POLL_SECONDS = 0.1
_DISPATCHER_RECOVERY_TIMEOUT = 30.0
_DETACHED = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CorruptQueueStateError(RuntimeError):
    """The durable queue cannot be trusted and was left untouched."""


QueueState = Literal["pending", "running", "replied", "failed", "cancelled", "uncertain"]
AttemptState = Literal["replied", "failed", "uncertain"]


class QueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    message_id: str = Field(min_length=1, max_length=160)
    sender: str = Field(min_length=1, max_length=160)
    state: QueueState = "pending"
    enqueued_at: float
    started_at: float | None = None
    finished_at: float | None = None
    raw_index: int | None = None
    attempt_token: str | None = Field(default=None, min_length=1, max_length=80)
    error: str = ""


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


def enqueue_locked(run_id: str, *, message_id: str, sender: str) -> QueueItem | None:
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
        enqueued_at=time.time(),
    )
    state["version"] = 1
    state["items"] = [*state.get("items", []), item.model_dump()]
    _write_state(run_id, state)
    return item


def enqueue(run_id: str, *, message_id: str, sender: str) -> QueueItem | None:
    with reg.record_lock(run_id):
        return enqueue_locked(run_id, message_id=message_id, sender=sender)


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


def items(run_id: str) -> list[QueueItem]:
    with reg.record_lock(run_id):
        return _items(_state(run_id))


def _set_dispatcher_locked(
    run_id: str, pid: int | None, token: str | None = None, started: int | None = None,
) -> None:
    state = _state(run_id)
    state["dispatcher_pid"] = pid
    state["dispatcher_token"] = token
    state["dispatcher_started"] = started
    _write_state(run_id, state)


def _dispatcher_matches(state: dict) -> bool:
    """Whether the dispatcher `state` records is still that very process: alive, and started when
    it was recorded to (a bare pid may since name an unrelated process). Where the system keeps no
    start time (`process_started` is None, macOS) no dispatcher is ever trusted: one is started."""
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
        process = subprocess.Popen(
            # The ACTIVE runtime's interpreter, never this process's: a dispatcher deliberately keeps
            # its runtime until its run ends (see `main`), so one started by a process that still runs
            # an older build would keep that build alive for hours after an upgrade.
            [active_interpreter(), "-m", "galaius.agents.agent_queue", "--dispatch", run_id, token],
            cwd=cwd if cwd and Path(cwd).is_dir() else ".",
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
            env=None if environment is None else dict(environment),
            # Windows: outlives the console that asked for it (its own group, a hidden console
            # its provider children share instead of each opening a window). 0 on POSIX.
            creationflags=_DETACHED,
        )
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


def _active(run: reg.AgentRun) -> bool:
    return run.status in ("running", "waiting") and run.process_running()


def _fresh_policy(run: reg.AgentRun):
    # Imported only in the detached child. The parent imports messaging -> this module.
    from galaius.agents.messaging import _policy_for_continuation, provider_for

    provider = provider_for(run.provider)
    _, criterion, model, reasoning = _policy_for_continuation(run, provider)
    return provider, criterion, model, reasoning


def _classify_attempt(run_id: str, raw_index: int | None) -> tuple[AttemptState, str]:
    """Classify one persisted provider attempt without treating exit as acceptance."""
    if raw_index is None:
        return "uncertain", "resumed turn has no persisted attempt anchor; resend explicitly"
    events = reg.read_events(run_id)
    attempt_events = [
        event for event in events
        if event.raw_index is not None and event.raw_index >= raw_index
    ]
    replies = [
        event for event in attempt_events
        if (event.kind == "text" or (event.kind == "done" and event.final_text))
        and event.text.strip()
    ]
    terminal = next(
        (event for event in reversed(attempt_events) if event.kind in {"done", "error"}),
        None,
    )
    if terminal is not None and terminal.kind == "done" and replies:
        return "replied", ""
    if terminal is not None:
        return "failed", "resumed turn completed without a reply; resend explicitly"
    return "uncertain", "resumed turn acceptance was not confirmed; resend explicitly"


def dispatch(run_id: str, dispatcher_token: str | None = None) -> None:
    """Drain one run serially; safe to call again after a dispatcher crash."""
    own_pid = os.getpid()
    try:
        while True:
            recover: tuple[QueueItem, reg.AgentRun] | None = None
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
                if _active(run):
                    item = None
                elif running is not None:
                    # The previous dispatcher may have died after the provider child finished but
                    # before it could persist the item's terminal state. Inspect the transcript
                    # before replaying the message.
                    recover = (running, run)
                    item = None
                else:
                    item = claim_next_locked(run_id)
                    if item is not None:
                        message = reg.message_for(run_id, item.message_id)
                        if message is None:
                            _replace_item_locked(
                                run_id, item.id, state="failed", finished_at=time.time(),
                                error="message transcript entry is missing",
                            )
                            item = None
                        else:
                            try:
                                provider, criterion, model, reasoning = _fresh_policy(run)
                                from galaius.agents.run import launch_continuation
                                lifecycle = launch_continuation(
                                    provider, run, run.provider_session_id or run.run_id,
                                    message.text, model=model, criterion=criterion,
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
                attempt_state, attempt_error = _classify_attempt(
                    run_id, recovered_item.raw_index,
                )
                with reg.record_lock(run_id):
                    current = next((i for i in _items(_state(run_id))
                                    if i.id == recovered_item.id), None)
                    if current is not None and current.state == "running":
                        _replace_item_locked(
                            run_id, current.id, state=attempt_state,
                            finished_at=time.time(), error=attempt_error,
                        )
                continue
            if item is None:
                time.sleep(_POLL_SECONDS)
                continue

            code = lifecycle.wait()
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
                    current = next((i for i in _items(_state(run_id)) if i.id == item.id), None)
                    if current is not None and current.state == "running":
                        _replace_item_locked(
                            run_id, item.id, state="failed", finished_at=time.time(), error=error,
                        )
                    if not stopped and (said is None or said.kind != "error" or not said.text.strip()):
                        reg.fail_turn_locked(run_id, error, exit_code=code)
            else:
                attempt_state, attempt_error = _classify_attempt(run_id, item.raw_index)
                with reg.record_lock(run_id):
                    current = next((i for i in _items(_state(run_id)) if i.id == item.id), None)
                    if current is not None and current.state == "running":
                        _replace_item_locked(
                            run_id, item.id, state=attempt_state, finished_at=time.time(),
                            error=attempt_error,
                        )
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
        current = next((item for item in items(run_id) if item.id == item_id), None)
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
    raise SystemExit("usage: python -m galaius.agents.agent_queue --dispatch RUN_ID")


if __name__ == "__main__":
    main()
