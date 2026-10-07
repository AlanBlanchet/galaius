"""Private explicit-completion transport with no session fallback."""

import asyncio
import importlib
import os
import uuid
from pathlib import Path
from typing import Literal, NotRequired, Required

from pydantic import PrivateAttr, TypeAdapter
from typing_extensions import TypedDict

from galaius.agents import quota
from galaius.agents.events import AgentEvent
from galaius.agents.registry import AgentRun
from galaius.agents.transport import (
    _ConversationStart,
    _ConversationTransport,
    _TransportUpdate,
    _TurnStart,
)


class _ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


class _CompletionMessage(TypedDict):
    content: str | None


class _CompletionChoice(TypedDict):
    message: _CompletionMessage


class _CompletionUsage(TypedDict, total=False):
    prompt_tokens: int
    completion_tokens: int


class _CompletionPayload(TypedDict, total=False):
    choices: Required[list[_CompletionChoice]]
    usage: NotRequired[_CompletionUsage]


_COMPLETION = TypeAdapter(_CompletionPayload)


class _CompletionTransport(_ConversationTransport):
    """Own explicit API execution and normalize its result before it reaches the host."""

    provider: str
    _runs: dict[str, AgentRun] = PrivateAttr(default_factory=dict)
    _history: dict[str, list[_ChatMessage]] = PrivateAttr(default_factory=dict)
    _updates: asyncio.Queue[_TransportUpdate | None] = PrivateAttr(default_factory=asyncio.Queue)
    _tasks: dict[str, asyncio.Task[None]] = PrivateAttr(default_factory=dict)

    @staticmethod
    def history(events: list[AgentEvent]) -> list[_ChatMessage]:
        messages: list[_ChatMessage] = []
        for event in events:
            if event.kind == "prompt":
                messages.append(_ChatMessage(role="user", content=event.text))
            elif event.kind == "text":
                messages.append(_ChatMessage(role="assistant", content=event.text))
        return messages

    async def start_conversation(
        self, *, model: str, workspace: Path, instruction: str | None
    ) -> _ConversationStart:
        del workspace
        conversation_id = str(uuid.uuid4())
        self._history[conversation_id] = (
            [_ChatMessage(role="system", content=instruction)]
            if instruction is not None else []
        )
        return _ConversationStart(conversation_id=conversation_id, model=model)

    async def resume_conversation(
        self, *, conversation_id: str, model: str, workspace: Path
    ) -> _ConversationStart:
        del conversation_id, model, workspace
        raise ConnectionError("completion conversations cannot resume after restart")

    async def start_turn(self, *, conversation_id: str, prompt: str) -> _TurnStart:
        run = self._runs.get(conversation_id)
        if run is None:
            raise ValueError("completion conversation is not bound")
        model = run.model
        if model is None:
            raise ValueError("completion conversation has no resolved model")
        turn_id = str(uuid.uuid4())
        messages = [*self._history.get(conversation_id, []), _ChatMessage(
            role="user", content=prompt,
        )]
        task = asyncio.create_task(
            self._complete_turn(run.run_id, turn_id, model, messages),
            name=f"completion:{run.run_id}:{turn_id}",
        )
        self._tasks[turn_id] = task
        task.add_done_callback(lambda completed: self._forget_task(turn_id, completed))
        return _TurnStart(turn_id=turn_id)

    async def cancel_turn(self, *, conversation_id: str, turn_id: str) -> None:
        if conversation_id not in self._runs:
            raise ConnectionError("completion conversation is not active")
        task = self._tasks.get(turn_id)
        if task is None or not task.cancel():
            raise ConnectionError("completion turn is no longer active")
        try:
            await task
        except asyncio.CancelledError:
            pass
        if not task.cancelled():
            raise ConnectionError("completion turn could not be cancelled")

    def _forget_task(self, turn_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(turn_id) is task:
            self._tasks.pop(turn_id, None)

    async def submit_interaction(
        self, *, run_id: str, interaction_id: str, values: dict[str, str | bool]
    ) -> None:
        del run_id
        raise ConnectionError("completion transport has no provider interactions")

    async def next_update(self) -> _TransportUpdate | None:
        return await self._updates.get()

    def bind_conversation(self, run: AgentRun) -> None:
        if run.model is None:
            raise ValueError("completion conversation has no resolved model")
        conversation_id = run.provider_session_id or run.run_id
        self._runs[conversation_id] = run.model_copy(deep=True)
        self._history.setdefault(conversation_id, self.history([]))

    def has_conversation(self, conversation_id: str) -> bool:
        return conversation_id in self._runs

    async def _complete_turn(
        self, run_id: str, turn_id: str, model: str, messages: list[_ChatMessage]
    ) -> None:
        events = await self.complete(
            run_id=run_id, turn_id=turn_id, model=model, messages=messages
        )
        for event in events:
            await self._updates.put(_TransportUpdate(conversation_id=run_id, event=event))
        response = next((event.text for event in events if event.kind == "text"), None)
        if response is not None:
            self._history[run_id] = [
                *messages, _ChatMessage(role="assistant", content=response),
            ]

    async def complete(
        self, *, run_id: str, turn_id: str, model: str, messages: list[_ChatMessage]
    ) -> tuple[AgentEvent, ...]:
        try:
            client = importlib.import_module("litellm")
            client.__dict__["suppress_debug_info"] = True
            response = await client.acompletion(
                model=model,
                messages=messages,
                api_base=(
                    os.environ.get("OPENAI_API_BASE")
                    if self.provider == "openai" else None
                ),
                num_retries=0,
                timeout=30,
            )
            payload = _COMPLETION.validate_python(response.model_dump())
            if not payload["choices"]:
                raise ValueError("completion has no choice")
            text = payload["choices"][0]["message"]["content"]
            if text is None or not text.strip():
                raise ValueError("completion has no text")
            usage = payload.get("usage", {})
            quota.served(self.provider, model)  # it answered: no earlier refusal holds against it
            return (
                AgentEvent(
                    kind="text", event_id=f"{turn_id}:response", turn_id=turn_id,
                    agent_run_id=run_id, text=text, status="running",
                ),
                AgentEvent(
                    kind="done", event_id=f"{turn_id}:done", turn_id=turn_id,
                    agent_run_id=run_id, text=text,
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"), status="completed",
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- clients expose provider-specific failures.
            # This is the ONE place a real vendor call answers for this transport — the same
            # shared cooldown memory a CLI-subprocess spawn writes to (`agents/run.py`,
            # `~/.galaius/out/agents/quota-cooldowns.json`) is written here too, so a quota
            # refusal heard on THIS route is honoured by every other launch, not just this one.
            # The exception's raw text is used ONLY for that match — the displayed event stays
            # the existing generic message; dumping a raw vendor exception body into the chat
            # panel would be its own regression, not part of this fix.
            if quota.REFUSAL.search(str(exc)):
                quota.record_refusal(self.provider, model, said=str(exc))
            return (AgentEvent(
                kind="error", event_id=f"{turn_id}:error", turn_id=turn_id,
                agent_run_id=run_id, text="Explicit API request failed.",
                status="provider_failed",
            ),)

    async def close(self) -> None:
        for task in tuple(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

__all__: list[str] = []
