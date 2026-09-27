"""Private local conversation host for the extension's newline-JSON console."""

import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    PrivateAttr,
    ValidationError,
)
from interact_core import PromptExecutionRef

from interact.agents import registry as reg
from interact.agents.assembly import _TransportRegistry, build_transport_registry
from interact.agents.events import AgentEvent
from interact.agents.protocol import (
    CancelCommand,
    CatalogCommand,
    CatalogResponse,
    ConversationCatalog,
    ConversationCommand,
    ConversationErrorCode,
    ConversationMethod,
    ConversationRequest,
    ConversationResponseValue,
    ConversationRoute,
    ConversationStreamEvent,
    ErrorResponse,
    InitializeCommand,
    InitializeResponse,
    InteractionCommand,
    ModelSelection,
    RunResponse,
    SendCommand,
    StartCommand,
)
from interact.agents.transport import _ConversationTransport
from interact.criteria import Variables
from interact.config import Config
from interact.prompt_cache import _PromptCache
from interact.prompt_client import _PromptClient
from interact.prompt_secret import read_prompt_token

_INPUT_LIMIT = 128 * 1024
_ENTITY_ID = re.compile(r"^[A-Za-z0-9._:@+-]{1,160}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_METHODS: list[ConversationMethod] = [
    "initialize", "catalog", "start", "send", "cancel", "interaction",
]
class _ConversationHost(BaseModel):
    """Private owner of one console, its provider process, and all active turns."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    workspace_root: Path
    transport_registry: _TransportRegistry
    config: Config
    _outbound: asyncio.Queue[BaseModel | None] = PrivateAttr(default_factory=asyncio.Queue)
    _holding: bool = PrivateAttr(default=False)
    _held_events: list[ConversationStreamEvent] = PrivateAttr(default_factory=list)
    _transport_consumers: dict[int, asyncio.Task[None]] = PrivateAttr(default_factory=dict)
    _background: set[asyncio.Task[None]] = PrivateAttr(default_factory=set)
    _active_turns: dict[str, str] = PrivateAttr(default_factory=dict)
    _projector: reg._ConversationProjector = PrivateAttr(default_factory=reg._ConversationProjector)
    _cancelled_turns: dict[str, str] = PrivateAttr(default_factory=dict)
    _projection_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

    async def serve(self) -> None:
        writer = asyncio.create_task(self._write_output())
        failures: list[BaseException] = []
        try:
            while line := await self._read_input():
                await self._serve_line(line)
        except BaseException as error:
            failures.append(error)
        finally:
            background = tuple(self._background)
            for task in background:
                task.cancel()
            for task in self._transport_consumers.values():
                task.cancel()
            if background:
                results = await asyncio.gather(*background, return_exceptions=True)
                failures.extend(
                    result for result in results
                    if isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                )
            if self._transport_consumers:
                results = await asyncio.gather(
                    *self._transport_consumers.values(), return_exceptions=True
                )
                failures.extend(
                    result for result in results
                    if isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                )
            try:
                await self.transport_registry.close()
            except BaseException as error:
                failures.append(error)
            try:
                await self._outbound.put(None)
                await writer
            except BaseException as error:
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("conversation host failed during shutdown", failures)

    async def _serve_line(self, line: bytes) -> None:
        self._holding = True
        self._held_events.clear()
        if len(line) > _INPUT_LIMIT:
            await self._discard_input_tail(line)
            response: ConversationResponseValue = self._error(
                "invalid", None, "invalid_request", "Request exceeds the console size limit."
            )
        else:
            try:
                command = ConversationCommand.model_validate_json(line).root
            except ValidationError:
                request_id, method, version = self._input_identity(line)
                code: ConversationErrorCode = (
                    "unsupported_version" if version is not None and version != 1
                    else "invalid_request"
                )
                response = self._error(
                    request_id,
                    method,
                    code,
                    "Request does not match the typed conversation protocol.",
                )
            else:
                response = await self._dispatch(command)
        await self._outbound.put(response)
        self._holding = False
        for event in self._held_events:
            await self._outbound.put(event)
        self._held_events.clear()

    async def _dispatch(self, command):
        if isinstance(command, InitializeCommand):
            return InitializeResponse(
                version=1, type="response", method="initialize", request_id=command.request_id,
                ok=True, methods=_METHODS,
            )
        if isinstance(command, CatalogCommand):
            return CatalogResponse(
                version=1, type="response", method="catalog", request_id=command.request_id,
                ok=True, catalog=await self._catalog(),
            )
        if isinstance(command, StartCommand):
            return await self._start(command)
        if isinstance(command, SendCommand):
            return await self._send(command)
        if isinstance(command, CancelCommand):
            return await self._cancel(command)
        return await self._interaction(cast(InteractionCommand, command))

    async def _catalog(self) -> ConversationCatalog:
        now = time.time()
        # Only routes that could actually carry a conversation. This used to MANUFACTURE two more
        # — a Claude and a Gemini consumer session — for the sole purpose of refusing them, and
        # the panel rendered all three side by side as "3 unavailable routes". Nothing the owner
        # can do makes those available, so they weren't routes — a policy notice wearing a route's
        # clothes, reading as a third broken feature next to a real "Codex CLI is not installed"
        # (which names an action he can take).
        #
        # The policy itself is unchanged and lives where it's enforced: interact never drives a
        # vendor's consumer session as a route. It spawns that vendor's own CLI, using his login
        # legitimately — a different thing, and how every agent already runs.
        routes = [await self.transport_registry.session_route(now)]
        routes.extend(self.transport_registry.completion_routes(now))
        return ConversationCatalog(routes=routes, criteria=Variables.names(), cataloged_at=now)

    async def _start(self, command: StartCommand) -> ConversationResponseValue:
        try:
            workspace = self._workspace(command.request.workspace_root)
        except ValueError:
            return self._error(
                command.request_id, command.method, "invalid_request",
                "Workspace must be an existing directory inside the allowed root.",
            )
        try:
            instruction, prompt_ref = self._resolve_prompt(command.request)
        except (LookupError, OSError, ValueError):
            return self._error(
                command.request_id, command.method, "invalid_request",
                "The referenced prompt could not be verified.",
            )
        catalog = await self._catalog()
        route = catalog.route_by_id(command.request.route_id)
        if route is None:
            return self._error(
                command.request_id, command.method, "not_found", "Selected route does not exist."
            )
        if route.availability != "available":
            code: ConversationErrorCode
            if route.availability == "unauthenticated":
                code = "unauthenticated"
            elif route.availability == "incompatible":
                code = "incompatible"
            else:
                code = "unavailable"
            return self._error(command.request_id, command.method, code, route.reason)
        try:
            model = route.resolve(command.request.selection)
        except ValueError:
            return self._error(
                command.request_id, command.method, "unavailable",
                "Selected model is not available on the selected route.",
            )
        connection = self.transport_registry.require(route)
        try:
            started = await connection.start_conversation(
                model=model.id, workspace=workspace, instruction=instruction
            )
            run_id = started.conversation_id
        except (ConnectionError, ValidationError, ValueError):
            return self._error(
                command.request_id, command.method, "provider_failed",
                "Provider could not start the selected conversation.",
            )
        if reg.get_run(run_id) is not None:
            return self._error(
                command.request_id,
                command.method,
                "provider_failed",
                "Provider returned a conversation identifier that is already in use.",
            )
        run = reg.AgentRun(
            run_id=run_id,
            kind="conversation",
            provider=route.provider,
            name=f"{route.label} conversation",
            task=command.request.prompt[:500],
            cwd=str(workspace),
            project=reg.project_for(str(workspace)),
            pid=connection.pid,
            model=model.id,
            root_run_id=run_id,
            provider_session_id=run_id,
            connection=route.connection,
            requested_model=command.request.selection.model,
            requested_criterion=command.request.selection.criterion,
            prompt=prompt_ref,
            cataloged_at=route.cataloged_at,
            charge_path=route.charge_path,
            cost_certainty=route.cost_certainty,
            capabilities=list(route.capabilities),
            started_at=time.time(),
            status="starting",
        )
        reg.save_run(run)
        self.transport_registry.bind(route, run)
        self._ensure_transport_consumer(connection)
        try:
            await self._begin_turn(connection, run, command.request.prompt)
        except (ConnectionError, ValidationError, ValueError):
            await self._record_event(run_id, AgentEvent(
                kind="error",
                event_id=f"{run_id}:start:error",
                text="Provider protocol failed.",
                status="provider_failed",
            ))
        current = reg.get_run(run_id) or run
        return RunResponse(
            version=1, type="response", method="start", request_id=command.request_id,
            ok=True, run=current,
        )

    def _resolve_prompt(
        self, request: ConversationRequest,
    ) -> tuple[str | None, PromptExecutionRef | None]:
        if request.prompt_selection is None:
            return None, None
        if (
            not self.config.prompt_endpoint
            or not self.config.prompt_account
            or (not self.config.prompt_token and self.config.prompt_token_file is None)
        ):
            raise ValueError("prompt service configuration is incomplete")
        if self.config.prompt_token and self.config.prompt_token_file is not None:
            raise ValueError("prompt token configuration is ambiguous")
        token = (
            read_prompt_token(self.config.prompt_token_file)
            if self.config.prompt_token_file is not None else self.config.prompt_token
        )
        cache = _PromptCache(self.config.prompt_cache)
        try:
            content, reference = _PromptClient(
                self.config.prompt_endpoint, token
            ).resolve(self.config.prompt_account, cache, request.prompt_selection)
        finally:
            cache.close()
        return content, reference

    async def _resolve_continuation_model(self, run: reg.AgentRun):
        """The model a RESUMED turn should actually run on — re-derived exactly like a fresh
        `_start`, never `run.model` replayed verbatim. `route.id` is deterministic from
        provider+connection (`f"{provider}:{connection}"`, `assembly.py`), so the run's own
        stored fields find it in a FRESH catalog without a separately persisted route id.
        Returns the resolved `Model` (`interact.agents.protocol` owns that import — the host
        stays free of concrete provider/model imports, an architecture invariant this module is
        checked against).
        """
        if run.connection is None:
            raise ValueError("stored conversation has no recorded connection")
        catalog = await self._catalog()
        route = catalog.route_by_id(f"{run.provider}:{run.connection}")
        if route is None:
            raise ValueError(f"route {run.provider}:{run.connection} no longer exists")
        return route.resolve(ModelSelection(model=run.requested_model, criterion=run.requested_criterion))

    async def _send(self, command: SendCommand) -> ConversationResponseValue:
        run = reg.get_run(command.run_id)
        if run is None or run.kind != "conversation":
            return self._error(
                command.request_id, command.method, "not_found", "Conversation does not exist."
            )
        if run.run_id in self._active_turns:
            return self._error(
                command.request_id, command.method, "conflict",
                "Conversation already has an active turn.",
            )
        try:
            workspace = self._workspace(run.cwd)
        except ValueError:
            return self._error(
                command.request_id, command.method, "invalid_request",
                "Stored workspace is outside the allowed root.",
            )
        try:
            connection = await self.transport_registry.for_run(run)
            run.pid = connection.pid
            reg.save_run(run)
            conversation_id = run.provider_session_id
            if conversation_id is None:
                raise ValueError("stored conversation has no provider session")
            if not connection.has_conversation(conversation_id):
                # Re-resolve exactly like `_start` does — never replay the model RECORDED on the
                # run: that model may have since gone into quota cooldown (the CLI-subprocess
                # spawn path had this exact bug). Scoped to this run's OWN route/provider — a
                # vendor session cannot switch CLI mid-conversation, only the model it is asked
                # for — and refuses via `route.resolve`'s own quota check when nothing on this
                # route currently clears the run's original selection.
                try:
                    model = await self._resolve_continuation_model(run)
                except ValueError as error:
                    return self._error(
                        command.request_id, command.method, "unavailable", str(error),
                    )
                await connection.resume_conversation(
                    conversation_id=conversation_id,
                    model=model.id,
                    workspace=workspace,
                )
                connection.bind_conversation(run)
                # The stored record must reflect what is ACTUALLY running, not the model the
                # conversation started on — the whole point of re-resolving is that they can differ.
                run.model = model.id
                reg.save_run(run)
            self._ensure_transport_consumer(connection)
            await self._begin_turn(connection, run, command.prompt)
        except (ConnectionError, ValidationError, ValueError):
            await self._record_event(run.run_id, AgentEvent(
                kind="error",
                event_id=f"{run.run_id}:continuation:{uuid.uuid4()}:error",
                agent_run_id=run.run_id,
                text="Provider continuation failed.",
                status="provider_failed",
            ))
            return self._error(
                command.request_id, command.method, "provider_failed",
                "Provider could not continue this conversation.",
            )
        return RunResponse(
            version=1, type="response", method="send", request_id=command.request_id,
            ok=True, run=reg.get_run(run.run_id) or run,
        )

    async def _begin_turn(
        self, connection: _ConversationTransport, run: reg.AgentRun, prompt: str
    ) -> None:
        if run.provider_session_id is None:
            raise ConnectionError("provider session is unavailable")
        async with self._projection_lock:
            started = await connection.start_turn(
                conversation_id=run.provider_session_id, prompt=prompt
            )
            turn_id = started.turn_id
            await self._record_event(run.run_id, AgentEvent(
                kind="prompt", event_id=f"{turn_id}:prompt",
                turn_id=turn_id, agent_run_id=run.run_id, text=prompt, status="running",
            ))
            self._active_turns[run.run_id] = turn_id

    async def _cancel(self, command: CancelCommand) -> ConversationResponseValue:
        run = reg.get_run(command.run_id)
        turn_id = self._active_turns.get(command.run_id)
        if run is None:
            return self._error(
                command.request_id, command.method, "not_found", "Conversation does not exist."
            )
        if turn_id is None:
            acknowledged = self._cancelled_turns.get(command.run_id)
            if acknowledged is not None:
                return RunResponse(
                    version=1, type="response", method="cancel",
                    request_id=command.request_id, ok=True, run=run,
                )
            return self._error(
                command.request_id, command.method, "conflict",
                "Conversation has no active turn.",
            )
        try:
            connection = await self.transport_registry.for_run(run)
            if run.provider_session_id is None:
                raise ConnectionError("provider session is unavailable")
            await connection.cancel_turn(
                conversation_id=run.provider_session_id, turn_id=turn_id
            )
        except ConnectionError:
            return self._error(
                command.request_id, command.method, "provider_failed",
                "Provider did not acknowledge the interruption.",
            )
        self._active_turns.pop(run.run_id, None)
        self._cancelled_turns[run.run_id] = turn_id
        await self._record_event(run.run_id, AgentEvent(
            kind="cancelled", event_id=f"{turn_id}:cancelled", turn_id=turn_id,
            agent_run_id=run.run_id, text="Turn cancelled.", status="cancelled",
        ))
        return RunResponse(
            version=1, type="response", method="cancel", request_id=command.request_id,
            ok=True, run=reg.get_run(run.run_id) or run,
        )

    async def _interaction(self, command: InteractionCommand) -> ConversationResponseValue:
        run = reg.get_run(command.run_id)
        interaction_id = command.submission.interaction_id
        if run is None:
            return self._error(
                command.request_id, command.method, "not_found", "Interaction does not exist."
            )
        connection = await self.transport_registry.for_run(run)
        if not connection.has_interaction(run_id=run.run_id, interaction_id=interaction_id):
            return self._error(
                command.request_id, command.method, "not_found", "Interaction does not exist."
            )
        try:
            await connection.submit_interaction(
                run_id=run.run_id,
                interaction_id=interaction_id,
                values=command.submission.values,
            )
        except ValueError:
            return self._error(
                command.request_id, command.method, "invalid_request",
                "Interaction response does not match the pending fields.",
            )
        except ConnectionError:
            return self._error(
                command.request_id, command.method, "provider_failed",
                "Provider did not accept the interaction response.",
            )
        run.status = "running"
        reg.save_run(run)
        await self._record_event(run.run_id, AgentEvent(
            kind="interaction_resolved",
            event_id=f"{command.submission.interaction_id}:resolved",
            agent_run_id=run.run_id,
            status="running",
        ))
        return RunResponse(
            version=1, type="response", method="interaction", request_id=command.request_id,
            ok=True, run=reg.get_run(run.run_id) or run,
        )

    def _ensure_transport_consumer(self, connection: _ConversationTransport) -> None:
        identity = id(connection)
        if identity in self._transport_consumers:
            return
        task = asyncio.create_task(self._consume_transport(connection))
        self._transport_consumers[identity] = task

    async def _consume_transport(self, connection: _ConversationTransport) -> None:
        while True:
            update = await connection.next_update()
            if update is None:
                return
            async with self._projection_lock:
                projected = await self._record_event(
                    update.conversation_id, update.event, child=update.child
                )
            event = update.event
            if event.kind in ("done", "error", "cancelled"):
                self._active_turns.pop(update.conversation_id, None)
            elif (
                event.kind == "started"
                and event.turn_id is not None
                and projected is not None
                and projected.status == "running"
            ):
                self._active_turns[update.conversation_id] = event.turn_id

    async def _record_event(
        self, run_id: str, event: AgentEvent, *, child: reg.AgentRun | None = None
    ) -> reg.AgentRun | None:
        run = (
            self._projector.apply(run_id, event)
            if child is None
            else self._projector.apply_child(child, event)
        )
        if run is None:
            return None
        envelope = ConversationStreamEvent(version=1, type="event", run=run, event=event)
        if self._holding:
            self._held_events.append(envelope)
        else:
            await self._outbound.put(envelope)
        return run

    def _workspace(self, requested: str | None) -> Path:
        candidate = self.workspace_root if requested is None else Path(requested)
        if not candidate.is_absolute():
            raise ValueError("workspace must be absolute")
        try:
            resolved = candidate.resolve(strict=True)
            allowed = self.workspace_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("workspace is unavailable") from exc
        if not resolved.is_dir() or not resolved.is_relative_to(allowed):
            raise ValueError("workspace is outside the allowed root")
        return resolved

    @staticmethod
    def _error(
        request_id: str,
        method: ConversationMethod | None,
        code: ConversationErrorCode,
        message: str,
    ) -> ErrorResponse:
        safe_request = request_id if _REQUEST_ID.fullmatch(request_id) else "invalid"
        return ErrorResponse(
            version=1,
            type="response",
            method=method,
            request_id=safe_request,
            ok=False,
            error_code=code,
            error=" ".join(message.split())[:500],
        )

    @staticmethod
    def _input_identity(
        line: bytes,
    ) -> tuple[str, ConversationMethod | None, int | None]:
        try:
            raw = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return "invalid", None, None
        if not isinstance(raw, dict):
            return "invalid", None, None
        request_id = raw.get("request_id")
        method = raw.get("method")
        version = raw.get("version")
        safe_request = request_id if isinstance(request_id, str) else "invalid"
        safe_method = cast(ConversationMethod, method) if method in _METHODS else None
        safe_version = version if isinstance(version, int) else None
        return safe_request, safe_method, safe_version

    async def _read_input(self) -> bytes:
        return await asyncio.to_thread(sys.stdin.buffer.readline, _INPUT_LIMIT + 1)

    async def _discard_input_tail(self, first: bytes) -> None:
        if first.endswith(b"\n"):
            return
        while chunk := await asyncio.to_thread(sys.stdin.buffer.readline, _INPUT_LIMIT + 1):
            if chunk.endswith(b"\n"):
                return

    async def _write_output(self) -> None:
        while message := await self._outbound.get():
            sys.stdout.write(message.model_dump_json() + "\n")
            sys.stdout.flush()

class ConversationRefused(RuntimeError):
    """The host answered a command with an error: `code` is its ConversationErrorCode."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConversationHost:
    """The same conversations the extension's console hosts, driven in-process by a caller that is
    not a newline-JSON pipe (the machine runner serving the web): each call is one command, its
    answer the run as the registry now holds it, a refusal a `ConversationRefused`. Stream events
    still land in the registry (`_record_event`); the console's outbound copy is drained here."""

    def __init__(self, workspace_root: Path) -> None:
        resolved = workspace_root.expanduser().resolve(strict=True)
        self._host = _ConversationHost(workspace_root=resolved, transport_registry=build_transport_registry(resolved), config=Config())
        self._drain = asyncio.get_running_loop().create_task(self._discard_outbound())

    async def _discard_outbound(self) -> None:
        while await self._host._outbound.get() is not None:
            pass

    async def _run(self, command: BaseModel) -> reg.AgentRun:
        answer = await self._host._dispatch(command)
        if isinstance(answer, ErrorResponse):
            raise ConversationRefused(answer.error_code, answer.error)
        return cast(RunResponse, answer).run

    @staticmethod
    def _request_id() -> str:
        return uuid.uuid4().hex

    async def catalog(self) -> ConversationCatalog:
        return await self._host._catalog()

    async def start(self, prompt: str, workspace: Path, *, route_id: str, model: str | None) -> reg.AgentRun:
        request = {"route_id": route_id, "prompt": prompt, "workspace_root": str(workspace), "selection": ModelSelection(model=model).model_dump()}
        return await self._run(StartCommand.model_validate({"version": 1, "request_id": self._request_id(), "method": "start", "request": request}))

    async def send(self, run_id: str, prompt: str) -> reg.AgentRun:
        return await self._run(SendCommand(version=1, request_id=self._request_id(), method="send", run_id=run_id, prompt=prompt))

    async def cancel(self, run_id: str) -> reg.AgentRun:
        return await self._run(CancelCommand(version=1, request_id=self._request_id(), method="cancel", run_id=run_id))

    async def answer(self, run_id: str, interaction_id: str, values: dict[str, str | bool]) -> reg.AgentRun:
        return await self._run(InteractionCommand.model_validate({"version": 1, "request_id": self._request_id(), "method": "interaction", "run_id": run_id,
                                                                  "submission": {"interaction_id": interaction_id, "values": values}}))

    async def close(self) -> None:
        await self._host.transport_registry.close()
        await self._host._outbound.put(None)
        await self._drain


def run_console(workspace_root: Path) -> None:
    """Standalone CLI launcher for one least-privileged local conversation console."""
    resolved = workspace_root.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("workspace root must be a directory")
    asyncio.run(_ConversationHost(
        workspace_root=resolved,
        transport_registry=build_transport_registry(resolved),
        config=Config(),
    ).serve())


__all__ = ["ConversationHost", "ConversationRefused", "run_console"]
