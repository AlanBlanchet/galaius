"""A script starts a server workflow, gets the hand back when it ends, and continues with its
outputs — through the SDK (`interact.Client` / `AsyncClient`) and the CLI (`interact workflows`)."""

import asyncio
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from interact_core import ArtifactRef, TriggerInvocation, WorkflowEvent, WorkflowRevision, WorkflowRun
from interact_core.accounts import Account, Bootstrap, Workspace

from interact.agents.catalog_connection import CatalogConnection, CatalogConnectionError
from interact.cli.app import app as cli
from interact.client import AsyncClient, Client
from interact.config import UserConfig
from interact.workflows import WorkflowNotFound, WorkflowRunFailed

REPORT = b"quarterly report\n"


@pytest.fixture
def server(tmp_path, monkeypatch):
    """The workspace API as a script sees it: 202 start, a run that advances one status per read,
    its events behind an `after` cursor, the exact revision, and the file output's bytes."""
    monkeypatch.setattr(UserConfig, "PATH", tmp_path / "config.env")
    connection = CatalogConnection(endpoint="http://127.0.0.1:8767", auth_mode="preview", workspace_id=uuid4())
    connection.save()
    base = f"/v1/workspaces/{connection.workspace_id}"
    bootstrap = Bootstrap(account=Account(account_id=uuid4(), email="fixture@example.invalid", locale="en", verified=True),
                          workspaces=(Workspace(workspace_id=connection.workspace_id, name="Fixture", role="owner"),),
                          current_workspace_id=connection.workspace_id, csrf_token="synthetic-csrf",
                          session_expires_at=datetime.now(UTC) + timedelta(hours=1))
    step = uuid4()
    workflow = WorkflowRevision(key={"id": uuid4()}, revision=uuid4(), name="Write a report", created_at=datetime.now(UTC), edges=(),
        nodes=({"id": step, "label": "Draft", "x": 0, "y": 0, "impl": {"kind": "builtin", "op": "input"}, "config": {"value": ""},
                "ports": ({"name": "value", "direction": "output", "value_type": "text"}, {"name": "input", "direction": "input", "value_type": "text"})},),
        interface={"outputs": ({"name": "answer", "target": {"node": step, "port": "value"}}, {"name": "report", "target": {"node": step, "port": "value"}})})
    artifact = ArtifactRef(connection={"id": uuid4(), "revision": uuid4(), "capability": "write"}, path="reports/q3.txt",
                           digest=hashlib.sha256(REPORT).hexdigest(), media_type="text/plain", size=len(REPORT))
    state = {"outcome": "succeeded", "served": REPORT, "lose_start_answers": 0, "runs": {}, "events": {}, "starts": [], "head": workflow, "revisions": {workflow.revision: workflow}, "slow_cancel": False, "dropped_reads": 0}

    def emit(run: WorkflowRun, kind: str, **payload) -> None:
        events = state["events"][run.id]
        events.append(WorkflowEvent(run_id=run.id, sequence=len(events) + 1, kind=kind, timestamp=datetime.now(UTC), payload=payload))

    def advance(run: WorkflowRun) -> WorkflowRun:
        if run.status == "queued":
            run = run.model_copy(update={"status": "running"})
            emit(run, "started")
            emit(run, "progress", type="step", node_id=str(step), status="succeeded", duration_ms=12)
        elif run.status == "running":
            failed = state["outcome"] == "failed"
            run = run.model_copy(update={"status": state["outcome"], "error": "step Draft failed" if failed else None,
                                         "result": None if failed else {"answer": "42", "report": artifact.model_dump(mode="json")}})
            emit(run, "error" if failed else "result")
        state["runs"][run.id] = run
        return run

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/auth/local-preview":
            return httpx.Response(200, headers={"Set-Cookie": "session=synthetic; Path=/"}, json={})
        if path == "/v1/bootstrap":
            return httpx.Response(200, content=bootstrap.model_dump_json())
        if path == f"{base}/workflows":
            return httpx.Response(200, json=[state["head"].model_dump(mode="json")])
        if path.startswith(f"{base}/workflows/{workflow.key.id}/revisions/"):
            return httpx.Response(200, content=state["revisions"][UUID(path.rsplit("/", 1)[1])].model_dump_json())
        if path == f"{base}/workflows/{workflow.key.id}/runs" and request.method == "POST":
            assert request.headers["Prefer"] == "respond-async" and request.headers["x-csrf-token"] == "synthetic-csrf"
            key = request.headers["Idempotency-Key"]
            state["starts"].append(key)
            existing = next((run for run in state["runs"].values() if run.idempotency_key == key), None)
            run = existing or WorkflowRun(id=uuid4(), workflow={"key": workflow.key, "revision": workflow.revision}, status="queued", idempotency_key=key,
                                          invocation=TriggerInvocation.model_validate_json(request.content), created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
            if existing is None:
                state["runs"][run.id], state["events"][run.id] = run, []
                emit(run, "queued")
            if state["lose_start_answers"]:
                state["lose_start_answers"] -= 1
                raise httpx.ReadTimeout("answer lost in transit", request=request)
            return httpx.Response(202, content=run.model_dump_json())
        if path.startswith(f"{base}/runs/"):
            if request.method == "GET" and state["dropped_reads"]:
                state["dropped_reads"] -= 1
                raise httpx.RemoteProtocolError("server restarting", request=request)
            run_id = UUID(path.split("/")[5])
            if path.endswith("/events"):
                after = int(request.url.params["after"])
                return httpx.Response(200, json=[event.model_dump(mode="json") for event in state["events"][run_id] if event.sequence > after])
            if path.endswith("/cancel"):
                state["runs"][run_id] = state["runs"][run_id].model_copy(update={"status": "cancelled"})
                if state["slow_cancel"]:
                    raise httpx.ReadTimeout("cancelled, answer late", request=request)
                return httpx.Response(200, content=state["runs"][run_id].model_dump_json())
            return httpx.Response(200, content=advance(state["runs"][run_id]).model_dump_json())
        assert path == f"{base}/connections/{artifact.connection.id}/{artifact.connection.revision}/artifacts/{artifact.path}"
        return httpx.Response(200, content=state["served"])

    connect = CatalogConnection.connect
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(CatalogConnection, "connect", lambda self, **kwargs: connect(self, transport=transport))
    return state


def test_run_hands_back_outputs_and_files(server, tmp_path) -> None:
    run = Client(poll_seconds=0).workflows.run("Write a report", inputs={"topic": "Q3"})

    assert run.status == "succeeded" and run.outputs["answer"] == "42"
    assert (saved := run.download("report", tmp_path)).read_bytes() == REPORT
    assert run.download("report", tmp_path) == saved  # the same file again is no conflict
    assert server["runs"][run.id].invocation.values == {"topic": "Q3"}


def test_stream_yields_every_event_once_in_order(server) -> None:
    run = Client(poll_seconds=0).workflows.start("Write a report")

    events = list(run.stream())

    assert [event.sequence for event in events] == list(range(1, len(server["events"][run.id]) + 1))
    assert events[-1].kind == "result" and run.done
    assert [line for event in events if (line := run.describe(event)) and "Draft" in line][0].endswith("Draft: succeeded - 12 ms")


def test_failed_run_never_hands_back_outputs(server) -> None:
    server["outcome"] = "failed"
    run = Client(poll_seconds=0).workflows.run("Write a report")

    with pytest.raises(WorkflowRunFailed, match="step Draft failed"):
        run.outputs


def test_lost_start_answer_is_retried_with_the_same_key(server) -> None:
    server["lose_start_answers"] = 1

    run = Client(poll_seconds=0).workflows.start("Write a report")

    assert len(server["runs"]) == 1 and len(set(server["starts"])) == 1 and len(server["starts"]) == 2
    assert run.id in server["runs"]


def test_a_waiting_script_outlives_dropped_connections(server) -> None:
    server["dropped_reads"] = 4

    assert Client(poll_seconds=0).workflows.run("Write a report").outputs["answer"] == "42"


@pytest.mark.parametrize("slow_cancel", [False, True])
def test_cancel_holds_even_when_its_answer_is_late(server, slow_cancel) -> None:
    server["slow_cancel"] = slow_cancel
    run = Client(poll_seconds=0).workflows.start("Write a report")

    assert run.cancel().status == "cancelled" and run.refresh().status == "cancelled"


def test_same_key_after_an_edit_returns_the_run_on_its_own_revision(server) -> None:
    client = Client(poll_seconds=0)
    first = client.workflows.run("Write a report", idempotency_key="nightly-2026-09-25")
    edited = server["head"].model_copy(update={"revision": uuid4(), "parent_revision": server["head"].revision})
    server["head"] = server["revisions"][edited.revision] = edited

    again = client.workflows.run("Write a report", idempotency_key="nightly-2026-09-25")

    assert again.id == first.id and again.workflow.revision == first.workflow.revision != edited.revision
    assert again.outputs["answer"] == "42" and len(server["runs"]) == 1


@pytest.mark.parametrize(("served", "existing", "path", "refusal"), [
    (b"something else\n", None, "reports/q3.txt", "digest"),
    (REPORT, b"kept", "reports/q3.txt", "other content"),
    (REPORT, None, "../../escape.txt", "leaves the download directory"),
])
def test_a_file_is_saved_only_where_and_as_recorded(server, tmp_path, served, existing, path, refusal) -> None:
    server["served"] = served
    run = Client(poll_seconds=0).workflows.run("Write a report")
    (downloads := tmp_path / "downloads").mkdir()
    if existing:
        (downloads / "reports").mkdir()
        (downloads / "reports" / "q3.txt").write_bytes(existing)

    with pytest.raises(CatalogConnectionError, match=refusal):
        run.download(run.files["report"].model_copy(update={"path": path}), downloads)

    assert sorted(item.relative_to(downloads).as_posix() for item in downloads.rglob("*") if item.is_file()) == (["reports/q3.txt"] if existing else [])
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize("workflow", ["No such workflow", str(uuid4())])
def test_unknown_workflow_is_named(server, workflow) -> None:
    with pytest.raises(WorkflowNotFound, match="no workflow"):
        Client().workflows.start(workflow)
    assert server["runs"] == {}


def test_async_client_is_the_same_surface(server) -> None:
    async def script():
        client = AsyncClient(poll_seconds=0)
        run = await client.workflows.start("Write a report", {"topic": "Q3"})
        kinds = [event.kind async for event in run.stream()]
        attached = await client.workflows.attach(run.id)
        return run, kinds, attached

    run, kinds, attached = asyncio.run(script())

    assert kinds[0] == "queued" and kinds[-1] == "result"
    assert run.outputs["answer"] == "42" and attached.workflow.name == "Write a report"


@pytest.mark.parametrize(("outcome", "arguments", "code", "status"), [
    ("succeeded", [], 0, "succeeded"),
    ("failed", [], 1, "failed"),
    ("succeeded", ["--detach"], 0, "queued"),
])
@pytest.mark.parametrize("from_stdin", [False, True])
def test_cli_blocks_prints_outputs_and_exits_by_status(server, capsys, tmp_path, monkeypatch, outcome, arguments, code, status, from_stdin) -> None:
    server["outcome"] = outcome
    (inputs := tmp_path / "inputs.json").write_text(json.dumps({"topic": "Q3", "pages": 3}))
    monkeypatch.setattr("sys.stdin", io.StringIO(inputs.read_text()))

    with pytest.raises(SystemExit) as exit_:
        cli(["workflows", "run", "Write a report", "--input-json", "-" if from_stdin else str(inputs), "--input", "pages=5", "--poll-seconds", "0", *arguments])

    printed = capsys.readouterr()
    assert exit_.value.code == code and json.loads(printed.out)["status"] == status
    assert next(iter(server["runs"].values())).invocation.values == {"topic": "Q3", "pages": 5}
    assert ("Draft: succeeded" in printed.err) == (not arguments)


def test_cli_downloads_file_outputs_and_names_unknown_workflows(server, capsys, tmp_path) -> None:
    with pytest.raises(SystemExit) as exit_:
        cli(["workflows", "run", "Write a report", "--download", str(tmp_path), "--quiet", "--poll-seconds", "0"])
    printed = capsys.readouterr()
    assert exit_.value.code == 0 and printed.err == ""
    assert (saved := tmp_path / "reports" / "q3.txt").read_bytes() == REPORT
    summary = json.loads(printed.out)
    assert summary["outputs"]["report"]["downloaded_to"] == str(saved) and summary["result"]["answer"] == "42"

    with pytest.raises(SystemExit) as exit_:
        cli(["workflows", "run", "Missing"])
    assert exit_.value.code == 2 and "no workflow" in json.loads(capsys.readouterr().out)["error"]
