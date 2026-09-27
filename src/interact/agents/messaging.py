"""Durable message delivery shared by the CLI and MCP agent-send surfaces."""

from __future__ import annotations

import os
import secrets
from contextlib import ExitStack
from typing import Literal

from pydantic import BaseModel, ConfigDict

from interact.agents import registry as reg
from interact.agents.policy import PolicyError
from interact.agents.providers import AgentProvider, _CLIP, provider_for
from interact.agents.run import ModelUnavailable, load_policy, resolve_continuable_model

DeliveryState = Literal["queued", "replied", "error"]


def _validate_message(message: str) -> str | None:
    """Apply the same transcript-sized input boundary to every sending surface."""
    if not isinstance(message, str) or not message.strip():
        return "ERROR: message must contain text."
    if len(message) > _CLIP:
        return f"ERROR: message exceeds the {_CLIP}-character transcript limit."
    return None


class Delivery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: DeliveryState
    text: str
    run_id: str
    queue_id: str | None = None

    @classmethod
    def queued(cls, text: str, run_id: str, *, queue_id: str) -> "Delivery":
        return cls(
            state="queued", text=text, run_id=run_id, queue_id=queue_id,
        )


def check_deliverable(run_id: str, *, sender: str | None = None):
    """Return the owned recipient or a refusal with its safe alternative.

    ``sender`` is the addressing run's own id. An OPERATOR (CLI/panel, no recorded run of their
    own) is never scoped — a human already sees every run it names, the same as `agent_list
    --all-sessions`. An AGENT is scoped to its own conversation the same way `agent_spawn` refuses
    an unrecorded parent (:func:`interact.agents.run.run_agent`): a run belonging to a DIFFERENT
    known session is not this caller's to resume, even though it already holds the id — a leaked
    or guessed id from another project/session must not become a live channel into it. Unknown
    ownership on either side (no session recorded yet) is never treated as a mismatch.
    """
    resolved = reg.resolve_run_id(run_id)
    run = reg.get_run(resolved) if resolved else None
    if run is None and resolved:
        # Only an editor session interact did not start lacks a stored record.
        run = next((r for r in reg.list_runs(include_foreign=True) if r.run_id == resolved), None)
    if run is None:
        return None, f"ERROR: no agent run {run_id!r}. Use agent_list to see the run ids."
    if getattr(run, "foreign", False):
        return None, (f"ERROR: {run.name!r} is one of your own editor sessions, not an agent "
                      "interact started — interact can watch it, but must not type into it.")
    if sender is not None and run.session_id is not None:
        origin = reg.get_run(sender)
        if origin is not None and origin.session_id is not None and origin.session_id != run.session_id:
            return None, (f"ERROR: {run.name!r} belongs to a different session; this run cannot "
                          "message across session boundaries.")
    try:
        provider = provider_for(run.provider)
    except ValueError as error:
        return None, f"ERROR: {error}"
    if not provider.can_resume:
        return None, (f"ERROR: the {run.provider!r} CLI cannot continue a session, so a message "
                      "would arrive with no context. Spawn a new agent with agent_spawn instead.")
    if not run.agent:
        return None, "ERROR: this run has no named agent role; it cannot receive a policy-safe continuation."
    if not _session_id(run):
        return None, (f"ERROR: {run.name!r} has no provider session id yet; wait for its first "
                      "thread-start event before sending a message.")
    return run, None


def _policy_for_continuation(run, provider: AgentProvider):
    """Resolve current policy; dispatcher calls this again immediately before each resume."""
    try:
        policy = load_policy()
        policy, _ = policy.for_launch(run.agent, reference=run.agent_ref)
        if not policy.provider_active(provider.name):
            raise ModelUnavailable(f"Agent provider {provider.name!r} is disabled by policy")
        criterion = policy.criterion_for(run.agent)
        if not criterion:
            raise ModelUnavailable(f"No model criterion for {run.agent!r}; configure it in the agent policy UI")
        if policy.catalog is None and not provider.valid_definition(run.agent):
            raise ModelUnavailable(f"No installed definition for {run.agent!r}")
        try:
            provider.validate_permission_mode(run.permission_mode)
        except ValueError as error:
            raise ModelUnavailable(
                f"Recorded permission intent {run.permission_mode!r} is not accepted by "
                f"{provider.name!r}; refusing continuation"
            ) from error
        model = resolve_continuable_model(criterion, dict(os.environ), provider=provider, weights=policy.weights_for(run.agent))[1]
        return policy, criterion, model, policy.reasoning_for(run.agent)
    except (ModelUnavailable, PolicyError, ValueError) as error:
        raise ModelUnavailable(str(error)) from error


def _session_id(run) -> str | None:
    return run.provider_session_id or (run.run_id if run.provider == "claude" else None)

