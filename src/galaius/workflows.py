"""Run a server workflow from a script and continue with its outputs — the reverse direction of a
machine-function workflow node: there, code on this PC is reached FROM a workflow; here, code on
this PC REACHES a workflow and gets the hand back when it ends.

    from galaius.client import Client
    run = Client().workflows.run("Summarize a page", inputs={"url": "https://example.org"})
    print(run.outputs["summary"])

`AsyncClient` is the same surface with `await` / `async for`. Every call goes through
`ServerWorkspace` (the configured `galaius agents sync` connection: loopback session or workspace
API key), so the SDK, the CLI (`galaius workflows run`), the TUI and the editor extension share
one revision-guarded, idempotent start path."""

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

import httpx
from galaius_core import ArtifactRef, TriggerInvocation, WorkflowEvent, WorkflowRevision, WorkflowRevisionRef, WorkflowRun
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from galaius.server_workspace import ServerWorkspace, WorkflowRunRequest, WorkflowRunUncertain

#: A run in one of these statuses has reached its outcome; nothing about it changes any more.
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


class WorkflowNotFound(LookupError):
    """No workflow, or more than one, answers to this name or id in the connected workspace."""


class WorkflowRunFailed(RuntimeError):
    """The run ended without succeeding; `run` holds its record (status, error, recovered steps)."""

    def __init__(self, run: WorkflowRun) -> None:
        super().__init__(f"workflow run {run.id} {run.status}: {run.error or 'no error message'}")
        self.run = run


class ServerBound(BaseModel):
    """The connected workspace plus how often a waiting caller asks it again."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    server: ServerWorkspace = Field(default_factory=ServerWorkspace.configured)
    poll_seconds: float = Field(default=1.0, ge=0)
    #: A read is asked again when the CONNECTION failed (timeout, dropped socket: a restart or a
    #: busy moment of the server), never when the server answered — a waiting script outlives
    #: about a minute of that before it gives up.
    read_attempts: int = Field(default=8, ge=1)

    def read(self, call, *arguments):
        for attempt in range(1, self.read_attempts + 1):
            try:
                return call(*arguments)
            except httpx.TransportError:
                if attempt == self.read_attempts:
                    raise
                time.sleep(min(self.poll_seconds * 2 ** attempt, 10))
        raise AssertionError("the last attempt returns or raises")


class RunState(ServerBound):
    """One run as this client last saw it: the exact workflow revision it runs (names its outputs
    and steps), its record, and the event cursor. `Run` adds the blocking I/O, `AsyncRun` the same
    I/O for asyncio code."""

    workflow: WorkflowRevision
    record: WorkflowRun
    cursor: int = 0

    @property
    def id(self) -> UUID:
        return self.record.id

    @property
    def status(self):
        return self.record.status

    @property
    def done(self) -> bool:
        return self.record.status in TERMINAL_STATUSES

    @property
    def error(self) -> str | None:
        return self.record.error

    @property
    def outputs(self) -> dict[str, object]:
        """Each exposed output by name — a file output is an `ArtifactRef` (`download` fetches
        it); an output the run did not produce is absent (`record.skipped_outputs` says why).
        A workflow exposing no output has none here; `record.result` holds its last step's value.
        Raises `WorkflowRunFailed` unless the run succeeded, so a script never continues on a
        failed run's missing values."""
        if self.record.status != "succeeded":
            raise WorkflowRunFailed(self.record)
        names = [entry.name for entry in self.workflow.interface.outputs]
        result = self.record.result
        values = {names[0]: result} if len(names) == 1 else dict(result) if isinstance(result, dict) and names else {}
        return {name: self.typed(value) for name, value in values.items() if name not in self.record.skipped_outputs}

    @property
    def files(self) -> dict[str, ArtifactRef]:
        return {name: value for name, value in self.outputs.items() if isinstance(value, ArtifactRef)}

    @staticmethod
    def typed(value: object) -> object:
        """A file travels as a JSON object inside a multi-output result; give it back its type."""
        if not isinstance(value, dict):
            return value
        try:
            return ArtifactRef.model_validate(value)
        except ValidationError:
            return value

    def summary(self) -> dict[str, object]:
        """The JSON a shell script reads: identity, outcome, outputs (files as their record)."""
        return {
            "run_id": str(self.record.id),
            "workflow": {"id": str(self.workflow.key.id), "revision": str(self.workflow.revision), "name": self.workflow.name},
            "status": self.record.status,
            "outputs": {name: value.model_dump(mode="json") if isinstance(value, ArtifactRef) else value
                        for name, value in self.outputs.items()} if self.record.status == "succeeded" else None,
            "result": self.record.result if self.record.status == "succeeded" else None,
            "skipped_outputs": self.record.skipped_outputs,
            "error": self.record.error,
        }

    def describe(self, event: WorkflowEvent) -> str | None:
        """One progress line for a person watching a terminal; `None` for agent-internal detail
        (tool calls, delegations) the run page shows instead."""
        elapsed = (event.timestamp - self.record.created_at).total_seconds()
        if event.kind != "progress":
            return f"[{elapsed:7.1f}s] run {event.kind}"
        if event.payload.get("type") != "step":
            return None
        node = UUID(str(event.payload["node_id"]))
        label = next((item.label for item in self.workflow.nodes if item.id == node), str(node))
        duration = event.payload.get("duration_ms")
        detail = event.payload.get("error") or event.payload.get("skipped_because") or (f"{duration} ms" if duration is not None else "")
        return f"[{elapsed:7.1f}s] {label}: {event.payload['status']}{' - ' + detail if detail else ''}"

    def artifact(self, output: str | ArtifactRef) -> ArtifactRef:
        return self.files[output] if isinstance(output, str) else output

    @staticmethod
    def deadline(timeout: float | None) -> float | None:
        return None if timeout is None else time.monotonic() + timeout

    def pause(self, deadline: float | None) -> float | None:
        """After a fresh read: `None` once the run ended, else how long to wait before the next
        one. Past `deadline`, `TimeoutError` — the run itself keeps going server-side and
        `attach(run.id)` picks it back up."""
        if self.done:
            return None
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"workflow run {self.record.id} still {self.record.status} at its timeout; it keeps running server-side")
        return self.poll_seconds

    def to(self, kind: type):
        """The same run as another `RunState` kind (`Run` -> `AsyncRun`)."""
        return kind(**{name: getattr(self, name) for name in RunState.model_fields})

    def follow(self, events: tuple[WorkflowEvent, ...]) -> tuple[WorkflowEvent, ...]:
        if events:
            self.cursor = events[-1].sequence
        return events


