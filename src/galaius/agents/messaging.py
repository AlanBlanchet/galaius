"""Durable message delivery shared by the CLI and MCP agent-send surfaces."""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Mapping
from contextlib import ExitStack
from typing import Literal

from pydantic import BaseModel, ConfigDict

from galaius_core import ReadsAt

from galaius.agents import agent_queue
from galaius.agents import registry as reg
from galaius.agents.policy import PolicyError
from galaius.agents.providers import AgentProvider, _CLIP, provider_for
from galaius.agents.run import ModelUnavailable, load_policy, resolve_continuable_model

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
    #: `queued`: when the run's agent reads it (`galaius_core.ReadsAt`); None when not queued.
    reads_at: ReadsAt | None = None
    #: `queued`: the message was queued by an earlier call under the same message id, not by this one.
    repeated: bool = False

    @classmethod
    def queued(cls, run: reg.AgentRun, item: agent_queue.QueueItem, *, repeated: bool = False) -> "Delivery":
        """`item`, queued for `run` (its lock held), and when its agent reads it: `now` a turn starts on
        it (or, `repeated`, it has read it already), `next_step` the turn running (or the one starting
        on a message ahead of it) reads it after its current tool call, `turn_end` once that turn ends:
        its CLI reads nothing mid-turn, or the run is fenced (the hook cannot claim the message from
        inside the fence)."""
        waiting = [other.id for other in agent_queue.items_locked(run.run_id) if other.state in {"pending", "running"}]
        working = item.state == "pending" and ((item.id in waiting and waiting.index(item.id) > 0) or (run.status in ("running", "waiting") and run.process_running()))
        reads_at: ReadsAt = "now" if not working else "next_step" if run.fence is None and provider_for(run.provider).reads_mid_turn else "turn_end"
        return cls(state="queued", text=f"Queued for {run.name} ({run.run_id[:8]}), delivery {item.id[:8]}.", run_id=run.run_id,
                   queue_id=item.id, reads_at=reads_at, repeated=repeated)

    @classmethod
    def repeat(cls, run_id: str, message_id: str) -> "Delivery | None":
        """The message already queued for `run_id` under `message_id` (its lock held), told as when it
        was queued, or why it was not delivered; None when there is none."""
        run = reg.get_run(run_id)
        item = next((item for item in agent_queue.items_locked(run_id) if item.message_id == message_id), None) if run is not None else None
        if run is None or item is None:
            return None
        if item.state in {"failed", "cancelled"}:
            return cls(state="error", text=f"ERROR: {item.error or f'the message was {item.state}'}", run_id=run_id, queue_id=item.id, repeated=True)
        return cls.queued(run, item, repeated=True)


def check_deliverable(run_id: str, *, sender: str | None = None):
    """Return the owned recipient or a refusal with its safe alternative.

    ``sender`` is the addressing run's own id. An OPERATOR (CLI/panel, no recorded run of their
    own) is never scoped — a human already sees every run it names, the same as `agent_list
    --all-sessions`. An AGENT is scoped to its own conversation the same way `agent_spawn` refuses
    an unrecorded parent (:func:`galaius.agents.run.run_agent`): a run belonging to a DIFFERENT
    known session is not this caller's to resume, even though it already holds the id — a leaked
    or guessed id from another project/session must not become a live channel into it. Unknown
    ownership on either side (no session recorded yet) is never treated as a mismatch.
    """
    resolved = reg.resolve_run_id(run_id)
    run = reg.get_run(resolved) if resolved else None
    if run is None and resolved:
        # Only an editor session galaius did not start lacks a stored record.
        run = next((r for r in reg.list_runs(include_foreign=True) if r.run_id == resolved), None)
    if run is None:
        return None, f"ERROR: no agent run {run_id!r}. Use agent_list to see the run ids."
    if getattr(run, "foreign", False):
        return None, (f"ERROR: {run.name!r} is one of your own editor sessions, not an agent "
                      "galaius started — galaius can watch it, but must not type into it.")
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


def policy_for_continuation(run, provider: AgentProvider, environment: Mapping[str, str] | None = None):
    """Resolve current policy; dispatcher calls this again immediately before each resume.
    `environment`: the one the resumed turn runs in (credentials decide what can run), this
    process's own when None."""
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
        model = resolve_continuable_model(criterion, dict(os.environ if environment is None else environment), provider=provider, weights=policy.weights_for(run.agent), role=run.agent)[1]
        return policy, criterion, model, policy.reasoning_for(run.agent)
    except (ModelUnavailable, PolicyError, ValueError) as error:
        raise ModelUnavailable(str(error)) from error


def _session_id(run) -> str | None:
    return run.provider_session_id or (run.run_id if run.provider == "claude" else None)

