"""Workspace records through the existing authenticated catalog session."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4

import httpx
from interact_core import AgentGraph, AgentGraphUpdate, AgentRevision, AgentRevisionRef, AgentStartSpec, ArtifactRef, CompanyProfile, ConfiguredModelRef, MachineSummary, ReleaseInfo, TriggerInvocation, WorkflowEvent, WorkflowRevision, WorkflowRevisionRef, WorkflowRun
from interact_core.accounts import Bootstrap
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from interact.agents.catalog import CatalogSnapshot
from interact.agents.catalog_connection import CatalogAuthenticationError, CatalogConnection, CatalogConnectionError
from interact.criteria import Criteria
from interact.server_prompts import ServerPrompts


class WorkspaceConflictError(CatalogConnectionError):
    """The displayed graph is no longer current; keep the unsaved draft."""


class WorkflowConflictError(CatalogConnectionError):
    """The selected workflow revision changed before execution."""


class WorkflowRunRejected(CatalogConnectionError):
    """This attempt was rejected before dispatch; prior attempts remain independent."""


class WorkflowRunUncertain(CatalogConnectionError):
    """A request may have reached execution; inspect it using the retained key."""


class WorkflowRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workflow: WorkflowRevisionRef
    idempotency_key: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    invocation: TriggerInvocation = Field(default_factory=TriggerInvocation)


class AgentEdit(BaseModel):
    """Only editable fields, with omitted fields preserving server values."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    reports_to: UUID | None = None
    criteria: str | None = None
    criteria_weights: str = ""
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"] = "high"
    model: ConfiguredModelRef | None = None

    def apply(self, current: AgentRevision):
        changes = self.model_dump(exclude_unset=True)
        if "criteria" in changes and self.criteria:
            Criteria.parse(self.criteria)
        if "criteria_weights" in changes:
            Criteria.validate_weights(self.criteria_weights)
        return AgentRevision.model_validate({
            **current.model_dump(), **changes, "revision": uuid4(),
            "parent_revision": current.revision, "created_at": datetime.now(UTC),
        })


class RemoteRunLine(BaseModel):
    """One line of a run on another computer, as its page shows it (said / final / step / error ...)."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    kind: str
    text: str = ""
    at: float = 0.0


class RemoteRunEvents(BaseModel):
    """A window of a run on another computer: its lines after a cursor, and where the next starts."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    run_id: UUID
    cursor: int | None = None
    items: tuple[RemoteRunLine, ...] = ()