class Run(RunState):
    """A started run. `wait()` blocks until it ends; `stream()` yields its events as they land."""

    def refresh(self) -> Self:
        self.record = self.read(self.server.workflow_run, self.record.id)
        return self

    def events(self) -> tuple[WorkflowEvent, ...]:
        """Events since the last call."""
        return self.follow(self.read(self.server.workflow_events, self.record.id, self.cursor))

    def polls(self, timeout: float | None = None) -> Iterator[Self]:
        """One fresh record per round until the run ends."""
        deadline = self.deadline(timeout)
        while True:
            yield self.refresh()
            if (pause := self.pause(deadline)) is None:
                return
            time.sleep(pause)

    def stream(self, timeout: float | None = None) -> Iterator[WorkflowEvent]:
        """Every event from the cursor on, ending after the run's last one. The record is read
        BEFORE the events each round, so a terminal record never leaves an event behind."""
        for _ in self.polls(timeout):
            yield from self.events()

    def wait(self, timeout: float | None = None) -> Self:
        """Until the run ends."""
        for _ in self.polls(timeout):
            pass
        return self

    def cancel(self) -> Self:
        self.record = self.server.cancel_run(self.record.id)
        return self

    def download(self, output: str | ArtifactRef, destination: Path | str = ".", *, overwrite: bool = False) -> Path:
        """Saves one file output (by output name or record) — into a directory under its own
        relative path — after checking its size and sha256 against the run's record."""
        return self.server.download_artifact(self.artifact(output), Path(destination), overwrite=overwrite)


