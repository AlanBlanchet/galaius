"""Private Codex app-server transport and exact frame validation."""

import asyncio
import hashlib
import json
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Literal, NotRequired, Required

from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PrivateAttr,
    TypeAdapter,
    ValidationError,
)
from typing_extensions import TypedDict

from interact import installed_version
from interact.agents.codex_schema import (
    REJECTED_REQUEST_POLICIES,
    encode_supported_response,
    reject_unsupported,
    validate_request,
)
from interact.agents.events import (
    TOKEN_FIELDS,
    AgentEvent,
    TokenUsage,
    ConversationInteraction,
    InteractionField,
    InteractionKind,
)
from interact.agents.ceiling import contained
from interact.agents.providers import CodexProvider
from interact.agents.registry import AgentRun
from interact.agents.transport import (
    _ConversationStart,
    _ConversationTransport,
    _TransportUpdate,
    _TurnStart,
)

_PROVIDER_LIMIT = 1024 * 1024
_RPC_TIMEOUT = 10.0
_ENTITY_ID = re.compile(r"^[A-Za-z0-9._:@+-]{1,160}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:/@+-]{1,256}$")
_KNOWN_METHODS = frozenset({
    "turn/started",
    "item/agentMessage/delta",
    "item/started",
    "item/completed",
    "thread/tokenUsage/updated",
    "turn/completed",
})


def _provider_error_text(value: JsonValue, *, fallback: str) -> str:
    if isinstance(value, dict) and isinstance(value.get("message"), str):
        detail = value["message"]
    elif isinstance(value, str):
        detail = value
    else:
        detail = json.dumps(value, ensure_ascii=False)
    detail = " ".join(detail.split())[:400]
    return f"{fallback}: {detail or 'no details'}"


class _Account(TypedDict):
    type: str


class _AccountResult(TypedDict):
    account: _Account | None
    requiresOpenaiAuth: bool


class _ModelRow(TypedDict, total=False):
    model: str
    id: str
    hidden: bool
    isDefault: bool


class _ModelPage(TypedDict):
    data: list[_ModelRow]
    nextCursor: str | None


class _Thread(TypedDict):
    id: str
    sessionId: str


class _ThreadResult(TypedDict, total=False):
    thread: Required[_Thread]
    model: NotRequired[str]


class _Turn(TypedDict):
    id: str


class _TurnResult(TypedDict):
    turn: _Turn


_ACCOUNT = TypeAdapter(_AccountResult)
_MODEL_PAGE = TypeAdapter(_ModelPage)
_THREAD_RESULT = TypeAdapter(_ThreadResult)
_TURN_RESULT = TypeAdapter(_TurnResult)
_PARAMS = TypeAdapter(dict[str, JsonValue])


class _RpcMessage(BaseModel):
    """One app-server frame; vendor envelope extensions stay opaque.

    Codex emits top-level metadata such as ``emittedAtMs`` on unsolicited notifications. The
    method-specific payloads remain validated at their consumers; rejecting an envelope extension
    here would kill the whole connection before the requested response can arrive.
    """

    model_config = ConfigDict(extra="ignore")

    id: int | str | None = None
    method: str | None = Field(default=None, max_length=160)
    params: JsonValue | None = None
    result: JsonValue | None = None
    error: JsonValue | None = None

    @property
    def provider_request(self) -> bool:
        return self.method is not None and self.id is not None

    @property
    def notification(self) -> bool:
        return self.method is not None and self.id is None

    @property
    def response(self) -> bool:
        return self.method is None and self.id is not None