def deliver_message(run_id: str, message: str, *, sender: str | None = None) -> Delivery:
    """Record and enqueue one message; a detached per-run dispatcher performs the resume."""
    from interact.agents import agent_queue

    if error := _validate_message(message):
        return Delivery(state="error", text=error, run_id=run_id)
    speaker = sender or sender_id()
    run, error = check_deliverable(run_id, sender=speaker)
    if error:
        return Delivery(state="error", text=error, run_id=run_id)
    assert run is not None
    provider = provider_for(run.provider)
    with ExitStack() as locks:
        for locked_run in sorted({run.run_id, speaker}):
            locks.enter_context(reg.record_lock(locked_run))
        run = reg.get_run(run.run_id)
        if run is None:
            return Delivery(state="error", text=f"ERROR: no agent run {run_id!r}.", run_id=run_id)
        try:
            _, criterion, model, reasoning = _policy_for_continuation(run, provider)
        except ModelUnavailable as policy_error:
            return Delivery(state="error", text=f"ERROR: {policy_error}", run_id=run.run_id)
        if _session_id(run) is None:
            return Delivery(
                state="error", text="ERROR: provider session identity is unavailable; not queued.",
                run_id=run.run_id,
            )
        message_id = secrets.token_hex(16)
        origin = reg.get_run(speaker)
        if origin is not None:
            message = origin.handoff_header() + message
        try:
            # The queue intent is durable before either transcript append. A crash after this
            # point leaves a recoverable pending item instead of an invisible message.
            item = agent_queue.enqueue_locked(
                run.run_id, message_id=message_id, sender=speaker,
            )
        except agent_queue.CorruptQueueStateError as queue_error:
            return Delivery(state="error", text=f"ERROR: {queue_error}", run_id=run.run_id)
        if item is None:
            return Delivery(
                state="error", text=f"ERROR: {run.name}'s message queue is full.",
                run_id=run.run_id,
            )
        recorded_id = reg.record_message_event_locked(
            from_run=speaker, to_run=run.run_id, text=message, event_id=message_id,
        )
        if recorded_id is None:
            agent_queue.mark_locked(
                run.run_id, item.id, "failed", error="message transcript entry was not recorded",
            )
            return Delivery(
                state="error", text=f"ERROR: could not record the message to {run_id!r}.",
                run_id=run.run_id,
            )
        if run.status in ("running", "waiting") and run.pid and reg._alive(run.pid):
            if run.model != model or run.requested_criterion != criterion or run.reasoning != reasoning:
                # Informational only. Effective values change when dispatcher starts the turn.
                reg._merge_record_locked(run.run_id, {
                    "pending_model": model,
                    "pending_criterion": criterion,
                    "pending_reasoning": reasoning,
                })
        try:
            agent_queue.ensure_dispatcher_locked(run.run_id, cwd=run.cwd or ".")
        except (OSError, RuntimeError, ValueError) as dispatcher_error:
            return Delivery(
                state="error",
                text=f"ERROR: could not start {run.name}'s dispatcher — {dispatcher_error}",
                run_id=run.run_id,
            )
        return Delivery.queued(
            f"Queued for {run.name} ({run.run_id[:8]}), delivery {item.id[:8]}.",
            run.run_id, queue_id=item.id,
        )


async def wait_for_reply(delivery: Delivery) -> str:
    """Wait for one durable item and return only the reply after its message anchor."""
    if delivery.queue_id is None:
        delivery.state = "error"
        return "ERROR: queued delivery has no persisted queue id."
    from interact.agents import agent_queue
    try:
        item = await agent_queue.wait_for_item(delivery.run_id, delivery.queue_id)
    except agent_queue.CorruptQueueStateError as error:
        delivery.state = "error"
        return f"ERROR: {error}"
    if item is None:
        delivery.state = "error"
        return "ERROR: queued delivery disappeared."
    if item.state == "cancelled":
        delivery.state = "error"
        return "ERROR: queued delivery was cancelled."
    if item.state == "failed":
        delivery.state = "error"
        return f"ERROR: queued delivery failed. {item.error}".strip()
    if item.state == "uncertain":
        delivery.state = "error"
        return f"ERROR: queued delivery is uncertain. {item.error}".strip()
    anchor = item.raw_index
    if anchor is None:
        delivery.state = "error"
        return "ERROR: queued delivery has no persisted attempt anchor; resend explicitly."
    events = reg.read_events(delivery.run_id)
    replies = [
        event.text for event in events
        if (event.kind == "text" or (event.kind == "done" and event.final_text))
        and event.text.strip()
        and event.raw_index is not None and event.raw_index >= anchor
    ]
    if not replies:
        delivery.state = "error"
        return "ERROR: resumed agent exited without a reply event."
    delivery.state = "replied"
    origin = reg.get_run(delivery.run_id)
    header = origin.handoff_header() if origin is not None else ""
    return f"{delivery.text}\n{header}{replies[-1]}"


def sender_id() -> str:
    return os.environ.get("INTERACT_RUN_ID") or "operator"