class Workflows(ServerBound):
    """The connected workspace's workflows, addressed by exact name or id."""

    #: Starting is idempotent on its key, so an answer lost in transit is asked again with the
    #: SAME key — the server hands back the one run it already started, never a second one.
    start_attempts: int = Field(default=3, ge=1)

    def list(self) -> tuple[WorkflowRevision, ...]:
        return self.read(self.server.workflows)

    def get(self, workflow: str | UUID) -> WorkflowRevision:
        """The current revision of the workflow with this id or exact name."""
        heads = self.list()
        matches = [head for head in heads if str(head.key.id) == str(workflow) or head.name == workflow]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise WorkflowNotFound(f"{len(matches)} workflows are named {workflow!r}; pass an id: " + ", ".join(str(head.key.id) for head in matches))
        raise WorkflowNotFound(f"no workflow named or identified {workflow!r} among the {len(heads)} of the connected workspace")

    def start(self, workflow: str | UUID | WorkflowRevision, inputs: dict[str, object] | None = None, *, idempotency_key: str | None = None) -> Run:
        """Starts the current revision and returns once the server accepted it. `inputs` map to
        the workflow's exposed inputs and variables by name. Reusing `idempotency_key` returns
        the run that key already started."""
        revision = workflow if isinstance(workflow, WorkflowRevision) else self.get(workflow)
        request = WorkflowRunRequest(workflow=WorkflowRevisionRef(key=revision.key, revision=revision.revision),
                                     idempotency_key=idempotency_key or f"sdk:{uuid4()}", invocation=TriggerInvocation(values=inputs or {}))
        for attempt in range(1, self.start_attempts + 1):
            try:
                record = self.server.run_workflow(request)
                break
            except WorkflowRunUncertain:
                if attempt == self.start_attempts:
                    raise
                time.sleep(self.poll_seconds * attempt)
        if record.workflow.revision != revision.revision:
            revision = self.read(self.server.workflow_revision, record.workflow)
        return Run(server=self.server, poll_seconds=self.poll_seconds, read_attempts=self.read_attempts, workflow=revision, record=record)

    def run(self, workflow: str | UUID | WorkflowRevision, inputs: dict[str, object] | None = None, *, timeout: float | None = None, idempotency_key: str | None = None) -> Run:
        """Starts the workflow and blocks until it ends; read `.outputs` to continue."""
        return self.start(workflow, inputs, idempotency_key=idempotency_key).wait(timeout)

    def attach(self, run_id: UUID | str) -> Run:
        """A run started elsewhere (another script, `--detach`, the web app), with the exact
        revision it runs."""
        record = self.read(self.server.workflow_run, UUID(str(run_id)))
        return Run(server=self.server, poll_seconds=self.poll_seconds, read_attempts=self.read_attempts, workflow=self.read(self.server.workflow_revision, record.workflow), record=record)


class AsyncRun(RunState):
    """`Run` for asyncio code: the same state and methods, each server call off the event loop."""

    async def refresh(self) -> Self:
        self.record = await asyncio.to_thread(self.read, self.server.workflow_run, self.record.id)
        return self

    async def events(self) -> tuple[WorkflowEvent, ...]:
        return self.follow(await asyncio.to_thread(self.read, self.server.workflow_events, self.record.id, self.cursor))

    async def polls(self, timeout: float | None = None) -> AsyncIterator[Self]:
        deadline = self.deadline(timeout)
        while True:
            yield await self.refresh()
            if (pause := self.pause(deadline)) is None:
                return
            await asyncio.sleep(pause)

    async def stream(self, timeout: float | None = None) -> AsyncIterator[WorkflowEvent]:
        async for _ in self.polls(timeout):
            for event in await self.events():
                yield event

    async def wait(self, timeout: float | None = None) -> Self:
        async for _ in self.polls(timeout):
            pass
        return self

    async def cancel(self) -> Self:
        self.record = await asyncio.to_thread(self.server.cancel_run, self.record.id)
        return self

    async def download(self, output: str | ArtifactRef, destination: Path | str = ".", *, overwrite: bool = False) -> Path:
        return await asyncio.to_thread(lambda: self.server.download_artifact(self.artifact(output), Path(destination), overwrite=overwrite))


class AsyncWorkflows(BaseModel):
    """`Workflows` for asyncio code."""

    workflows: Workflows

    async def list(self) -> tuple[WorkflowRevision, ...]:
        return await asyncio.to_thread(self.workflows.list)

    async def get(self, workflow: str | UUID) -> WorkflowRevision:
        return await asyncio.to_thread(self.workflows.get, workflow)

    async def start(self, workflow: str | UUID | WorkflowRevision, inputs: dict[str, object] | None = None, *, idempotency_key: str | None = None) -> AsyncRun:
        return (await asyncio.to_thread(lambda: self.workflows.start(workflow, inputs, idempotency_key=idempotency_key))).to(AsyncRun)

    async def run(self, workflow: str | UUID | WorkflowRevision, inputs: dict[str, object] | None = None, *, timeout: float | None = None, idempotency_key: str | None = None) -> AsyncRun:
        return await (await self.start(workflow, inputs, idempotency_key=idempotency_key)).wait(timeout)

    async def attach(self, run_id: UUID | str) -> AsyncRun:
        return (await asyncio.to_thread(self.workflows.attach, run_id)).to(AsyncRun)
