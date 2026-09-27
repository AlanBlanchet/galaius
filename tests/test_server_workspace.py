"""Native controls use authenticated server records and preserve conflicts as drafts."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from interact_core import AgentGraph, AgentGraphUpdate, AgentRevision, AgentRevisionRef, ConfiguredModelRef, PromptExecutionRef, PromptKey, TriggerInvocation, WorkflowRevision, WorkflowRun
from interact_core.accounts import Account, Bootstrap, Workspace
from textual.widgets import Button, Input, Select, Static, TabbedContent, Tree

from interact import USER_AGENT
from interact.agents.catalog_connection import CatalogAuthenticationError, CatalogConnection
from interact.cli.app import app as cli
from interact.cli.tui import InteractTUI, WorkspacePane
from interact.config import UserConfig
from interact.server_workspace import AgentEdit, ServerWorkspace, WorkflowConflictError, WorkflowRunRejected, WorkflowRunRequest, WorkflowRunUncertain, WorkspaceConflictError


@pytest.fixture
def workspace_server(tmp_path, monkeypatch):
    monkeypatch.setattr(UserConfig, "PATH", tmp_path / "config.env")
    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode="preview", workspace_id=uuid4())
    connection.save()
    prompt = PromptExecutionRef(key=PromptKey(namespace="fixture", slug="primary"), revision=uuid4(), channel="stable", digest="a" * 64)
    root = AgentRevision(id=uuid4(), revision=uuid4(), name="Root", role_key="root", prompt=prompt,
                         criteria="price.in >= 0", resources=(), created_at=datetime.now(UTC))
    child = root.model_copy(update={"id": uuid4(), "revision": uuid4(), "name": "Child", "role_key": "child", "reports_to": root.id})
    graph = AgentGraph(revision="a" * 64, root_agent=AgentRevisionRef(id=root.id, revision=root.revision), agents=(root, child))
    bootstrap = Bootstrap(account=Account(account_id=uuid4(), email="fixture@example.invalid", locale="en", verified=True),
                          workspaces=(Workspace(workspace_id=connection.workspace_id, name="Fixture", role="owner"),),
                          current_workspace_id=connection.workspace_id, csrf_token="synthetic-csrf",
                          session_expires_at=datetime.now(UTC) + timedelta(hours=1))
    state = {"graph": graph, "requests": [], "failure": None, "connection": connection, "bootstrap": bootstrap,
             "workflow_id": uuid4(), "workflow_revision": uuid4(), "runs": {}, "token": uuid4().hex}
    workflow = WorkflowRevision(key={"id": state["workflow_id"]}, revision=state["workflow_revision"], name="Fixture workflow",
        nodes=({"id": uuid4(), "label": "Value", "x": 0, "y": 0, "impl": {"kind": "builtin", "op": "input"}, "config": {"value": "fixture"},
                "ports": ({"name": "value", "direction": "output", "value_type": "text"},)},),
        edges=(), interface={}, created_at=datetime.now(UTC))

    def respond(request):
        state["requests"].append(request)
        if state["failure"] == "offline":
            raise httpx.ConnectError("offline", request=request)
        if request.url.path == "/v1/auth/local-preview":
            return httpx.Response(200, headers={"Set-Cookie": "session=synthetic; Path=/"}, json={})
        token = "Authorization" in request.headers
        if token:
            assert request.headers["Authorization"] == f"Bearer {state['token']}"
            assert "Cookie" not in request.headers
        else:
            assert request.headers["Cookie"] == "session=synthetic"
        if request.url.path == "/v1/bootstrap":
            return httpx.Response(200, content=state["bootstrap"].model_dump_json())
        if request.url.path == "/v1/account/tool-settings":
            return httpx.Response(200, json={"schema_version": 1, "revision": 0, "values": {}})
        if request.url.path == f"/v1/workspaces/{connection.workspace_id}/workflows/{state['workflow_id']}/runs":
            if request.method == "GET":
                return httpx.Response(200, json=[run.model_dump(mode="json") for run in state["runs"].values()])
            assert request.method == "POST"
            if token:
                assert "x-csrf-token" not in request.headers
            else:
                assert request.headers["x-csrf-token"] == bootstrap.csrf_token
            if isinstance(state["failure"], int):
                return httpx.Response(state["failure"])
            if request.headers["If-Match"] != f'"{state["workflow_revision"]}"':
                return httpx.Response(409, json={"code": "workflow_conflict"})
            key = request.headers["Idempotency-Key"]
            invocation = TriggerInvocation.model_validate_json(request.content)
            if "invalid" in invocation.values:
                return httpx.Response(400, json={"code": "invalid_request"})
            if key not in state["runs"]:
                state["runs"][key] = WorkflowRun(id=uuid4(), workflow={"key": {"id": state["workflow_id"]}, "revision": state["workflow_revision"]},
                    status="running", idempotency_key=key, invocation=invocation, created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
            if state["failure"] == "run-timeout":
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return httpx.Response(201, content=state["runs"][key].model_dump_json())
        if request.url.path.endswith("/workflows"):
            return httpx.Response(200, json=[workflow.model_copy(update={"revision": state["workflow_revision"]}).model_dump(mode="json")])
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=[])
        assert request.url.path == f"/v1/workspaces/{connection.workspace_id}/agent-graph"
        if request.method == "GET":
            return httpx.Response(200, content=state["graph"].model_dump_json())
        assert request.method == "PUT"
        assert request.headers["x-csrf-token"] == bootstrap.csrf_token
        assert request.headers["Origin"] == connection.endpoint
        if isinstance(state["failure"], int):
            return httpx.Response(state["failure"])
        if state["failure"] == "wrong-save":
            return httpx.Response(200, content=state["graph"].model_dump_json())
        update = AgentGraphUpdate.model_validate_json(request.content)
        if update.expected_revision != state["graph"].revision:
            return httpx.Response(409)
        heads = {agent.id: agent for agent in state["graph"].agents}
        for agent in update.agents:
            assert agent.parent_revision == heads[agent.id].revision
            assert agent.revision != heads[agent.id].revision
            heads[agent.id] = agent
        root_id = update.root_agent if "root_agent" in update.model_fields_set else state["graph"].root_agent.id
        state["graph"] = AgentGraph(revision=hashlib.sha256(request.content).hexdigest(), agents=tuple(heads.values()),
                                    root_agent=AgentRevisionRef(id=root_id, revision=heads[root_id].revision) if root_id else None)
        return httpx.Response(200, content=state["graph"].model_dump_json())

    connect = CatalogConnection.connect
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(CatalogConnection, "connect", lambda self, **kwargs: connect(self, transport=transport))
    return state


def test_graph_edit_uses_exact_parent_revision_and_shared_session(workspace_server):
    newer = workspace_server["graph"].model_dump(mode="json")
    newer["agents"] = [{**agent, "field_from_a_newer_release": "kept"} for agent in newer["agents"]]
    workspace_server["graph"] = AgentGraph.model_validate(newer)
    server = ServerWorkspace.configured()
    graph = server.graph()
    child = graph.agents[1]
    saved = server.edit_agent(graph, child.id, AgentEdit(criteria="price.in >= 1", reasoning="low", reports_to=None))
    changed = saved.agents[1]
    assert changed.parent_revision == child.revision
    assert changed.criteria == "price.in >= 1" and changed.reasoning == "low" and changed.reports_to is None
    assert saved.root_agent == graph.root_agent
    assert changed.prompt == child.prompt and changed.capabilities == child.capabilities
    assert sum(request.url.path == "/v1/auth/local-preview" for request in workspace_server["requests"]) == 1
    put = next(request for request in workspace_server["requests"] if request.method == "PUT")
    assert json.loads(put.content)["agents"][0]["field_from_a_newer_release"] == "kept"
    assert {request.headers["user-agent"] for request in workspace_server["requests"]} == {USER_AGENT}


def test_root_change_detaches_in_one_cas(workspace_server):
    server = ServerWorkspace.configured()
    graph = server.graph()
    saved = server.set_root(graph, graph.agents[1].id)
    assert saved.root_agent.id == graph.agents[1].id
    assert saved.agents[1].reports_to is None
    assert len([request for request in workspace_server["requests"] if request.method == "PUT"]) == 1


def test_workflow_run_is_revision_guarded_idempotent_and_status_is_real(workspace_server):
    server = ServerWorkspace.configured()
    request = WorkflowRunRequest(workflow={"key": {"id": workspace_server["workflow_id"]}, "revision": workspace_server["workflow_revision"]},
                                 idempotency_key=str(uuid4()), invocation={"values": {"question": "bounded task"}})
    first = server.run_workflow(request)
    second = server.run_workflow(request)
    assert first.id == second.id and first.status == "running"
    assert first.invocation.values == {"question": "bounded task"}
    assert len(workspace_server["runs"]) == 1
    workspace_server["runs"][request.idempotency_key] = first.model_copy(update={"status": "succeeded", "result": "actual result"})
    current = server.workflow_status(request.workflow.key.id, request.idempotency_key)
    assert current.status == "succeeded" and current.result == "actual result"


def test_workflow_revision_change_never_runs_latest(workspace_server):
    request = WorkflowRunRequest(workflow={"key": {"id": workspace_server["workflow_id"]}, "revision": uuid4()}, idempotency_key=str(uuid4()))
    with pytest.raises(WorkflowConflictError):
        ServerWorkspace.configured().run_workflow(request)
    assert workspace_server["runs"] == {}


def test_workflow_timeout_retains_request_for_status_lookup(workspace_server):
    server = ServerWorkspace.configured()
    request = WorkflowRunRequest(workflow={"key": {"id": workspace_server["workflow_id"]}, "revision": workspace_server["workflow_revision"]}, idempotency_key=str(uuid4()))
    workspace_server["failure"] = "run-timeout"
    with pytest.raises(WorkflowRunUncertain):
        server.run_workflow(request)
    assert server.workflow_status(request.workflow.key.id, request.idempotency_key).id == workspace_server["runs"][request.idempotency_key].id
    workspace_server["failure"] = None
    assert server.run_workflow(request).id == workspace_server["runs"][request.idempotency_key].id
    assert len(workspace_server["runs"]) == 1


def test_workflow_invalid_input_rejection_then_corrected_new_request(workspace_server, capsys):
    server = ServerWorkspace.configured()
    request = WorkflowRunRequest(workflow={"key": {"id": workspace_server["workflow_id"]}, "revision": workspace_server["workflow_revision"]},
                                 idempotency_key=str(uuid4()), invocation={"values": {"invalid": "example"}})
    with pytest.raises(WorkflowRunRejected):
        server.run_workflow(request)
    assert server.workflow_status(request.workflow.key.id, request.idempotency_key) is None
    with pytest.raises(SystemExit) as result:
        cli(["workspace", "workflow-run", str(request.workflow.key.id), "--revision", str(request.workflow.revision),
             "--idempotency-key", request.idempotency_key, "--invocation-json", request.invocation.model_dump_json(), "--json-out"])
    assert result.value.code == 1
    assert json.loads(capsys.readouterr().out)["code"] == "run_rejected"
    corrected = WorkflowRunRequest(workflow=request.workflow, idempotency_key=str(uuid4()), invocation={"values": {"question": "corrected"}})
    assert server.run_workflow(corrected).invocation.values == {"question": "corrected"}
    assert list(workspace_server["runs"]) == [corrected.idempotency_key]


@pytest.mark.parametrize("status", [500, 502, 503])
def test_workflow_server_failure_never_becomes_definite_rejection(workspace_server, status):
    workspace_server["failure"] = status
    request = WorkflowRunRequest(workflow={"key": {"id": workspace_server["workflow_id"]}, "revision": workspace_server["workflow_revision"]}, idempotency_key=str(uuid4()))
    with pytest.raises(WorkflowRunUncertain):
        ServerWorkspace.configured().run_workflow(request)


@pytest.mark.parametrize("status", [201, 403])
def test_workflow_token_uses_existing_execute_scope_only(workspace_server, tmp_path, status):
    token_file = tmp_path / "synthetic-token"
    token_file.write_text(workspace_server["token"])
    token_file.chmod(0o600)
    server = ServerWorkspace(connection=workspace_server["connection"].model_copy(update={"auth_mode": "token", "token_file": token_file}))
    request = WorkflowRunRequest(workflow={"key": {"id": workspace_server["workflow_id"]}, "revision": workspace_server["workflow_revision"]}, idempotency_key=str(uuid4()))
    if status == 403:
        workspace_server["failure"] = 403
        with pytest.raises(CatalogAuthenticationError):
            server.run_workflow(request)
        assert not workspace_server["runs"]
    else:
        assert server.run_workflow(request).status == "running"
    assert not any(item.url.path == "/v1/bootstrap" for item in workspace_server["requests"])


def test_approve_script_posts_the_exact_digest_with_owner_csrf(tmp_path, monkeypatch) -> None:
    """The client-side half of threat-modeler mitigation #1: approving a script hits the OWNER-
    gated endpoint with the bootstrap CSRF token, exactly like `save_graph` already does for
    graph edits -- never a bare unauthenticated POST."""
    monkeypatch.setattr(UserConfig, "PATH", tmp_path / "config.env")
    workspace_id, machine_id, digest = uuid4(), uuid4(), "a" * 64
    posted = {}
    fixture_csrf = "-".join(("synthetic", "csrf"))

    def respond(request):
        if request.url.path == "/v1/auth/local-preview":
            return httpx.Response(200, headers={"Set-Cookie": "session=synthetic; Path=/"}, json={})
        if request.url.path == "/v1/bootstrap":
            return httpx.Response(200, content=Bootstrap(
                account=Account(account_id=uuid4(), email="fixture@example.invalid", locale="en", verified=True),
                workspaces=(Workspace(workspace_id=workspace_id, name="Fixture", role="owner"),),
                current_workspace_id=workspace_id, csrf_token=fixture_csrf,
                session_expires_at=datetime.now(UTC) + timedelta(hours=1),
            ).model_dump_json())
        assert request.url.path == f"/v1/workspaces/{workspace_id}/machines/{machine_id}/script-approvals"
        assert request.headers["x-csrf-token"] == fixture_csrf
        posted["body"] = json.loads(request.content)
        return httpx.Response(201, json={"approved": True, "source_digest": digest})

    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode="preview", workspace_id=workspace_id)
    connection.save()
    server = ServerWorkspace(connection=connection)

    server.approve_script(machine_id, digest, transport=httpx.MockTransport(respond))

    assert posted["body"] == {"source_digest": digest}


def test_cli_workflow_run_and_status_are_pinned_structured_json(workspace_server, capsys):
    identity, revision, key = str(workspace_server["workflow_id"]), str(workspace_server["workflow_revision"]), str(uuid4())
    with pytest.raises(SystemExit) as result:
        cli(["workspace", "workflow-run", identity, "--revision", revision, "--idempotency-key", key,
             "--invocation-json", '{"values":{"question":"example"}}', "--json-out"])
    assert result.value.code == 0
    response = json.loads(capsys.readouterr().out)
    assert response["run"]["workflow"]["revision"] == revision
    assert response["run"]["idempotency_key"] == key
    with pytest.raises(SystemExit) as result:
        cli(["workspace", "workflow-status", identity, "--idempotency-key", key, "--json-out"])
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out)["run"]["status"] == "running"


@pytest.mark.parametrize("failure,kind", [(409, WorkspaceConflictError), (401, CatalogAuthenticationError), (403, CatalogAuthenticationError)])
def test_save_refusal_preserves_graph_and_draft(workspace_server, failure, kind):
    server = ServerWorkspace.configured()
    graph = server.graph()
    edit = AgentEdit(criteria="price.in >= 1")
    workspace_server["failure"] = failure
    with pytest.raises(kind):
        server.edit_agent(graph, graph.agents[1].id, edit)
    assert workspace_server["graph"] == graph
    assert edit.criteria == "price.in >= 1"
    if failure != 409:
        assert not server.connection.session_path().exists()


def test_token_connection_never_writes_or_reads_token_for_save(workspace_server):
    connection = workspace_server["connection"].model_copy(update={"auth_mode": "token", "token_file": UserConfig.PATH.parent / "not-read"})
    server = ServerWorkspace(connection=connection)
    graph = workspace_server["graph"]
    with pytest.raises(CatalogAuthenticationError, match="read-only"):
        server.set_root(graph, graph.agents[1].id)
    assert not workspace_server["requests"]


def test_cli_current_graph_edit_and_conflict_json(workspace_server, capsys):
    with pytest.raises(SystemExit) as success:
        cli(["workspace", "status", "--json-out"])
    assert success.value.code == 0
    value = json.loads(capsys.readouterr().out)
    assert value["ok"] and value["state"] == "current"
    assert "token_file" not in value and "csrf_token" not in value
    child = workspace_server["graph"].agents[1]
    args = ["workspace", "agent-edit", str(child.id), "--expected-revision", value["graph"]["revision"],
            "--edit-json", '{"criteria":"price.in >= 2","reasoning":"medium"}', "--json-out"]
    with pytest.raises(SystemExit) as success:
        cli(args)
    assert success.value.code == 0
    assert json.loads(capsys.readouterr().out)["graph"]["agents"][1]["reasoning"] == "medium"
    with pytest.raises(SystemExit):
        cli(args)
    assert json.loads(capsys.readouterr().out)["code"] == "conflict"


def test_real_application_routes(workspace_server):
    server = ServerWorkspace.configured()
    identity = uuid4()
    assert server.link("agents", identity).endswith(f"/#agents?agent={identity}")
    assert server.link("workflows", identity).endswith(f"/#workflows?workflow={identity}")
    assert server.link("personal").endswith("/#personal")


def test_configured_model_selection_keeps_exact_provider_connection(workspace_server):
    server = ServerWorkspace.configured()
    model = ConfiguredModelRef(id="configured/model", connection={"id": uuid4(), "revision": uuid4(), "capability": "http"})
    graph = server.graph()
    saved = server.edit_agent(graph, graph.agents[1].id, AgentEdit(model=model))
    assert saved.agents[1].model == model
    assert saved.agents[1].criteria == graph.agents[1].criteria


def test_viewer_read_access_does_not_enable_graph_writes(workspace_server):
    bootstrap = workspace_server["bootstrap"]
    workspace_server["bootstrap"] = bootstrap.model_copy(update={"workspaces": (bootstrap.workspaces[0].model_copy(update={"role": "viewer"}),)})
    server = ServerWorkspace.configured()
    graph = server.graph()
    assert not server.can_edit()
    with pytest.raises(CatalogAuthenticationError, match="permission"):
        server.edit_agent(graph, graph.agents[1].id, AgentEdit(reasoning="low"))
    assert not any(request.method == "PUT" for request in workspace_server["requests"])


def test_success_status_with_wrong_edit_is_not_accepted(workspace_server):
    server = ServerWorkspace.configured()
    graph = server.graph()
    workspace_server["failure"] = "wrong-save"
    with pytest.raises(ValueError, match="differs"):
        server.edit_agent(graph, graph.agents[1].id, AgentEdit(reasoning="low"))


async def test_tui_keyboard_saves_server_and_retains_conflict_draft(workspace_server, monkeypatch):
    monkeypatch.setattr(InteractTUI, "_load_registry_info", lambda self: None)
    monkeypatch.setattr(InteractTUI, "_check_update", lambda self: None)
    app = InteractTUI()
    async with app.run_test(size=(120, 55)) as pilot:
        app.query_one(TabbedContent).active = "tab-workspace"
        await pilot.pause()
        pane = app.query_one(WorkspacePane)
        await pane.workers.wait_for_complete()
        tree = pane.query_one(Tree)
        tree.focus()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert pane.selected is not None
        pane.query_one("#workspace-criteria", Input).value = "price.in >= 2"
        pane.query_one("#workspace-reasoning", Select).value = "low"
        button = pane.query_one("#workspace-save", Button)
        button.focus()
        await pilot.press("enter")
        await pilot.pause()
        selected = next(agent for agent in workspace_server["graph"].agents if agent.id == pane.selected)
        assert selected.criteria == "price.in >= 2" and selected.reasoning == "low"
        workspace_server["failure"] = 409
        pane.query_one("#workspace-criteria", Input).value = "price.in >= 3"
        button.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert pane.query_one("#workspace-criteria", Input).value == "price.in >= 3"
        assert "Draft retained" in str(pane.query_one("#workspace-status", Static).render())
        assert next(agent for agent in workspace_server["graph"].agents if agent.id == pane.selected).criteria == "price.in >= 2"
        # Native tree creation/render stays bounded at the full supported graph size.
        template = workspace_server["graph"].agents[0]
        deep = []
        for index in range(1000):
            deep.append(template.model_copy(update={"id": uuid4(), "revision": uuid4(), "name": f"Agent {index}", "role_key": f"agent-{index}",
                                                      "reports_to": deep[-1].id if deep else None}))
        graph = AgentGraph(revision="b" * 64, agents=tuple(deep), root_agent=AgentRevisionRef(id=deep[0].id, revision=deep[0].revision))
        pane.selected = None
        pane.accept(graph)
        await pilot.pause()
        node = tree.root
        shown = 0
        while node.children:
            node = node.children[0]
            node.expand()
            shown += 1
        assert shown == 32
        tree.select_node(node)
        await pilot.pause()
        pane.query_one("#workspace-subtree", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert tree.root.data == deep[31].id
        assert tree.root.children[0].data == deep[32].id
        workspace_server["failure"] = "run-timeout"
        pane.query_one("#workspace-workflow", Select).value = str(workspace_server["workflow_id"])
        pane.query_one("#workspace-workflow-inputs", Input).value = '{"question":"keyboard request"}'
        pane.query_one("#workspace-run-workflow", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        request = pane.workflow_requests[workspace_server["workflow_id"]]
        assert len(workspace_server["runs"]) == 1
        assert "may still be running" in str(pane.query_one("#workspace-workflows", Static).render())
        pane.query_one("#workspace-new-execution", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert pane.workflow_requests[workspace_server["workflow_id"]].idempotency_key == request.idempotency_key
        workspace_server["failure"] = None
        pane.query_one("#workspace-run-workflow", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert len(workspace_server["runs"]) == 1
        assert pane.workflow_requests[workspace_server["workflow_id"]] == request
        workspace_server["runs"][request.idempotency_key] = workspace_server["runs"][request.idempotency_key].model_copy(update={"status": "succeeded"})
        pane.query_one("#workspace-run-status", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "succeeded" in str(pane.query_one("#workspace-workflows", Static).render())
        pane.query_one("#workspace-new-execution", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert workspace_server["workflow_id"] not in pane.workflow_requests
        workspace_server["failure"] = None
        pane.query_one("#workspace-workflow-inputs", Input).value = '{"invalid":"example"}'
        pane.query_one("#workspace-run-workflow", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        rejected = pane.workflow_requests[workspace_server["workflow_id"]]
        assert workspace_server["workflow_id"] in pane.workflow_settled
        assert "rejected before dispatch" in str(pane.query_one("#workspace-workflows", Static).render())
        pane.query_one("#workspace-run-status", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "New execution" in str(pane.query_one("#workspace-workflows", Static).render())
        inputs = pane.query_one("#workspace-workflow-inputs", Input)
        inputs.focus()
        await pilot.press("home", "shift+end", "backspace")
        await pilot.press(*list('{"question":"corrected"}'))
        pane.query_one("#workspace-new-execution", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert inputs.value == '{"question":"corrected"}'
        assert workspace_server["workflow_id"] not in pane.workflow_requests
        pane.query_one("#workspace-run-workflow", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        corrected = pane.workflow_requests[workspace_server["workflow_id"]]
        assert corrected.idempotency_key != rejected.idempotency_key
        assert workspace_server["runs"][corrected.idempotency_key].invocation.values == {"question": "corrected"}