class _CodexTransport(_ConversationTransport):
    """One supervised app-server subprocess and its correlation-owned RPC state."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: CodexProvider
    workspace_root: Path
    _process: asyncio.subprocess.Process | None = PrivateAttr(default=None)
    _pending: dict[int, asyncio.Future[JsonValue]] = PrivateAttr(default_factory=dict)
    _events: asyncio.Queue[_RpcMessage | None] = PrivateAttr(default_factory=asyncio.Queue)
    _reader: asyncio.Task[None] | None = PrivateAttr(default=None)
    _stderr_reader: asyncio.Task[None] | None = PrivateAttr(default=None)
    _next_id: int = PrivateAttr(default=1)
    _next_event_id: int = PrivateAttr(default=1)
    _failed: bool = PrivateAttr(default=False)
    _failure_reason: str | None = PrivateAttr(default=None)
    _closing: bool = PrivateAttr(default=False)
    _interactions: dict[
        str, tuple[int | str, str, ConversationInteraction, str]
    ] = PrivateAttr(
        default_factory=dict
    )
    _usage_totals: dict[str, TokenUsage] = PrivateAttr(default_factory=dict)
    _delta_items: set[tuple[str, str]] = PrivateAttr(default_factory=set)
    _orphans: dict[str, list[_RpcMessage]] = PrivateAttr(default_factory=dict)
    _runs: dict[str, AgentRun] = PrivateAttr(default_factory=dict)
    _turn_runs: dict[str, str] = PrivateAttr(default_factory=dict)
    _active_turns: dict[str, str] = PrivateAttr(default_factory=dict)
    _terminal_children: set[str] = PrivateAttr(default_factory=set)
    _ready: deque[_TransportUpdate] = PrivateAttr(default_factory=deque)
    _updates_ended: bool = PrivateAttr(default=False)

    @classmethod
    async def open(cls, provider: CodexProvider, workspace_root: Path):
        connection = cls(provider=provider, workspace_root=workspace_root)
        await connection._spawn()
        return connection

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    async def initialize(self) -> bool:
        await self.request("initialize", {
            "clientInfo": {
                "name": "interact",
                "title": "Interact",
                "version": installed_version(),
            },
            "capabilities": {"experimentalApi": False},
        })
        await self.notify("initialized", {})
        account = _ACCOUNT.validate_python(await self.request("account/read", {
            "refreshToken": False,
        }))
        # Signed in = a ChatGPT account is present. `requiresOpenaiAuth` says the configured model
        # provider needs OpenAI auth (true for the default provider even when signed in, codex
        # 0.155.1): reading it as "not signed in" made every signed-in owner's session route
        # "unauthenticated".
        return bool(account["account"] is not None and account["account"]["type"] == "chatgpt")

    async def models(self) -> tuple[list[str], str | None]:
        cursor: str | None = None
        model_ids: list[str] = []
        default: str | None = None
        for _ in range(10):
            page = _MODEL_PAGE.validate_python(await self.request("model/list", {
                "cursor": cursor,
                "includeHidden": False,
                "limit": 100,
            }))
            for row in page["data"]:
                model_id = row.get("model") or row.get("id")
                if model_id is None or row.get("hidden", False):
                    continue
                model_ids.append(self.model_identifier(model_id))
                if row.get("isDefault", False):
                    default = model_id
                if len(model_ids) > 2_000:
                    raise ConnectionError("provider model catalog exceeded its model limit")
            cursor = page["nextCursor"]
            if cursor is None:
                break
        else:
            raise ConnectionError("provider model catalog exceeded its page limit")
        return list(dict.fromkeys(model_ids)), default

    async def start_conversation(
        self, *, model: str, workspace: Path, instruction: str | None
    ) -> _ConversationStart:
        parameters: dict[str, object] = {
            "model": model,
            "cwd": str(workspace),
            # No `runtimeWorkspaceRoots`: codex 0.155.1 refuses it unless the client declares the
            # experimental API, which this client never does; `cwd` scopes the read-only session.
            "sandbox": "read-only",
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "allowProviderModelFallback": False,
        }
        if instruction is not None:
            parameters["developerInstructions"] = instruction
        result = _THREAD_RESULT.validate_python(
            await self.request("thread/start", parameters)
        )
        conversation_id = self.identifier(result["thread"]["id"])
        if self.identifier(result["thread"]["sessionId"]) != conversation_id:
            raise ValueError("provider returned a non-root session")
        returned_model = result.get("model", model)
        if returned_model != model:
            raise ValueError("provider changed model")
        return _ConversationStart(conversation_id=conversation_id, model=returned_model)

    async def list_threads(self, *, limit: int = 50) -> list[dict[str, JsonValue]]:
        """The Codex conversations recorded on this computer, newest first (`thread/list`): each its
        id, preview, folder, originator (who made it) and last update."""
        result = await self.request("thread/list", {"limit": limit})
        rows = result.get("data") if isinstance(result, dict) else None
        keep = ("id", "preview", "cwd", "originator", "updatedAt", "forkedFromId")
        return [{key: row.get(key) for key in keep} for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)] if isinstance(rows, list) else []

    async def fork_conversation(self, *, thread_id: str, model: str, workspace: Path) -> _ConversationStart:
        """A COPY of conversation `thread_id` (`thread/fork`) that continues in `workspace` under
        the same read-only sandbox and ask-first approvals as a conversation started here; the
        original is never written."""
        result = _THREAD_RESULT.validate_python(await self.request("thread/fork", {
            "threadId": self.identifier(thread_id),
            "model": model,
            "cwd": str(workspace),
            "sandbox": "read-only",
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
        }))
        conversation_id = self.identifier(result["thread"]["id"])
        if conversation_id == thread_id:
            raise ValueError("provider continued the original instead of a copy")
        return _ConversationStart(conversation_id=conversation_id, model=result.get("model", model))

    async def resume_conversation(
        self, *, conversation_id: str, model: str, workspace: Path
    ) -> _ConversationStart:
        result = _THREAD_RESULT.validate_python(await self.request("thread/resume", {
            "threadId": conversation_id,
            "model": model,
            "cwd": str(workspace),
            "sandbox": "read-only",
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
        }))
        thread_id = self.identifier(result["thread"]["id"])
        session_id = self.identifier(result["thread"]["sessionId"])
        returned_model = result.get("model", model)
        if thread_id != conversation_id or session_id != conversation_id:
            raise ValueError("provider changed conversation")
        if returned_model != model:
            raise ValueError("provider changed model")
        return _ConversationStart(conversation_id=conversation_id, model=returned_model)

    async def start_turn(self, *, conversation_id: str, prompt: str) -> _TurnStart:
        result = _TURN_RESULT.validate_python(await self.request("turn/start", {
            "threadId": conversation_id,
            "input": [{"type": "text", "text": prompt}],
        }))
        turn_id = self.identifier(result["turn"]["id"])
        run = self._runs.get(conversation_id)
        if run is None:
            raise ValueError("conversation is not bound to the transport")
        self._remember_turn(run.run_id, turn_id)
        return _TurnStart(turn_id=turn_id)

    async def cancel_turn(self, *, conversation_id: str, turn_id: str) -> None:
        await self.request("turn/interrupt", {
            "threadId": conversation_id,
            "turnId": turn_id,
        })
        self.clear_transient(conversation_id)

    async def submit_interaction(
        self, *, run_id: str, interaction_id: str, values: dict[str, str | bool]
    ) -> None:
        pending = self._interactions.get(interaction_id)
        if pending is None or pending[3] != run_id:
            raise ValueError("interaction does not exist")
        request_id, method, interaction, _ = pending
        validated = interaction.validate_submission(values)
        result = encode_supported_response(
            {"id": request_id, "method": method, "params": {}}, validated
        )
        await self.respond(request_id, result)
        self._interactions.pop(interaction_id, None)

    def _register_interaction(
        self, *, interaction_id: str, request_id: int | str,
        method: str, interaction: ConversationInteraction, run_id: str,
    ) -> None:
        self._interactions[interaction_id] = (request_id, method, interaction, run_id)

    def has_interaction(self, *, run_id: str, interaction_id: str) -> bool:
        pending = self._interactions.get(interaction_id)
        return pending is not None and pending[3] == run_id

    def bind_conversation(self, run: AgentRun) -> None:
        conversation_id = run.provider_session_id or run.run_id
        conversation_id = self.identifier(conversation_id)
        self._runs[conversation_id] = run.model_copy(deep=True)
        self._usage_totals[conversation_id] = TokenUsage(
            **{field: getattr(run, field) or 0 for field in TOKEN_FIELDS}
        )

    def has_conversation(self, conversation_id: str) -> bool:
        return self.identifier(conversation_id) in self._runs

    def _buffer_orphan(self, conversation_id: str, message: _RpcMessage) -> None:
        pending = self._orphans.get(conversation_id)
        if pending is not None:
            if len(pending) < 100:
                pending.append(message)
        elif len(self._orphans) < 10:
            self._orphans[conversation_id] = [message]

    def _take_orphans(self, conversation_id: str) -> tuple[_RpcMessage, ...]:
        return tuple(self._orphans.pop(conversation_id, ()))

    async def next_update(self) -> _TransportUpdate | None:
        while not self._ready:
            if self._updates_ended:
                return None
            message = await self._events.get()
            if message is None:
                self._ready.extend(self._provider_failure_updates())
                self._updates_ended = True
                if not self._ready:
                    return None
                break
            try:
                self._ready.extend(self._normalize_message(message))
            except (TypeError, ValueError, ValidationError):
                await self._fail()
                self._ready.extend(self._provider_failure_updates())
                self._updates_ended = True
                if not self._ready:
                    return None
                break
        return self._ready.popleft()

    def normalize_collaboration(
        self,
        *,
        parent: AgentRun,
        params: dict[str, JsonValue],
        item: dict[str, JsonValue],
        turn_id: str,
        item_id: str,
        stage: Literal["started", "completed"],
    ) -> tuple[_TransportUpdate, ...]:
        receivers = item.get("receiverThreadIds")
        if not isinstance(receivers, list):
            raise TypeError("collaboration item is malformed")
        task = item.get("prompt")
        model = item.get("model")
        timestamp = params.get(
            "startedAtMs" if stage == "started" else "completedAtMs"
        )
        observed = timestamp / 1000 if isinstance(timestamp, (int, float)) else None
        updates: list[_TransportUpdate] = []
        for raw_child in receivers:
            if not isinstance(raw_child, str):
                raise TypeError("collaboration child identifier is malformed")
            child_id = self.identifier(raw_child)
            event_id = f"{turn_id}:{item_id}:{child_id}:{stage}"
            child = AgentRun(
                run_id=child_id,
                kind="provider_child",
                provider="codex",
                name="subagent",
                task=task if isinstance(task, str) else "",
                cwd=parent.cwd,
                project=parent.project,
                model=model if isinstance(model, str) else None,
                parent_run_id=parent.run_id,
                root_run_id=parent.root_run_id or parent.run_id,
                spawned_by_event_id=event_id,
                provider_session_id=child_id,
                connection="local_session",
                requested_model=model if isinstance(model, str) else None,
                charge_path="unknown",
                cost_certainty="unknown",
                capabilities=["collaboration"],
                started_at=(observed or time.time()) if stage == "started" else time.time(),
                finished_at=observed if stage == "completed" else None,
                status="running" if stage == "started" else "done",
            )
            self._runs[child_id] = child
            if stage == "completed":
                self._terminal_children.add(child_id)
            updates.append(_TransportUpdate(
                conversation_id=parent.run_id,
                child=child,
                event=AgentEvent(
                    kind="spawn",
                    event_id=event_id,
                    provider_cursor=item_id,
                    turn_id=turn_id,
                    agent_run_id=child_id,
                    parent_event_id=f"{turn_id}:{item_id}",
                    text=child.task,
                    status="running" if stage == "started" else "completed",
                ),
            ))
        return tuple(updates)

    def normalize_delta(
        self, *, run_id: str, thread_id: str, params: dict[str, JsonValue], turn_id: str | None
    ) -> AgentEvent:
        item_id = params.get("itemId")
        delta = params.get("delta")
        if turn_id is None or not isinstance(item_id, str) or not isinstance(delta, str):
            raise ValueError("agent message delta is malformed")
        item_id = self.identifier(item_id)
        self._delta_items.add((thread_id, item_id))
        event_sequence = self._next_event_id
        self._next_event_id += 1
        return AgentEvent(
            kind="text", event_id=f"{turn_id}:{item_id}:delta:{event_sequence}",
            provider_cursor=item_id, turn_id=turn_id, agent_run_id=run_id,
            text=delta[:32_768], status="running",
        )

    def consume_delta_marker(self, thread_id: str, item_id: str, *, completed: bool) -> bool:
        key = (thread_id, item_id)
        found = key in self._delta_items
        if completed:
            self._delta_items.discard(key)
        return found

    def clear_transient(self, conversation_id: str) -> None:
        self._delta_items = {
            item for item in self._delta_items if item[0] != conversation_id
        }

    def normalize_usage(
        self, *, run_id: str, thread_id: str, params: dict[str, JsonValue], turn_id: str | None
    ) -> AgentEvent:
        token_usage = params.get("tokenUsage")
        total = token_usage.get("total") if isinstance(token_usage, dict) else None
        if turn_id is None or not isinstance(total, dict):
            raise ValueError("token usage notification is malformed")
        # `TokenUsageBreakdown` (codex app-server schema): the thread's cumulative counts in
        # OpenAI's meaning — cached and cache-write tokens inside `inputTokens`, reasoning inside
        # `outputTokens`. `cacheWriteInputTokens` is the one optional field.
        counts = {
            "input_tokens": total.get("inputTokens"),
            "output_tokens": total.get("outputTokens"),
            "cached_input_tokens": total.get("cachedInputTokens", 0),
            "cache_write_input_tokens": total.get("cacheWriteInputTokens", 0),
        }
        if not all(isinstance(value, int) and not isinstance(value, bool)
                   for value in counts.values()):
            raise TypeError("token usage notification is malformed")
        current = TokenUsage(**counts)
        delta = current.since(self._usage_totals.get(thread_id, TokenUsage()))
        self._usage_totals[thread_id] = current
        return AgentEvent(
            kind="other",
            event_id=f"{turn_id}:usage:{current.input_tokens}:{current.output_tokens}",
            turn_id=turn_id, agent_run_id=run_id,
            **delta.model_dump(),
            raw_type="thread/tokenUsage/updated", status="running",
        )

    def normalize_completion(
        self, *, run_id: str, params: dict[str, JsonValue]
    ) -> AgentEvent:
        turn = params.get("turn")
        if not isinstance(turn, dict):
            raise TypeError("turn completion notification is malformed")
        turn_id = turn.get("id")
        status = turn.get("status")
        if not isinstance(turn_id, str) or not isinstance(status, str):
            raise TypeError("turn completion notification is malformed")
        turn_id = self.identifier(turn_id)
        if status == "interrupted":
            return AgentEvent(kind="cancelled", event_id=f"{turn_id}:cancelled",
                              turn_id=turn_id, agent_run_id=run_id,
                              text="Turn cancelled.", status="cancelled")
        if status == "completed":
            return AgentEvent(kind="done", event_id=f"{turn_id}:done", turn_id=turn_id,
                              agent_run_id=run_id, status="completed")
        failure = turn.get("error")
        if isinstance(failure, dict):
            message = failure.get("message")
            text = message if isinstance(message, str) else json.dumps(failure, ensure_ascii=False)
        elif isinstance(failure, str):
            text = failure
        else:
            text = "Codex turn failed."
        return AgentEvent(kind="error", event_id=f"{turn_id}:error", turn_id=turn_id,
                          agent_run_id=run_id, text=text[:2000],
                          status="provider_failed")

    def _normalize_message(self, message: _RpcMessage) -> tuple[_TransportUpdate, ...]:
        params = _PARAMS.validate_python(message.params or {})
        method = message.method or ""
        thread_id = params.get("threadId")
        if message.provider_request and not isinstance(thread_id, str):
            raw_turn_id = params.get("turnId")
            turn_id = self.identifier(raw_turn_id) if isinstance(raw_turn_id, str) else None
            run_id = self._turn_runs.get(turn_id or "")
            if run_id is None:
                raise ValueError("provider interaction has no active turn")
            return (self._normalize_interaction(run_id, message, params, turn_id),)
        if not isinstance(thread_id, str):
            if method in _KNOWN_METHODS or message.provider_request:
                raise ValueError("known provider frame has no thread identifier")
            return ()
        thread_id = self.identifier(thread_id)
        run = self._runs.get(thread_id)
        if run is None:
            self._buffer_orphan(thread_id, message)
            return ()
        run_id = run.run_id
        raw_turn_id = params.get("turnId")
        turn_id = self.identifier(raw_turn_id) if isinstance(raw_turn_id, str) else None
        if turn_id is not None and (
            run.kind != "provider_child" or run_id not in self._terminal_children
        ):
            self._remember_turn(run_id, turn_id)
        if message.provider_request:
            return (self._normalize_interaction(run_id, message, params, turn_id),)
        if method == "turn/started":
            turn = params.get("turn")
            raw_started_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(raw_started_id, str):
                raise ValueError("turn start notification is malformed")
            started_id = self.identifier(raw_started_id)
            self._remember_turn(run_id, started_id)
            return (self._update(run_id, AgentEvent(
                kind="started",
                event_id=f"{started_id}:started",
                turn_id=started_id,
                agent_run_id=run_id,
                status="running",
            )),)
        if method == "item/agentMessage/delta":
            return (self._update(run_id, self.normalize_delta(
                run_id=run_id,
                thread_id=thread_id,
                params=params,
                turn_id=turn_id,
            )),)
        if method in ("item/started", "item/completed"):
            return self._normalize_item(run, thread_id, params, turn_id, method)
        if method == "thread/tokenUsage/updated":
            return (self._update(run_id, self.normalize_usage(
                run_id=run_id,
                thread_id=thread_id,
                params=params,
                turn_id=turn_id,
            )),)
        if method == "turn/completed":
            event = self.normalize_completion(run_id=run_id, params=params)
            self._active_turns.pop(run_id, None)
            if event.turn_id is not None:
                self._turn_runs.pop(event.turn_id, None)
            self.clear_transient(thread_id)
            status: Literal["completed", "cancelled", "provider_failed"] = (
                "cancelled" if event.kind == "cancelled" else
                "completed" if event.kind == "done" else
                "provider_failed"
            )
            return (*self._clear_interaction_updates(run_id, status), self._update(run_id, event))
        event_sequence = self._next_event_id
        self._next_event_id += 1
        return (self._update(run_id, AgentEvent(
            kind="other",
            event_id=f"{turn_id or thread_id}:unknown:{event_sequence}",
            turn_id=turn_id,
            agent_run_id=run_id,
            raw_type=method,
            status="running",
        )),)

    def _normalize_item(
        self,
        run: AgentRun,
        thread_id: str,
        params: dict[str, JsonValue],
        turn_id: str | None,
        method: str,
    ) -> tuple[_TransportUpdate, ...]:
        item = params.get("item")
        timestamp_key = "startedAtMs" if method == "item/started" else "completedAtMs"
        if turn_id is None or not isinstance(item, dict) or not isinstance(
            params.get(timestamp_key), int
        ):
            raise ValueError("item lifecycle notification is malformed")
        item_type = item.get("type")
        raw_item_id = item.get("id")
        if not isinstance(item_type, str) or not isinstance(raw_item_id, str):
            raise TypeError("item lifecycle notification is malformed")
        item_id = self.identifier(raw_item_id)
        stage: Literal["started", "completed"] = (
            "started" if method == "item/started" else "completed"
        )
        self.consume_delta_marker(
            thread_id, item_id, completed=stage == "completed"
        )
        if item_type == "collabAgentToolCall":
            updates = list(self.normalize_collaboration(
                parent=run,
                params=params,
                item=item,
                turn_id=turn_id,
                item_id=item_id,
                stage=stage,
            ))
            for update in tuple(updates):
                child = update.child
                if child is None:
                    raise ValueError("collaboration update has no child")
                for orphan in self._take_orphans(child.run_id):
                    updates.extend(self._normalize_message(orphan))
            return tuple(updates)
        if item_type == "agentMessage":
            text = item.get("text")
            if stage == "completed" and isinstance(text, str):
                return (self._update(run.run_id, AgentEvent(
                    kind="text",
                    event_id=f"{turn_id}:{item_id}:completed",
                    provider_cursor=item_id,
                    turn_id=turn_id,
                    agent_run_id=run.run_id,
                    text=text[:32_768],
                    status="running",
                )),)
            return ()
        if item_type in ("commandExecution", "mcpToolCall"):
            tool_input = item.get("command") if item_type == "commandExecution" else item.get("tool")
            output = item.get("aggregatedOutput") if stage == "completed" else ""
            return (self._update(run.run_id, AgentEvent(
                kind="tool" if stage == "started" else "tool_result",
                event_id=f"{turn_id}:{item_id}:{stage}",
                provider_cursor=item_id,
                turn_id=turn_id,
                agent_run_id=run.run_id,
                tool=item_type,
                tool_input=self._tool_text(tool_input),
                tool_id=item_id,
                text=self._tool_text(output),
                status="running",
            )),)
        return (self._update(run.run_id, AgentEvent(
            kind="other",
            event_id=f"{turn_id}:{item_id}:{stage}",
            provider_cursor=item_id,
            turn_id=turn_id,
            agent_run_id=run.run_id,
            raw_type=f"item/{item_type[:80]}/{stage}",
            status="running",
        )),)

    def _normalize_interaction(
        self,
        run_id: str,
        message: _RpcMessage,
        params: dict[str, JsonValue],
        turn_id: str | None,
    ) -> _TransportUpdate:
        raw_item_id = params.get("itemId")
        if turn_id is None or not isinstance(raw_item_id, str) or message.id is None:
            raise ValueError("interaction request is malformed")
        item_id = self.identifier(raw_item_id)
        interaction_id = self._interaction_identifier(message.id)
        method = message.method or ""
        disclosure: list[str] = []
        fields: list[InteractionField]
        kind: InteractionKind
        if method == "item/commandExecution/requestApproval":
            kind = "command_approval"
            disclosure = [self._tool_text(params.get("command"))]
            fields = [InteractionField(
                key="decision", kind="choice", label="Allow this command?",
                options=["accept", "decline"],
            )]
        elif method == "item/fileChange/requestApproval":
            kind = "file_change_approval"
            disclosure = [self._tool_text(params.get("reason"))]
            fields = [InteractionField(
                key="decision", kind="choice", label="Allow this file change?",
                options=["accept", "decline"],
            )]
        elif method == "item/tool/requestUserInput":
            kind = "user_input"
            questions = params.get("questions")
            if not isinstance(questions, list) or not questions or not all(
                isinstance(question, dict) for question in questions
            ):
                raise ValueError("user input request is malformed")
            fields = []
            for raw_question in questions:
                if not isinstance(raw_question, dict):
                    raise TypeError("user input question is malformed")
                question: dict[str, JsonValue] = raw_question
                options = question.get("options")
                labels: list[str] = []
                if isinstance(options, list):
                    for option in options:
                        if isinstance(option, dict):
                            label = option.get("label")
                            if isinstance(label, str):
                                labels.append(label)
                question_id = question.get("id")
                if not isinstance(question_id, str):
                    raise TypeError("user input question has no identifier")
                fields.append(InteractionField(
                    key=question_id,
                    kind="choice" if labels else "text",
                    label=self._tool_text(question.get("question")) or "Provider question",
                    options=labels,
                ))
        else:
            raise ValueError("unsupported provider interaction")
        interaction = ConversationInteraction(
            id=interaction_id,
            kind=kind,
            title=method,
            fields=fields,
            disclosure=disclosure,
        )
        self._register_interaction(
            interaction_id=interaction_id,
            request_id=message.id,
            method=method,
            interaction=interaction,
            run_id=run_id,
        )
        return self._update(run_id, AgentEvent(
            kind="interaction",
            event_id=f"{turn_id}:{item_id}:interaction:{interaction_id}",
            provider_cursor=item_id,
            turn_id=turn_id,
            agent_run_id=run_id,
            interaction=interaction,
            status="interaction_required",
        ))

    def _clear_interaction_updates(
        self,
        run_id: str,
        status: Literal["completed", "cancelled", "provider_failed"],
    ) -> tuple[_TransportUpdate, ...]:
        interactions = tuple(
            interaction_id
            for interaction_id, pending in self._interactions.items()
            if pending[3] == run_id
        )
        updates = []
        for interaction_id in interactions:
            self._interactions.pop(interaction_id, None)
            updates.append(self._update(run_id, AgentEvent(
                kind="interaction_resolved",
                event_id=f"{interaction_id}:closed:{status}",
                agent_run_id=run_id,
                status=status,
            )))
        return tuple(updates)

    def _provider_failure_updates(self) -> tuple[_TransportUpdate, ...]:
        updates: list[_TransportUpdate] = []
        active = tuple(self._active_turns.items())
        for run_id, _ in active:
            updates.extend(self._clear_interaction_updates(run_id, "provider_failed"))
        for run in self._runs.values():
            if run.kind != "provider_child" or run.run_id in self._terminal_children:
                continue
            updates.append(self._update(run.run_id, AgentEvent(
                kind="error",
                event_id=f"{run.run_id}:provider-error",
                turn_id=self._active_turns.get(run.run_id),
                agent_run_id=run.run_id,
                text="Codex provider protocol failed.",
                status="provider_failed",
            )))
        for run_id, turn_id in active:
            run = next((item for item in self._runs.values() if item.run_id == run_id), None)
            if run is None or run.kind == "provider_child":
                continue
            updates.append(self._update(run_id, AgentEvent(
                kind="error",
                event_id=f"{turn_id}:provider-error",
                turn_id=turn_id,
                agent_run_id=run_id,
                text="Codex provider protocol failed.",
                status="provider_failed",
            )))
        self._active_turns.clear()
        self._turn_runs.clear()
        return tuple(updates)

    def _remember_turn(self, run_id: str, turn_id: str) -> None:
        self._turn_runs[turn_id] = run_id
        self._active_turns[run_id] = turn_id

    @staticmethod
    def _update(run_id: str, event: AgentEvent, child: AgentRun | None = None):
        return _TransportUpdate(conversation_id=run_id, event=event, child=child)

    @staticmethod
    def _interaction_identifier(request_id: int | str) -> str:
        type_tag = "integer" if isinstance(request_id, int) else "string"
        encoded = json.dumps(
            [type_tag, request_id], ensure_ascii=True, separators=(",", ":")
        ).encode()
        return f"approval-{hashlib.sha256(encoded).hexdigest()[:24]}"

    @staticmethod
    def _tool_text(value: JsonValue | None) -> str:
        if not isinstance(value, str):
            return ""
        clean = value.replace("\x00", "")
        clean = re.sub(
            r"(?i)(--?(?:api[_-]?key|auth[_-]?token|authorization|token))"
            r"(?:\s*=\s*|\s+)(?:'[^']*'|\"[^\"]*\"|\S+)",
            r"\1=[redacted]", clean,
        )
        clean = re.sub(
            r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s'\"]+",
            r"\1[redacted]",
            clean,
        )
        clean = re.sub(
            r"(?i)\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD))\s*=\s*"
            r"(?:'[^']*'|\"[^\"]*\"|\S+)",
            r"\1=[redacted]",
            clean,
        )
        clean = re.sub(
            r"(?i)(?:https?://)([^\s:/@]+):[^\s@]+@",
            r"https://[redacted-userinfo]@", clean,
        )
        return " ".join(clean.split())[:500]

    async def request(self, method: str, params: dict[str, JsonValue]) -> JsonValue:
        if self._failed or self._process is None or self._process.stdin is None:
            raise ConnectionError("provider session is unavailable")
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write({"id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=_RPC_TIMEOUT)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise ConnectionError("provider session timed out") from exc

    async def notify(self, method: str, params: dict[str, JsonValue]) -> None:
        await self._write({"method": method, "params": params})

    async def respond(self, request_id: int | str, result: dict[str, JsonValue]) -> None:
        await self._write({"id": request_id, "result": result})

    async def next_event(self) -> _RpcMessage | None:
        return await self._events.get()

    async def close(self) -> None:
        self._closing = True
        process = self._process
        if process is None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except TimeoutError:
                process.kill()
                await process.wait()
        tasks = tuple(
            task for task in (self._reader, self._stderr_reader)
            if task is not None
        )
        for task in (self._reader, self._stderr_reader):
            if task is not None and not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._process = None

    @staticmethod
    def identifier(value: str) -> str:
        if value in (".", "..") or _ENTITY_ID.fullmatch(value) is None:
            raise ValueError("provider returned an invalid identifier")
        return value

    @staticmethod
    def model_identifier(value: str) -> str:
        if _MODEL_ID.fullmatch(value) is None:
            raise ValueError("provider returned an invalid model identifier")
        return value

    async def _spawn(self) -> None:
        env = self.provider.subscription_env(dict(os.environ))
        self._process = await asyncio.create_subprocess_exec(
            *contained(self.provider.app_server_command()),
            cwd=self.workspace_root,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_PROVIDER_LIMIT + 1,
        )
        self._reader = asyncio.create_task(self._read())
        self._stderr_reader = asyncio.create_task(self._drain_stderr())

    async def _write(self, payload: dict[str, JsonValue]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.stdin.is_closing():
            raise ConnectionError("provider session is unavailable")
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        try:
            process.stdin.write(encoded)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            await self._fail()
            raise ConnectionError("provider session ended unexpectedly") from exc

    async def _read(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            await self._fail()
            return
        try:
            while line := await process.stdout.readline():
                if len(line) > _PROVIDER_LIMIT:
                    await self._fail()
                    return
                try:
                    message = _RpcMessage.model_validate_json(line)
                except ValidationError as error:
                    await self._fail(
                        f"Codex sent an invalid app-server frame: {' '.join(str(error).split())[:400]}"
                    )
                    return
                if message.response:
                    if not isinstance(message.id, int):
                        await self._fail("Codex returned a response with a non-integer request id")
                        return
                    future = self._pending.pop(message.id, None)
                    if future is None:
                        await self._fail(f"Codex returned an unknown response id {message.id}")
                        return
                    if message.error is not None:
                        future.set_exception(ConnectionError(_provider_error_text(
                            message.error, fallback="Codex rejected the request",
                        )))
                    elif "result" not in message.model_fields_set:
                        future.set_exception(ConnectionError("provider returned no result"))
                    else:
                        future.set_result(message.result)
                elif message.provider_request or message.notification:
                    if message.provider_request:
                        frame = validate_request({
                            "id": message.id,
                            "method": message.method,
                            "params": message.params,
                        })
                        if frame["method"] in REJECTED_REQUEST_POLICIES:
                            await self._write(reject_unsupported(frame))
                            continue
                    await self._events.put(message)
                else:
                    await self._fail("Codex returned an app-server frame without method or id")
                    return
        except (JsonSchemaValidationError, ValueError) as error:
            await self._fail(
                f"Codex sent an invalid app-server message: {' '.join(str(error).split())[:400]}"
            )
            return
        except asyncio.LimitOverrunError:
            await self._fail("Codex sent an app-server frame larger than 1 MiB")
            return
        if not self._closing:
            await self._fail("Codex app-server closed its output before completing a request")

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while await process.stderr.read(8192):
            pass

    async def _fail(self, reason: str | None = None) -> None:
        if self._failed:
            return
        if reason:
            self._failure_reason = reason
        self._failed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError(self._failure_reason or "provider protocol failed"))
        self._pending.clear()
        process = self._process
        if process is not None and process.returncode is None:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        elif process is not None:
            await process.wait()
        self._process = None
        if self._stderr_reader is not None and not self._stderr_reader.done():
            self._stderr_reader.cancel()
        await self._events.put(None)



__all__: list[str] = []