class ServerWorkspace(ServerPrompts):
    """No separate credentials, offline authoring, or caller-selected HTTP paths."""

    @classmethod
    def configured(cls):
        connection = CatalogConnection.load()
        if connection is None:
            raise CatalogConnectionError("No server workspace configured. Use interact agents sync to connect.")
        return cls(connection=connection)

    @property
    def workspace_endpoint(self):
        if self.connection.workspace_id is None:
            raise CatalogConnectionError("Select a server workspace first.")
        return f"/v1/workspaces/{self.connection.workspace_id}"

    def read_record(self, suffix: Literal["agent-graph", "agent-catalog", "workflows", "company", "version", "models"], schema, *, transport=None):
        path = "/v1/version" if suffix == "version" else f"{self.workspace_endpoint}/{suffix}"
        with self.session(transport=transport) as client:
            payload = self.connection.request(client, "GET", path)
            try:
                return TypeAdapter(schema).validate_json(payload)
            except ValueError as error:
                raise CatalogConnectionError(f"Invalid server {suffix} response.") from error

    def graph(self, *, transport=None):
        return self.read_record("agent-graph", AgentGraph, transport=transport)

    def agent_catalog(self, *, transport=None):
        return self.read_record("agent-catalog", CatalogSnapshot, transport=transport)

    def workflows(self, *, transport=None):
        return self.read_record("workflows", tuple[WorkflowRevision, ...], transport=transport)

    def company(self, *, transport=None):
        return self.read_record("company", CompanyProfile, transport=transport)

    def version(self, *, transport=None):
        return self.read_record("version", ReleaseInfo, transport=transport)

    def models(self, *, transport=None):
        return self.read_record("models", tuple[ConfiguredModelRef, ...], transport=transport)

    def can_edit(self, *, transport=None):
        if self.connection.auth_mode == "token":
            return False
        with self.session(transport=transport) as client:
            bootstrap = Bootstrap.model_validate_json(self.connection.request(client, "GET", "/v1/bootstrap"))
            return any(workspace.workspace_id == self.connection.workspace_id and workspace.role != "viewer" for workspace in bootstrap.workspaces)

    def agent_revision(self, reference: AgentRevisionRef, *, transport=None):
        with self.session(transport=transport) as client:
            payload = self.connection.request(client, "GET", f"{self.workspace_endpoint}/agents/{reference.id}/revisions/{reference.revision}")
            value = AgentRevision.model_validate_json(payload)
            if value.id != reference.id or value.revision != reference.revision:
                raise CatalogConnectionError("Server returned a different agent revision.")
            return value

    def workflow_history(self, workflow_id: UUID, *, transport=None):
        with self.session(transport=transport) as client:
            payload = self.connection.request(client, "GET", f"{self.workspace_endpoint}/workflows/{workflow_id}/runs")
            values = TypeAdapter(tuple[WorkflowRun, ...]).validate_json(payload)
            if any(value.workflow.key.id != workflow_id for value in values):
                raise CatalogConnectionError("Server returned another workflow's history.")
            return values

    def workflow_status(self, workflow_id: UUID, idempotency_key: str, *, transport=None):
        return next((run for run in self.workflow_history(workflow_id, transport=transport) if run.idempotency_key == idempotency_key), None)

    def workflow_revision(self, reference: WorkflowRevisionRef, *, transport=None):
        """The exact revision a run names, even after the workflow moved on."""
        with self.session(transport=transport) as client:
            value = WorkflowRevision.model_validate_json(self.connection.request(client, "GET", f"{self.workspace_endpoint}/workflows/{reference.key.id}/revisions/{reference.revision}"))
        if value.key.id != reference.key.id or value.revision != reference.revision:
            raise CatalogConnectionError("Server returned a different workflow revision.")
        return value

    def workflow_run(self, run_id: UUID, *, transport=None):
        with self.session(transport=transport) as client:
            value = WorkflowRun.model_validate_json(self.connection.request(client, "GET", f"{self.workspace_endpoint}/runs/{run_id}"))
        if value.id != run_id:
            raise CatalogConnectionError("Server returned another run.")
        return value

    def workflow_events(self, run_id: UUID, after: int = 0, *, transport=None):
        """Events of one run with a sequence above `after`, in order."""
        with self.session(transport=transport) as client:
            values = TypeAdapter(tuple[WorkflowEvent, ...]).validate_json(self.connection.request(client, "GET", f"{self.workspace_endpoint}/runs/{run_id}/events?after={after}"))
        if any(value.run_id != run_id or value.sequence <= after for value in values):
            raise CatalogConnectionError("Server returned events of another run or before the cursor.")
        return values

    def execute_headers(self, client: httpx.Client) -> dict[str, str]:
        """A cookie session proves execute rights and carries its CSRF token; a token session uses
        only its existing server-granted scope — the endpoint authorizes every call either way."""
        if self.connection.auth_mode != "preview":
            return {}
        bootstrap = Bootstrap.model_validate_json(self.connection.request(client, "GET", "/v1/bootstrap"))
        if not any(workspace.workspace_id == self.connection.workspace_id and workspace.role != "viewer" for workspace in bootstrap.workspaces):
            raise CatalogAuthenticationError("Workspace execution permission is required; request retained.")
        return {"x-csrf-token": bootstrap.csrf_token}

    def cancel_run(self, run_id: UUID, *, transport=None):
        """Cancelling is idempotent: when the answer is slow (the server is busy with the very run
        being cancelled), the run's own record says whether it took."""
        with self.session(transport=transport) as client:
            try:
                response = client.post(f"{self.workspace_endpoint}/runs/{run_id}/cancel", headers=self.execute_headers(client), timeout=30)
            except httpx.TimeoutException:
                run = self.workflow_run(run_id, transport=transport)
                if run.status in {"cancelling", "cancelled"}:
                    return run
                raise
            if response.status_code == 404:
                raise CatalogConnectionError(f"No run {run_id} in this workspace.")
            if response.status_code in {401, 403}:
                raise CatalogAuthenticationError(f"Cancel refused (HTTP {response.status_code}).")
            if not response.is_success:
                raise CatalogConnectionError(f"Cancel failed (HTTP {response.status_code}).")
            return WorkflowRun.model_validate_json(response.content)

    def download_artifact(self, artifact: ArtifactRef, destination: Path, *, overwrite: bool = False, transport=None) -> Path:
        """Streams one workflow file to `destination` — into a directory under the file's own
        relative path, never outside it — and keeps it only if its size and sha256 match the
        run's record. A file already there with the same content is the answer; with other
        content it is kept unless `overwrite`."""
        if destination.is_dir():
            root = destination.resolve()
            target = (root / artifact.path).resolve()
            if not target.is_relative_to(root) or target == root:
                raise CatalogConnectionError(f"File path {artifact.path!r} leaves the download directory; nothing saved.")
        else:
            target = destination
        if target.is_file() and not overwrite:
            if target.stat().st_size == artifact.size and hashlib.sha256(target.read_bytes()).hexdigest() == artifact.digest:
                return target
            raise CatalogConnectionError(f"{target} already exists with other content; nothing overwritten.")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.partial")
        digest, size = hashlib.sha256(), 0
        try:
            with self.session(transport=transport) as client, client.stream("GET", f"{self.workspace_endpoint}/connections/{artifact.connection.id}/{artifact.connection.revision}/artifacts/{quote(artifact.path)}") as response:
                if response.status_code == 404:
                    raise CatalogConnectionError(f"File {artifact.path} is no longer in its storage.")
                if response.status_code in {401, 403}:
                    raise CatalogAuthenticationError(f"File {artifact.path} refused (HTTP {response.status_code}).")
                if not response.is_success:
                    raise CatalogConnectionError(f"File download failed (HTTP {response.status_code}).")
                with partial.open("wb") as output:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > artifact.size:
                            break
                        digest.update(chunk)
                        output.write(chunk)
            if size != artifact.size or digest.hexdigest() != artifact.digest:
                raise CatalogConnectionError(f"File {artifact.path} does not match its recorded size and digest; nothing kept.")
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return target

    def run_workflow(self, request: WorkflowRunRequest, *, transport=None):
        """Start only the displayed revision and answer once the server accepted it (the run goes
        on server-side: follow it with `workflow_run` / `workflow_events`); retain the same key
        after uncertain delivery."""
        with self.session(transport=transport) as client:
            headers = {"Content-Type": "application/json", "If-Match": f'"{request.workflow.revision}"',
                       "Idempotency-Key": request.idempotency_key, "Prefer": "respond-async", **self.execute_headers(client)}
            try:
                with client.stream("POST", f"{self.workspace_endpoint}/workflows/{request.workflow.key.id}/runs",
                                   content=request.invocation.model_dump_json(), headers=headers) as response:
                    if response.status_code in {401, 403, 404}:
                        self.connection.invalidate_access(client)
                        raise CatalogAuthenticationError(f"Workflow execution refused (HTTP {response.status_code}); request retained. Existing execute permission is required.")
                    if response.status_code == 409:
                        raise WorkflowConflictError("Selected workflow revision changed; nothing dispatched by this request. Reload workflows and review the new revision.")
                    if response.status_code == 422:
                        raise WorkflowRunRejected("This idempotency key already started a run of another workflow or with other inputs; use a new key.")
                    if response.status_code in (400, 413):
                        raise WorkflowRunRejected("Workflow inputs rejected before dispatch. Correct input names and values, choose New execution, then submit explicitly.")
                    if response.status_code >= 500:
                        raise WorkflowRunUncertain("Server execution response failed. Check execution status with the same request key before retrying.")
                    if not 200 <= response.status_code < 300:
                        raise CatalogConnectionError(f"Workflow request rejected (HTTP {response.status_code}); inputs and request key retained.")
                    payload = bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 16 << 20:
                            raise WorkflowRunUncertain("Run response exceeds the limit. Check execution status with the same request key.")
            except httpx.HTTPError as error:
                raise WorkflowRunUncertain("Execution response unavailable; work may still be running. Check status with the same request key; do not create another execution.") from error
            try:
                run = WorkflowRun.model_validate_json(payload)
            except ValueError as error:
                raise WorkflowRunUncertain("Invalid execution response. Check status with the same request key.") from error
            # A replayed key returns the run it started, on the revision it started with — which
            # may be older than the one this request named.
            if run.workflow.key != request.workflow.key or run.idempotency_key != request.idempotency_key or run.invocation != request.invocation:
                raise WorkflowRunUncertain("Execution response differs from the selected workflow or inputs. Check status with the same request key.")
            return run

    def script_approvals(self, machine_id: UUID, *, transport=None) -> tuple[str, ...]:
        """The script digests the machine's owner approved on `machine_id`."""
        with self.session(transport=transport) as client:
            payload = self.connection.request(client, "GET", f"{self.workspace_endpoint}/machines/{machine_id}/script-approvals")
        try:
            return tuple(str(item["source_digest"]) for item in json.loads(payload))
        except (ValueError, KeyError, TypeError) as error:
            raise CatalogConnectionError("Invalid server script approvals response.") from error

    def approve_script(self, machine_id: UUID, source_digest: str, *, transport=None) -> None:
        """The client-side half of threat-modeler mitigation #1: only the machine OWNER (never
        workspace write-scope) may allowlist a script's exact content digest -- same bootstrap +
        CSRF handshake `save_graph` already uses, hitting the owner-gated approval endpoint."""
        if self.connection.auth_mode == "token":
            raise CatalogAuthenticationError("This connection is read-only. Approve from the signed-in server workspace.")
        with self.session(transport=transport) as client:
            bootstrap = Bootstrap.model_validate_json(self.connection.request(client, "GET", "/v1/bootstrap"))
            if not any(workspace.workspace_id == self.connection.workspace_id and workspace.role == "owner" for workspace in bootstrap.workspaces):
                raise CatalogAuthenticationError("Only the workspace owner may approve a script.")
            with client.stream("POST", f"{self.workspace_endpoint}/machines/{machine_id}/script-approvals",
                               content=json.dumps({"source_digest": source_digest}),
                               headers={"Content-Type": "application/json", "x-csrf-token": bootstrap.csrf_token}) as response:
                if response.status_code in {401, 403, 404}:
                    self.connection.invalidate_access(client)
                    raise CatalogAuthenticationError(f"Script approval refused (HTTP {response.status_code}).")
                if not 200 <= response.status_code < 300:
                    raise CatalogConnectionError(f"Script approval rejected (HTTP {response.status_code}).")

    def owner_call(self, method: Literal["GET", "POST"], path: str, body: BaseModel | None = None, *, timeout: float = 150, transport=None) -> bytes:
        """One call a computer's OWNER makes on the workspace (`path` below it): a signed-in session
        with its CSRF token, never the read-only key; a refusal raises with the server's own words."""
        if self.connection.auth_mode == "token":
            raise CatalogAuthenticationError("This connection is a read-only key: act on a computer from a signed-in session (interact login on the owner's computer).")
        with self.session(transport=transport) as client:
            headers = {} if method == "GET" else {"Content-Type": "application/json", "x-csrf-token": Bootstrap.model_validate_json(self.connection.request(client, "GET", "/v1/bootstrap")).csrf_token}
            response = client.request(method, f"{self.workspace_endpoint}{path}", content=body.model_dump_json() if body is not None else None, headers=headers, timeout=timeout)
            if not response.is_success:
                try:
                    said = response.json()
                    reason = said.get("message") or said.get("detail") or said.get("code")
                except (ValueError, AttributeError):
                    reason = None
                raise CatalogConnectionError(f"{reason or 'refused'} (HTTP {response.status_code})")
            return response.content

    def machine(self, which: str, *, transport=None) -> MachineSummary:
        """The connected (not revoked) computer named `which`, or with that id."""
        machines = TypeAdapter(tuple[MachineSummary, ...]).validate_json(self.owner_call("GET", "/machines", transport=transport))
        found = [machine for machine in machines if machine.state != "revoked" and (str(machine.id) == which or machine.name == which)]
        if len(found) != 1:
            names = ", ".join(sorted(machine.name for machine in machines if machine.state != "revoked"))
            raise CatalogConnectionError(f"{'no' if not found else 'more than one'} computer named {which!r} (connected: {names or 'none'})")
        return found[0]

    def start_on_machine(self, machine: MachineSummary, folder: str, spec: dict[str, object], *, transport=None) -> UUID:
        """Starts an agent on `machine` in `folder` ("<agent folder>/<path beneath it>"): the agent
        folder is the longest of the computer's own that `folder` starts with."""
        roots = json.loads(self.owner_call("GET", f"/machines/{machine.id}/agents/folders", transport=transport)).get("roots", [])
        parts = folder.strip("/").split("/")
        root = next((candidate for size in range(len(parts), 0, -1) if (candidate := "/".join(parts[:size])) in roots), None)
        if root is None:
            raise CatalogConnectionError(f"{folder!r} is not inside an agent folder of {machine.name} (its agent folders: {', '.join(roots) or 'none'})")
        start = AgentStartSpec(root=root, path="/".join(parts[len(root.split("/")):]), **spec)
        return UUID(json.loads(self.owner_call("POST", f"/machines/{machine.id}/agents", start, transport=transport))["run_id"])

    def machine_run_events(self, machine: MachineSummary, run_id: UUID, cursor: int | None = None, *, transport=None) -> RemoteRunEvents:
        query = f"?cursor={cursor}" if cursor is not None else ""
        return RemoteRunEvents.model_validate_json(self.owner_call("GET", f"/machines/{machine.id}/agents/{run_id}/events{query}", transport=transport))

    def link(self, view: Literal["agents", "workflows", "company", "personal", "connections", "prompts", "version", "assistant"], identity: UUID | None = None):
        query = urlencode({"agent" if view == "agents" else "workflow": str(identity)}) if identity and view in {"agents", "workflows"} else ""
        return f"{self.connection.endpoint.rstrip('/')}/#{view}{'?' + query if query else ''}"

    def save_graph(self, update: AgentGraphUpdate, *, transport=None):
        if self.connection.auth_mode == "token":
            raise CatalogAuthenticationError("This connection is read-only. Edit in the signed-in server workspace; draft retained.")
        with self.session(transport=transport) as client:
            bootstrap = Bootstrap.model_validate_json(self.connection.request(client, "GET", "/v1/bootstrap"))
            if not any(workspace.workspace_id == self.connection.workspace_id and workspace.role != "viewer" for workspace in bootstrap.workspaces):
                raise CatalogAuthenticationError("Workspace edit permission is required; draft retained.")
            with client.stream("PUT", f"{self.workspace_endpoint}/agent-graph",
                               content=update.model_dump_json(exclude_unset=True),
                               headers={"Content-Type": "application/json", "x-csrf-token": bootstrap.csrf_token}) as response:
                if response.status_code in {401, 403, 404}:
                    self.connection.invalidate_access(client)
                    raise CatalogAuthenticationError(f"Server edit refused (HTTP {response.status_code}). Sign in with workspace edit permission; draft retained.")
                if response.status_code == 409:
                    raise WorkspaceConflictError("Server graph changed. Draft retained; reload current graph before retrying.")
                if not 200 <= response.status_code < 300:
                    raise CatalogConnectionError(f"Server rejected graph (HTTP {response.status_code}); draft retained. Check parent, criteria and provider capabilities.")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 16 << 20:
                        raise CatalogConnectionError("Server graph exceeds response limit; refresh to verify save.")
            try:
                saved = AgentGraph.model_validate_json(payload)
            except ValueError as error:
                raise CatalogConnectionError("Invalid saved graph; draft retained. Refresh to verify save.") from error
            heads = {agent.id: agent for agent in saved.agents}
            for proposed in update.agents:
                actual = heads.get(proposed.id)
                if actual is None or any(getattr(actual, field) != getattr(proposed, field) for field in AgentEdit.model_fields):
                    raise CatalogConnectionError("Save response differs from requested agent edit; draft retained. Refresh to verify save.")
            if "root_agent" in update.model_fields_set and (saved.root_agent.id if saved.root_agent else None) != update.root_agent:
                raise CatalogConnectionError("Save response differs from requested Assistant root; draft retained.")
            return saved

    def edit_agent(self, graph: AgentGraph, agent_id: UUID, edit: AgentEdit, *, transport=None):
        current = next((agent for agent in graph.agents if agent.id == agent_id), None)
        if current is None:
            raise ValueError("Agent is absent from the displayed graph.")
        changed = edit.apply(current)
        return self.save_graph(AgentGraphUpdate(expected_revision=graph.revision, agents=(changed,)), transport=transport)

    def set_root(self, graph: AgentGraph, agent_id: UUID | None, *, transport=None):
        changed = ()
        if agent_id is not None:
            current = next((agent for agent in graph.agents if agent.id == agent_id), None)
            if current is None:
                raise ValueError("Root is absent from the displayed graph.")
            if current.reports_to is not None:
                changed = (AgentEdit(reports_to=None).apply(current),)
        return self.save_graph(AgentGraphUpdate(expected_revision=graph.revision, root_agent=agent_id, agents=changed), transport=transport)