def deliver_message(
    run_id: str, message: str, *, sender: str | None = None, environment: Mapping[str, str] | None = None, message_id: str | None = None,
) -> Delivery:
    """Record and enqueue one message (`queue_message`); a detached per-run dispatcher performs the resume.

    `environment` is the one the dispatcher and its resumed turn run in, and names the sender
    when `sender` does not (`GALAIUS_RUN_ID`); this process's own when None."""
    delivery = queue_message(run_id, message, sender=sender, environment=environment, message_id=message_id)
    if delivery.state != "queued":
        return delivery
    refused = start_dispatcher(delivery.run_id, os.environ if environment is None else environment)
    return delivery if refused is None else Delivery(state="error", text=f"ERROR: {refused}", run_id=delivery.run_id)


def start_dispatcher(run_id: str, environment: Mapping[str, str]) -> str | None:
    """Make sure run `run_id`'s dispatcher runs (in `environment`; one already running is kept):
    it delivers what is queued for the run. Why it could not start, None when it runs."""
    run = None
    try:
        with reg.record_lock(run_id):
            run = reg.get_run(run_id)
            agent_queue.ensure_dispatcher_locked(run_id, cwd=(run.cwd if run is not None else None) or ".", environment=dict(environment))
    except (OSError, RuntimeError, ValueError) as error:
        return f"could not start {run.name if run is not None else run_id}'s dispatcher — {error}"
    return None


def queue_message(
    run_id: str, message: str, *, sender: str | None = None, environment: Mapping[str, str] | None = None, message_id: str | None = None,
) -> Delivery:
    """Record one message and enqueue its delivery (`queued`, with its queue id), or say why not;
    whoever called delivers it: the run's dispatcher (`deliver_message`), or a long-lived launcher
    holding the run's next child (`galaius.agents.followup`). `environment` as `deliver_message`'s.
    `message_id`: the id its sender gave it (a request id from the web), kept by its queue item and
    its transcript entry; a message already queued under it is never queued again (`repeated`)."""
    environment = dict(os.environ if environment is None else environment)
    if message_id is not None:
        with reg.record_lock(recipient := reg.resolve_run_id(run_id) or run_id):
            if (repeat := Delivery.repeat(recipient, message_id)) is not None:
                return repeat
    if error := _validate_message(message):
        return Delivery(state="error", text=error, run_id=run_id)
    speaker = sender or sender_id(environment)
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
            _, criterion, model, reasoning = policy_for_continuation(run, provider, environment)
        except ModelUnavailable as policy_error:
            return Delivery(state="error", text=f"ERROR: {policy_error}", run_id=run.run_id)
        if _session_id(run) is None:
            return Delivery(
                state="error", text="ERROR: provider session identity is unavailable; not queued.",
                run_id=run.run_id,
            )
        if message_id is not None and (repeat := Delivery.repeat(run.run_id, message_id)) is not None:
            return repeat  # queued meanwhile, by another process
        message_id = message_id or secrets.token_hex(16)
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
        if run.status in ("running", "waiting") and run.process_running():
            if run.model != model or run.requested_criterion != criterion or run.reasoning != reasoning:
                # Informational only. Effective values change when dispatcher starts the turn.
                reg._merge_record_locked(run.run_id, {
                    "pending_model": model,
                    "pending_criterion": criterion,
                    "pending_reasoning": reasoning,
                })
        return Delivery.queued(run, item)


def wait_for_start(delivery: Delivery, *, timeout: float = 10.0, interval: float = 0.2, grace: float = 1.5) -> str | None:
    """Give the detached dispatcher `timeout` seconds to START the resumed turn; the reason when it
    could not, None otherwise.

    The reason is the queue item's own error (policy, catalog, a provider that would not spawn,
    a child that died at once). None means the turn's provider child is alive (its reply follows
    on its own), the item already replied, or the deadline passed with the item still unclaimed —
    the ask stays queued and the run's record tells the rest. What lets `galaius agents send`
    exit 1 within seconds of a refused resume instead of printing "Queued". A child counts as
    started only once it has lived `grace` seconds: one that dies on arrival (a CLI refusing, a
    missing session) is caught here, not reported as queued.
    """
    if delivery.state != "queued" or delivery.queue_id is None:
        return None
    deadline = time.monotonic() + timeout
    alive_since: float | None = None
    while True:
        item = next((i for i in agent_queue.items(delivery.run_id) if i.id == delivery.queue_id), None)
        if item is None:
            return "queued delivery disappeared"
        if item.state in ("failed", "cancelled", "uncertain"):
            return item.error or f"queued delivery {item.state}"
        if item.state == "replied":
            return None
        run = reg.get_run(delivery.run_id)
        if item.state == "running" and run is not None and run.process_running():
            alive_since = alive_since if alive_since is not None else time.monotonic()
            if time.monotonic() - alive_since >= grace:
                return None
        else:
            alive_since = None
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


async def wait_for_reply(delivery: Delivery) -> str:
    """Wait for one durable item and return only the reply after its message anchor."""
    if delivery.queue_id is None:
        delivery.state = "error"
        return "ERROR: queued delivery has no persisted queue id."
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


def sender_id(environment: Mapping[str, str] | None = None) -> str:
    return (os.environ if environment is None else environment).get("GALAIUS_RUN_ID") or "operator"
