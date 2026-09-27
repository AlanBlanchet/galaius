"""Typed JSON bridge for native workspace controls; the server owns every record."""

import json
from typing import Literal
from uuid import UUID

import httpx
from cyclopts import App
from pydantic import ValidationError

from interact.agents.catalog_connection import CatalogAuthenticationError, CatalogConnection, CatalogConnectionError
from interact.server_workspace import AgentEdit, ServerWorkspace, WorkflowConflictError, WorkflowRunRejected, WorkflowRunRequest, WorkflowRunUncertain, WorkspaceConflictError

workspace_app = App(name="workspace", help="Inspect and edit the connected server workspace.")


class WorkspaceCLI:
    @staticmethod
    def emit(operation):
        try:
            value = operation()
            print(json.dumps({"ok": True, **value}))
        except (ValueError, OSError, httpx.HTTPError) as error:
            code = "workflow_conflict" if isinstance(error, WorkflowConflictError) else "run_rejected" if isinstance(error, WorkflowRunRejected) else "run_uncertain" if isinstance(error, WorkflowRunUncertain) else "conflict" if isinstance(error, WorkspaceConflictError) else "authorization" if isinstance(error, CatalogAuthenticationError) else "unavailable"
            message = str(error) if isinstance(error, CatalogConnectionError) else "Invalid workspace input." if isinstance(error, (ValueError, ValidationError)) else "Cannot reach server workspace; no offline writes."
            print(json.dumps({"ok": False, "code": code, "message": message}))
            raise SystemExit(1) from None

    @staticmethod
    def view(server: ServerWorkspace, graph):
        return {"configured": True, "origin": server.connection.endpoint, "workspace_id": str(server.connection.workspace_id),
                "state": "current", "writable": server.can_edit(), "graph": graph.model_dump(mode="json")}

    @staticmethod
    def status():
        connection = CatalogConnection.load()
        if connection is None:
            return {"configured": False, "state": "unconfigured"}
        server = ServerWorkspace(connection=connection)
        return WorkspaceCLI.view(server, server.graph())

    @staticmethod
    def edit(expected_revision: str, agent_id: UUID, edit_json: str | None):
        server = ServerWorkspace.configured()
        graph = server.graph()
        if graph.revision != expected_revision:
            raise WorkspaceConflictError("Server graph changed. Draft retained; reload before retrying.")
        saved = server.set_root(graph, agent_id) if edit_json is None else server.edit_agent(graph, agent_id, AgentEdit.model_validate_json(edit_json))
        return WorkspaceCLI.view(server, saved)

    @staticmethod
    def run(workflow_id: UUID, revision: UUID, idempotency_key: str, invocation_json: str):
        request = WorkflowRunRequest(workflow={"key": {"id": workflow_id}, "revision": revision},
                                     idempotency_key=idempotency_key, invocation=json.loads(invocation_json))
        return {"run": ServerWorkspace.configured().run_workflow(request).model_dump(mode="json")}

    @staticmethod
    def run_status(workflow_id: UUID, idempotency_key: str):
        run = ServerWorkspace.configured().workflow_status(workflow_id, idempotency_key)
        return {"run": run.model_dump(mode="json") if run else None}


@workspace_app.command
def status(*, json_out: bool = False):
    """Current server graph and safe connection metadata (always structured JSON)."""
    WorkspaceCLI.emit(WorkspaceCLI.status)


@workspace_app.command
def graph(*, json_out: bool = False):
    """Read the current graph; no local fallback."""
    WorkspaceCLI.emit(WorkspaceCLI.status)


@workspace_app.command
def catalog(*, json_out: bool = False):
    """Read and verify the complete current agent catalog."""
    WorkspaceCLI.emit(lambda: {"catalog": ServerWorkspace.configured().agent_catalog().model_dump(mode="json")})


@workspace_app.command(name="agent-edit")
def agent_edit(agent_id: UUID, *, expected_revision: str, edit_json: str, json_out: bool = False):
    """Save criteria, reasoning or reporting parent against the displayed graph revision."""
    WorkspaceCLI.emit(lambda: WorkspaceCLI.edit(expected_revision, agent_id, edit_json))


@workspace_app.command(name="root-set")
def root_set(agent_id: UUID, *, expected_revision: str, json_out: bool = False):
    """Bind the Assistant root and remove its reporting parent atomically."""
    WorkspaceCLI.emit(lambda: WorkspaceCLI.edit(expected_revision, agent_id, None))


@workspace_app.command
def workflows(*, json_out: bool = False):
    """List exact current workflow revisions; run them in the permission-aware server editor."""
    WorkspaceCLI.emit(lambda: {"workflows": [workflow.model_dump(mode="json") for workflow in ServerWorkspace.configured().workflows()]})


@workspace_app.command
def company(*, json_out: bool = False):
    """Read the current company profile."""
    WorkspaceCLI.emit(lambda: {"company": ServerWorkspace.configured().company().model_dump(mode="json")})


@workspace_app.command
def models(*, json_out: bool = False):
    """Read exact configured model and provider connection references."""
    WorkspaceCLI.emit(lambda: {"models": [model.model_dump(mode="json") for model in ServerWorkspace.configured().models()]})


@workspace_app.command(name="workflow-history")
def workflow_history(workflow_id: UUID, *, json_out: bool = False):
    """Read real execution state for a server workflow."""
    WorkspaceCLI.emit(lambda: {"runs": [run.model_dump(mode="json") for run in ServerWorkspace.configured().workflow_history(workflow_id)]})


@workspace_app.command(name="workflow-run")
def workflow_run(workflow_id: UUID, *, revision: UUID, idempotency_key: str, invocation_json: str = "{}", json_out: bool = False):
    """Run the selected immutable revision with an explicit retry-safe request key."""
    WorkspaceCLI.emit(lambda: WorkspaceCLI.run(workflow_id, revision, idempotency_key, invocation_json))


@workspace_app.command(name="workflow-status")
def workflow_status(workflow_id: UUID, *, idempotency_key: str, json_out: bool = False):
    """Find the real execution state after submission or an uncertain response."""
    WorkspaceCLI.emit(lambda: WorkspaceCLI.run_status(workflow_id, idempotency_key))


@workspace_app.command
def version(*, json_out: bool = False):
    """Read the server release and changelog."""
    WorkspaceCLI.emit(lambda: {"version": ServerWorkspace.configured().version().model_dump(mode="json")})


@workspace_app.command
def link(view: Literal["agents", "workflows", "company", "personal", "connections", "prompts", "version", "assistant"], *, identity: UUID | None = None, json_out: bool = False):
    """Return an existing server application route; never copy session credentials."""
    WorkspaceCLI.emit(lambda: {"url": ServerWorkspace.configured().link(view, identity)})
