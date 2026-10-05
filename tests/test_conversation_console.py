import ast
import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Literal

import pytest
from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import JsonValue, ValidationError
from interact_core import PromptKey, PromptSelection
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

from interact.agents import registry as reg
from interact.agents.assembly import _TransportRegistry
from interact.agents.codex_schema import encode_supported_response
from interact.agents.codex_transport import _CodexTransport, _RpcMessage
from interact.agents.events import AgentEvent, ConversationInteraction, InteractionField
from interact.agents.host import _ConversationHost
from interact.agents.protocol import (
    ConversationRequest,
    InteractionSubmission,
    ModelSelection,
)
from interact.agents.providers import CodexProvider
from interact.data import PackageData
from interact.config import Config
from interact.private_files import PRIVATE_FILES
from interact.processes import process_group_options, stop_process_tree
from interact.server_registry import _alive as process_alive
from tests.support import catalog_json
from tests.support.agents import install_fake_cli
from tests.support.private_files import loosen

FIXTURES = Path(__file__).parent / "fixtures" / "agents"
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
CODEX_SCHEMA_CAPTURE = PROJECT_ROOT / "src/interact/agents/codex_app_server_schema"


@pytest.fixture
def console_workspace(monkeypatch):
    bundled_raw = PackageData.read(PackageData.MODELS) or "{}"
    bundled = json.loads(bundled_raw)
    providers = bundled.get("providers", {})
    for spec in providers.values():
        if not isinstance(spec, dict):
            continue
        for env_key in spec.get("envKeys", []):
            if isinstance(env_key, str):
                monkeypatch.setenv(env_key, "")
    monkeypatch.setenv("OLLAMA_DISCOVERY", "0")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("INTERACT_MODELS_JSON", "{}")

    root = Path("out/tests/conversation-console") / str(uuid.uuid4())
    workspace = root / "workspace"
    binary_dir = root / "bin"
    home = root / "home"
    for path in (workspace, binary_dir, home):
        path.mkdir(parents=True)
    install_fake_cli(binary_dir, "codex", FIXTURES / "fake_codex_app_server.py")
    (binary_dir / "codex.json").write_text(json.dumps({
        "mode": "success",
        "models": ["openai/example-model"],
    }))
    monkeypatch.setenv("PATH", os.pathsep.join(
        (str(binary_dir.resolve()), str(Path(sys.executable).parent), os.environ["PATH"])
    ))
    monkeypatch.setenv("HOME", str(home.resolve()))
    monkeypatch.setenv("USERPROFILE", str(home.resolve()))
    monkeypatch.setenv("INTERACT_AGENTS_DIR", str(home.resolve() / ".interact" / "out" / "agents"))
    yield root, workspace.resolve(), binary_dir
    shutil.rmtree(root)


async def _open_console(workspace: Path):
    return await asyncio.create_subprocess_exec(
        "uv", "run", "--project", str(PROJECT_ROOT), "interact", "agents", "console",
        "--workspace-root", str(workspace),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(os.environ),
        cwd=workspace,
        **process_group_options(),
    )


async def _exchange(process, payload: dict) -> dict:
    process.stdin.write((json.dumps(payload) + "\n").encode())
    await process.stdin.drain()
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        response = json.loads(line)
        if response.get("type") == "response" and response.get("request_id") == payload["request_id"]:
            return response


async def _event(process, *kinds: str) -> dict:
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        message = json.loads(line)
        if message.get("type") == "event" and message["event"]["kind"] in kinds:
            return message


async def _stop_console(process) -> None:
    process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        await stop_process_tree(process)  # `uv run` and the console under it, never an orphan
    stderr = process.stderr
    assert stderr is not None
    detail = (await asyncio.wait_for(stderr.read(), timeout=10)).decode(errors="replace")
    assert process.returncode == 0
    assert "Traceback" not in detail


def _scenario(binary_dir: Path, **values: object) -> None:
    (binary_dir / "codex.json").write_text(json.dumps(values))


def _codex_log(binary_dir: Path) -> list[dict]:
    path = binary_dir / "codex.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _models(*models: tuple[str, float]) -> str:
    return catalog_json(
        *((model, cost, cost) for model, cost in models),
        env_keys={"openai": ["OPENAI_API_KEY"]},
        extra={model: {"capabilities": ["llm"]} for model, _ in models},
        defaults={},
    )


async def _open_api_server(root: Path, *, delay: float = 0.0):
    port_file = root / "api.port"
    log_file = root / "api.log"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(FIXTURES / "fake_openai_server.py"),
        "--port-file", str(port_file),
        "--log-file", str(log_file),
        "--delay", str(delay),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr = process.stderr
    assert stderr is not None
    for _ in range(100):
        if port_file.exists():
            return process, int(port_file.read_text()), log_file
        if process.returncode is not None:
            detail = (await stderr.read()).decode(errors="replace")
            raise RuntimeError(f"fake API server exited: {detail}")
        await asyncio.sleep(0.02)
    process.terminate()
    await process.wait()
    raise RuntimeError("fake API server did not publish its port")


async def _open_prompt_server(
    root: Path, *, content: str, revision: str, token: str,
    body_revision: str | None = None,
):
    port_file = root / "prompt.port"
    log_file = root / "prompt.log"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(FIXTURES / "fake_prompt_server.py"),
        "--port-file", str(port_file),
        "--log-file", str(log_file),
        "--content", content,
        "--revision", revision,
        "--token", token,
        *(["--body-revision", body_revision] if body_revision is not None else []),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr = process.stderr
    assert stderr is not None
    for _ in range(100):
        if port_file.exists():
            return process, int(port_file.read_text()), log_file
        if process.returncode is not None:
            detail = (await stderr.read()).decode(errors="replace")
            raise RuntimeError(f"fake prompt server exited: {detail}")
        await asyncio.sleep(0.02)
    process.terminate()
    await process.wait()
    raise RuntimeError("fake prompt server did not publish its port")


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "request_id": "r", "method": "start"},
        {"version": 1, "request_id": "r", "method": "send", "run_id": "run"},
        {
            "version": 1,
            "request_id": "r",
            "method": "interaction",
            "run_id": "run",
            "submission": {"interaction_id": "interaction"},
        },
    ],
)
async def test_command_boundary_rejects_incomplete_method_payloads(
    console_workspace, payload
) -> None:
    _, workspace, _ = console_workspace
    process = await _open_console(workspace)
    try:
        response = await _exchange(process, payload)
        assert response["ok"] is False
        assert response["error_code"] == "invalid_request"
        assert len(response["error"]) <= 500
    finally:
        await _stop_console(process)


def test_model_selection_is_one_unresolved_intent() -> None:
    with pytest.raises(ValidationError, match="model.*criterion|criterion.*model"):
        ModelSelection(model="openai/example-model", criterion="price.in < 1")


def test_interaction_submission_validates_and_encodes_one_atomic_typed_mapping() -> None:
    interaction = ConversationInteraction(
        id="interaction-1",
        kind="user_input",
        title="Typed input",
        fields=[
            InteractionField(
                key="choice", kind="choice", label="Choice", options=["A", "B"]
            ),
            InteractionField(key="reason", kind="text", label="Reason"),
            InteractionField(key="notes", kind="text", label="Notes", required=False),
            InteractionField(key="confirmed", kind="boolean", label="Confirmed"),
        ],
    )

    values = interaction.validate_submission({
        "choice": "A", "reason": "because", "notes": "", "confirmed": True,
    })
    encoded = encode_supported_response(
        {"id": "request", "method": "item/tool/requestUserInput", "params": {}},
        values,
    )

    assert values == {"choice": "A", "reason": "because", "confirmed": True}
    assert encoded == {"answers": {
        "choice": {"answers": ["A"]},
        "reason": {"answers": ["because"]},
        "confirmed": {"answers": ["true"]},
    }}


@pytest.mark.parametrize(
    "values",
    [
        {"reason": "because", "confirmed": True},
        {"choice": "A", "reason": "because", "confirmed": True, "unknown": "value"},
        {"choice": "C", "reason": "because", "confirmed": True},
        {"choice": True, "reason": "because", "confirmed": True},
        {"choice": "A", "reason": True, "confirmed": True},
        {"choice": "A", "reason": "   ", "confirmed": True},
        {"choice": "A", "reason": "because", "confirmed": "true"},
    ],
    ids=[
        "missing-required", "unknown", "choice-membership", "choice-kind",
        "text-kind", "required-text-blank", "boolean-kind",
    ],
)
def test_interaction_submission_rejects_the_whole_invalid_mapping(
    values: dict[str, object],
) -> None:
    interaction = ConversationInteraction(
        id="interaction-1",
        kind="user_input",
        title="Typed input",
        fields=[
            InteractionField(
                key="choice", kind="choice", label="Choice", options=["A", "B"]
            ),
            InteractionField(key="reason", kind="text", label="Reason"),
            InteractionField(key="notes", kind="text", label="Notes", required=False),
            InteractionField(key="confirmed", kind="boolean", label="Confirmed"),
        ],
    )

    with pytest.raises(ValueError):
        interaction.validate_submission(values)


@pytest.mark.parametrize("key", [".", "..", "__proto__", "prototype", "constructor"])
def test_interaction_field_keys_are_safe(key: str) -> None:
    with pytest.raises(ValidationError):
        InteractionField(key=key, kind="text", label="Unsafe")


def test_interaction_field_keys_must_be_unique() -> None:
    with pytest.raises(ValidationError):
        ConversationInteraction(
            id="interaction-1",
            kind="user_input",
            title="Duplicate input",
            fields=[
                InteractionField(key="same", kind="text", label="First"),
                InteractionField(key="same", kind="text", label="Second"),
            ],
        )


@pytest.mark.parametrize(
    "field",
    [
        {"key": "choice", "kind": "choice", "label": "Choice", "options": []},
        {"key": "choice", "kind": "choice", "label": "Choice", "options": ["A", "A"]},
        {"key": "notes", "kind": "text", "label": "Notes", "options": ["A"]},
        {"key": "flag", "kind": "boolean", "label": "Flag", "options": ["true"]},
    ],
    ids=["empty-choice", "duplicate-choice", "text-options", "boolean-options"],
)
def test_interaction_field_schema_is_self_consistent(field: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        InteractionField.model_validate(field)


def test_all_optional_interaction_accepts_an_empty_submission() -> None:
    submission = InteractionSubmission(interaction_id="interaction-1", values={})
    interaction = ConversationInteraction(
        id="interaction-1",
        kind="user_input",
        title="Optional input",
        fields=[InteractionField(
            key="notes", kind="text", label="Notes", required=False,
        )],
    )

    assert interaction.validate_submission(submission.values) == {}
    with pytest.raises(ValidationError):
        InteractionSubmission(interaction_id="interaction-1", values={"notes": 1})


def test_codex_turn_error_is_preserved() -> None:
    transport = _CodexTransport(provider=CodexProvider(), workspace_root=Path("."))

    event = transport.normalize_completion(run_id="run-1", params={
        "turn": {
            "id": "turn-1",
            "status": "failed",
            "error": {"message": "synthetic provider failure"},
        },
    })

    assert event.kind == "error"
    assert event.status == "provider_failed"
    assert event.text == "synthetic provider failure"


def test_codex_accepts_vendor_extension_fields_on_notifications() -> None:
    frame = _RpcMessage.model_validate({
        "method": "configWarning",
        "params": {"summary": "synthetic warning", "details": None},
        "emittedAtMs": 1_790_092_139_983,
    })

    assert frame.notification
    assert frame.method == "configWarning"


async def test_codex_handshake_failure_is_shown_in_catalog_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    class FailedConnection:
        async def initialize(self) -> bool:
            raise ValueError("provider frame validation failed: emittedAtMs")

        async def close(self) -> None:
            return None

    async def open_failed(*_args: object, **_kwargs: object) -> FailedConnection:
        return FailedConnection()

    monkeypatch.setattr("interact.agents.assembly._CodexTransport.open", open_failed)
    registry = _TransportRegistry(workspace_root=tmp_path)

    await registry._open_session()

    assert registry._session_availability == "incompatible"
    assert registry._session_reason.startswith(
        "Codex app-server handshake failed: provider frame validation failed: emittedAtMs."
    )
    assert "Check the installed Codex CLI and sign-in" in registry._session_reason


async def test_codex_model_catalog_failure_is_shown_in_route_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    class ActivePolicy:
        def provider_active(self, name: str) -> bool:
            assert name == "codex"
            return True

    class FailedCatalogConnection:
        async def models(self) -> tuple[list[str], str | None]:
            raise ValueError("model/list response validation failed: data")

    monkeypatch.setattr("interact.agents.assembly.Policy.load", lambda: ActivePolicy())
    registry = _TransportRegistry(workspace_root=tmp_path)
    registry._session = FailedCatalogConnection()
    registry._session_authenticated = True
    registry._session_availability = "available"

    route = await registry.session_route(0)

    assert route.availability == "incompatible"
    assert route.reason.startswith(
        "Codex app-server model catalog failed: model/list response validation failed: data."
    )
    assert "use an API route" in route.reason


async def test_codex_empty_model_catalog_is_actionable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    class ActivePolicy:
        def provider_active(self, name: str) -> bool:
            assert name == "codex"
            return True

    class EmptyCatalogConnection:
        async def models(self) -> tuple[list[str], str | None]:
            return [], None

    monkeypatch.setattr("interact.agents.assembly.Policy.load", lambda: ActivePolicy())
    registry = _TransportRegistry(workspace_root=tmp_path)
    registry._session = EmptyCatalogConnection()
    registry._session_authenticated = True
    registry._session_availability = "available"

    route = await registry.session_route(0)

    assert route.availability == "incompatible"
    assert "no usable models" in route.reason
    assert "check Codex model access" in route.reason
    assert "choose an API route" in route.reason


async def test_codex_unauthenticated_route_names_the_login_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    class UnauthenticatedConnection:
        async def initialize(self) -> bool:
            return False

        async def close(self) -> None:
            return None

    async def open_unauthenticated(*_args: object, **_kwargs: object) -> UnauthenticatedConnection:
        return UnauthenticatedConnection()

    monkeypatch.setattr("interact.agents.assembly._CodexTransport.open", open_unauthenticated)
    registry = _TransportRegistry(workspace_root=tmp_path)

    await registry._open_session()

    assert registry._session_availability == "unauthenticated"
    assert "login intent" in registry._session_reason
    assert "sign in with Codex" in registry._session_reason
    assert "choose an API route" in registry._session_reason


async def test_interaction_remains_retryable_until_provider_response_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    transport = _CodexTransport(provider=CodexProvider(), workspace_root=tmp_path)
    interaction = ConversationInteraction(
        id="interaction-1",
        kind="command_approval",
        title="Approve command",
        fields=[InteractionField(
            key="decision", kind="choice", label="Decision", options=["accept", "decline"]
        )],
    )
    transport._register_interaction(
        interaction_id=interaction.id,
        request_id="provider-request",
        method="item/commandExecution/requestApproval",
        interaction=interaction,
        run_id="owner-run",
    )

    async def fail_response(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("simulated response failure")

    monkeypatch.setattr(_CodexTransport, "respond", fail_response)
    with pytest.raises(ConnectionError, match="simulated response failure"):
        await transport.submit_interaction(
            run_id="owner-run",
            interaction_id=interaction.id,
            values={"decision": "decline"},
        )
    assert transport.has_interaction(run_id="owner-run", interaction_id=interaction.id)

    responses: list[tuple[int | str, dict[str, JsonValue]]] = []

    async def accept_response(
        _transport: _CodexTransport, request_id: int | str, result: dict[str, JsonValue]
    ) -> None:
        responses.append((request_id, result))

    monkeypatch.setattr(_CodexTransport, "respond", accept_response)
    await transport.submit_interaction(
        run_id="owner-run",
        interaction_id=interaction.id,
        values={"decision": "decline"},
    )

    assert responses == [("provider-request", {"decision": "decline"})]
    assert not transport.has_interaction(run_id="owner-run", interaction_id=interaction.id)


async def test_transport_registry_closes_each_transport_identity_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    provider = CodexProvider()
    first = _CodexTransport(provider=provider, workspace_root=tmp_path)
    middle = first.model_copy()
    last = first.model_copy()
    registry = _TransportRegistry(workspace_root=tmp_path)
    registry._session = first
    registry._route_transports["first-route-alias"] = first
    registry._route_transports["middle-route"] = middle
    registry._run_transports["first-run-alias"] = first
    registry._run_transports["last-run"] = last
    registry._run_transports["middle-run-alias"] = middle
    assert len({id(first), id(middle), id(last)}) == 3
    assert first == middle == last
    with pytest.raises(TypeError, match="unhashable"):
        {first, middle, last}
    closed: list[int] = []

    async def record_close(transport: _CodexTransport) -> None:
        closed.append(id(transport))
        if transport is first:
            raise RuntimeError("first transport close failed")
        if transport is last:
            raise ConnectionError("last transport close failed")

    monkeypatch.setattr(_CodexTransport, "close", record_close)

    with pytest.raises(ExceptionGroup) as caught:
        await registry.close()

    assert closed == [id(first), id(middle), id(last)]
    assert [str(error) for error in caught.value.exceptions] == [
        "first transport close failed",
        "last transport close failed",
    ]


async def test_host_finishes_its_writer_when_registry_close_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    registry = _TransportRegistry(workspace_root=tmp_path)
    host = _ConversationHost(
        workspace_root=tmp_path, transport_registry=registry, config=Config()
    )
    writer_started = asyncio.Event()
    writer_finished = asyncio.Event()
    writer_tasks: list[asyncio.Task[None]] = []

    async def read_eof_after_writer_started(_host: _ConversationHost) -> bytes:
        await writer_started.wait()
        return b""

    async def observe_writer_sentinel(observed: _ConversationHost) -> None:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("writer did not run in an asyncio task")
        writer_tasks.append(current)
        writer_started.set()
        assert await observed._outbound.get() is None
        writer_finished.set()

    async def fail_registry_close(_registry: _TransportRegistry) -> None:
        raise RuntimeError("registry close failed")

    monkeypatch.setattr(_ConversationHost, "_read_input", read_eof_after_writer_started)
    monkeypatch.setattr(_ConversationHost, "_write_output", observe_writer_sentinel)
    monkeypatch.setattr(_TransportRegistry, "close", fail_registry_close)

    try:
        with pytest.raises(RuntimeError, match="registry close failed"):
            await asyncio.wait_for(host.serve(), timeout=0.5)
        assert writer_finished.is_set(), (
            "serve must signal and await its writer even when transport teardown raises"
        )
    finally:
        for task in writer_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*writer_tasks, return_exceptions=True)


async def test_fake_app_server_crosses_catalog_thread_turn_and_stream(console_workspace) -> None:
    _, workspace, binary_dir = console_workspace
    _scenario(binary_dir, mode="success", paged_models=[
        ["openai/example-model"],
        ["openai/second-example", "openai/example-model"],
    ])
    process = await _open_console(workspace)
    stdout = process.stdout
    assert stdout is not None
    try:
        initialized = await _exchange(process, {
            "version": 1, "request_id": "init", "method": "initialize",
        })
        assert initialized["ok"] is True
        catalog_response = await _exchange(process, {
            "version": 1, "request_id": "catalog", "method": "catalog",
        })
        route = next(route for route in catalog_response["catalog"]["routes"]
                     if route["id"] == "codex:local_session")
        assert route["availability"] == "available", route["reason"]
        assert "experimental" in route["reason"]
        assert "experimental" not in route["billing_note"]
        assert "plan quota or usage credits" in route["billing_note"]
        assert [model["id"] for model in route["models"]] == [
            "openai/example-model", "openai/second-example",
        ]

        request = ConversationRequest(
            route_id=route["id"],
            prompt="answer from the deterministic process",
            selection=ModelSelection(model="openai/example-model"),
            workspace_root=str(workspace),
        )
        started = await _exchange(process, {
            "version": 1,
            "request_id": "start",
            "method": "start",
            "request": request.model_dump(mode="json"),
        })
        streamed = []
        while not any(item["event"]["kind"] in ("done", "error") for item in streamed):
            streamed.append(json.loads(await asyncio.wait_for(stdout.readline(), timeout=10)))

        assert streamed[-1]["event"]["kind"] == "done", streamed[-1]["event"]
        assert started["run"]["provider"] == "codex"
        assert started["run"]["provider_session_id"] == "thread-root"
        assert started["run"]["prompt"] is None
        assert any(item["event"]["kind"] == "text"
                   and item["event"]["text"] == "synthetic answer"
                   for item in streamed)
        methods = [json.loads(line)["method"]
                   for line in (binary_dir / "codex.log").read_text().splitlines()
                   if json.loads(line)["method"] is not None]
        assert methods[:8] == [
            "initialize",
            "initialized",
            "account/read",
            "model/list",
            "model/list",
            "model/list",
            "model/list",
            "thread/start",
        ]
        assert "turn/start" in methods
        thread_start = next(record for record in _codex_log(binary_dir)
                            if record["method"] == "thread/start")
        assert thread_start["params"] == {
            "model": "openai/example-model",
            "cwd": str(workspace),
            "sandbox": "read-only",
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "allowProviderModelFallback": False,
        }
        initialize = next(record for record in _codex_log(binary_dir)
                          if record["method"] == "initialize")
        assert initialize["params"]["clientInfo"]["name"] == "interact"
        assert initialize["params"]["clientInfo"]["version"]
        assert initialize["params"]["capabilities"] == {"experimentalApi": False}
        account = next(record for record in _codex_log(binary_dir)
                       if record["method"] == "account/read")
        assert account["params"] == {"refreshToken": False}
        listed = [record for record in _codex_log(binary_dir)
                  if record["method"] == "model/list"]
        assert [record["params"]["cursor"] for record in listed[:2]] == [None, "1"]
        turn_start = next(record for record in _codex_log(binary_dir)
                          if record["method"] == "turn/start")
        assert turn_start["params"] == {
            "threadId": "thread-root",
            "input": [{"type": "text", "text": "answer from the deterministic process"}],
        }
    finally:
        await _stop_console(process)


async def test_fake_app_server_preserves_codex_turn_error(console_workspace) -> None:
    _, workspace, binary_dir = console_workspace
    _scenario(binary_dir, mode="failed_turn", models=["openai/example-model"])
    process = await _open_console(workspace)
    try:
        started = await _exchange(process, {
            "version": 1, "request_id": "start", "method": "start",
            "request": {
                "route_id": "codex:local_session", "prompt": "fail visibly",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        assert started["ok"] is True
        failed = await _event(process, "error")
        assert failed["event"]["status"] == "provider_failed"
        assert failed["event"]["text"] == "synthetic provider failure"
    finally:
        await _stop_console(process)


@pytest.mark.parametrize(
    ("case", "expected_ok"),
    [
        ("verified", True),
        ("verified-file", True),
        ("revision-mismatch", False),
        ("digest-mismatch", False),
        ("missing-config", False),
        ("wrong-token", False),
        ("offline-uncached", False),
        ("token-file-mode", False),
        ("token-file-symlink", False),
        ("token-file-oversize", False),
        ("token-ambiguous", False),
    ],
)
async def test_console_binds_server_prompt_before_starting_provider(
    console_workspace, monkeypatch: pytest.MonkeyPatch, case: str, expected_ok: bool,
) -> None:
    root, workspace, binary_dir = console_workspace
    trusted = "trusted prompt from the authenticated catalog"
    revision = "00000000-0000-4000-8000-000000000002"
    token = "synthetic-test-token"
    digest = hashlib.sha256(trusted.encode()).hexdigest()
    prompt_server, port, _ = await _open_prompt_server(
        root,
        content=trusted,
        revision=revision,
        token=token,
        body_revision=(
            "00000000-0000-4000-8000-000000000003"
            if case == "revision-mismatch" else None
        ),
    )
    endpoint = "http://127.0.0.1:1" if case == "offline-uncached" else f"http://127.0.0.1:{port}"
    monkeypatch.setenv("INTERACT_PROMPT_ENDPOINT", endpoint)
    monkeypatch.setenv("INTERACT_PROMPT_ACCOUNT", "tenant-a")
    token_file = root / "prompt-token"
    token_file.write_text(token if case != "token-file-oversize" else "x" * 4097)
    PRIVATE_FILES.restrict(token_file)
    if case == "token-file-mode":
        loosen(token_file)
    configured_file = token_file
    if case == "token-file-symlink":
        configured_file = root / "prompt-token-link"
        configured_file.symlink_to(token_file)
    file_case = case.startswith("token-file") or case in {"verified-file", "token-ambiguous"}
    monkeypatch.setenv(
        "INTERACT_PROMPT_TOKEN",
        "wrong" if case == "wrong-token" else (token if not file_case or case == "token-ambiguous" else ""),
    )
    if file_case:
        monkeypatch.setenv("INTERACT_PROMPT_TOKEN_FILE", str(configured_file.resolve()))
    else:
        monkeypatch.delenv("INTERACT_PROMPT_TOKEN_FILE", raising=False)
    monkeypatch.setenv("INTERACT_PROMPT_CACHE", str((root / "prompts.sqlite3").resolve()))
    if case == "missing-config":
        monkeypatch.setenv("INTERACT_PROMPT_ACCOUNT", "")
    process = await _open_console(workspace)
    try:
        catalog_response = await _exchange(process, {
            "version": 1, "request_id": "catalog", "method": "catalog",
        })
        route = next(route for route in catalog_response["catalog"]["routes"]
                     if route["id"] == "codex:local_session")
        request = ConversationRequest(
            route_id=route["id"],
            prompt="What should the user do next?",
            prompt_selection=PromptSelection(
                key=PromptKey(namespace="interact", slug="system"),
                channel="stable",
                digest="0" * 64 if case == "digest-mismatch" else digest,
            ),
            selection=ModelSelection(model="openai/example-model"),
            workspace_root=str(workspace),
        )
        started = await _exchange(process, {
            "version": 1,
            "request_id": "start",
            "method": "start",
            "request": request.model_dump(mode="json"),
        })
        assert started["ok"] is expected_ok, started
        if expected_ok:
            await _event(process, "done", "error")
            turn_start = next(record for record in _codex_log(binary_dir)
                              if record["method"] == "turn/start")
            assert turn_start["params"]["input"] == [
                {"type": "text", "text": "What should the user do next?"}
            ]
            thread_start = next(record for record in _codex_log(binary_dir)
                                if record["method"] == "thread/start")
            assert thread_start["params"]["developerInstructions"] == trusted
            assert started["run"]["prompt"] == {
                **request.prompt_selection.model_dump(mode="json"),
                "revision": revision,
            }
        else:
            assert started["error_code"] == "invalid_request"
            assert not any(record["method"] in {"thread/start", "turn/start"}
                           for record in _codex_log(binary_dir))
    finally:
        await _stop_console(process)
        prompt_server.terminate()
        await prompt_server.wait()
    assert (binary_dir / "codex.exit").read_text() == "closed\n"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("missing", "unavailable"),
        ("auth_required", "unauthenticated"),
        ("incompatible", "incompatible"),
        ("empty_models", "incompatible"),
    ],
)
async def test_catalog_reports_session_negative_controls(
    console_workspace, mode, expected, monkeypatch
) -> None:
    _, workspace, binary_dir = console_workspace
    if mode == "missing":
        for installed in (binary_dir / "codex", binary_dir / "codex.cmd"):
            installed.unlink(missing_ok=True)
        safe_path = [
            str(binary_dir),
            str(Path(shutil.which("uv") or "uv").parent),
            str(Path(sys.executable).parent),
            *os.defpath.split(os.pathsep),
        ]
        monkeypatch.setenv("PATH", os.pathsep.join(dict.fromkeys(safe_path)))
    elif mode == "empty_models":
        _scenario(binary_dir, mode="success", paged_models=[[]])
    else:
        _scenario(binary_dir, mode=mode, models=["openai/example-model"])
    process = await _open_console(workspace)
    try:
        response = await _exchange(process, {
            "version": 1, "request_id": "catalog", "method": "catalog",
        })
        route = next(route for route in response["catalog"]["routes"]
                     if route["id"] == "codex:local_session")
        assert route["availability"] == expected
        assert route["models"] == []
    finally:
        await _stop_console(process)


async def test_criterion_resolves_only_inside_the_selected_route(
    console_workspace, monkeypatch
) -> None:
    _, workspace, binary_dir = console_workspace
    model_ids = ["openai/expensive-example", "openai/cheap-example"]
    _scenario(binary_dir, mode="success", models=model_ids)
    monkeypatch.setenv("INTERACT_MODELS_JSON", _models(
        (model_ids[0], 10.0), (model_ids[1], 0.5)
    ))
    process = await _open_console(workspace)
    try:
        catalog = await _exchange(process, {
            "version": 1, "request_id": "catalog", "method": "catalog",
        })
        route = next(route for route in catalog["catalog"]["routes"]
                     if route["id"] == "codex:local_session")
        response = await _exchange(process, {
            "version": 1,
            "request_id": "start",
            "method": "start",
            "request": {
                "route_id": route["id"],
                "prompt": "choose inside this route",
                "selection": {"criterion": "price.in < 1"},
                "workspace_root": str(workspace),
            },
        })
        assert response["ok"] is True
        assert response["run"]["model"] == "openai/cheap-example"
    finally:
        await _stop_console(process)


async def test_model_disappearing_after_catalog_is_refused_without_fallback(
    console_workspace
) -> None:
    _, workspace, binary_dir = console_workspace
    _scenario(
        binary_dir,
        mode="success",
        model_pages=[
            ["openai/example-model", "openai/disappearing-example"],
            ["openai/example-model"],
        ],
    )
    process = await _open_console(workspace)
    try:
        await _exchange(process, {
            "version": 1, "request_id": "catalog", "method": "catalog",
        })
        response = await _exchange(process, {
            "version": 1,
            "request_id": "start",
            "method": "start",
            "request": {
                "route_id": "codex:local_session",
                "prompt": "do not reroute me",
                "selection": {"model": "openai/disappearing-example"},
                "workspace_root": str(workspace),
            },
        })
        assert response["ok"] is False
        assert response["error_code"] == "unavailable"
        assert all(record["method"] != "thread/start" for record in _codex_log(binary_dir))
    finally:
        await _stop_console(process)


@pytest.mark.parametrize(
    "selection",
    [
        ModelSelection(model="openai/zeta-selected"),
        ModelSelection(criterion="price.in < 1"),
    ],
    ids=["explicit-model", "route-criterion"],
)
async def test_explicit_api_route_executes_the_resolved_model_exactly_once(
    console_workspace, monkeypatch, selection: ModelSelection
) -> None:
    root, workspace, binary_dir = console_workspace
    api_process, port, api_log = await _open_api_server(root)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-value")
    monkeypatch.setenv("OPENAI_API_BASE", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setenv("INTERACT_MODELS_JSON", _models(
        ("openai/alpha-default", 10.0),
        ("openai/zeta-selected", 0.5),
    ))
    process = await _open_console(workspace)
    try:
        catalog = await _exchange(process, {
            "version": 1, "request_id": "catalog", "method": "catalog",
        })
        routes = {item["id"]: item for item in catalog["catalog"]["routes"]}
        # The manufactured consumer-session rows are gone (see the route-list test): a policy
        # notice is not a route. What this test is about — the API route — is unaffected.
        assert not any(r["availability"] == "policy_blocked" for r in routes.values())
        assert routes["openai:api"]["connection"] == "api"
        route = next(route for route in catalog["catalog"]["routes"]
                     if route["id"] == "openai:api")
        response = await _exchange(process, {
            "version": 1,
            "request_id": "start",
            "method": "start",
            "request": {
                "route_id": route["id"],
                "prompt": "one explicit API request",
                "selection": selection.model_dump(mode="json"),
                "workspace_root": str(workspace),
            },
        })
        assert response["ok"] is True
        done = await _event(process, "done")
        assert done["event"]["text"] == "synthetic api answer"
        requests = [json.loads(line) for line in api_log.read_text().splitlines()]
        assert len(requests) == 1
        canonical_model = f"openai/{requests[0]['model']}"
        assert canonical_model == response["run"]["model"] == "openai/zeta-selected"
        assert done["run"]["model"] == canonical_model
        stored = reg.get_run(response["run"]["run_id"])
        assert stored is not None and stored.model == canonical_model
        assert all(record["method"] != "thread/start" for record in _codex_log(binary_dir))
        assert route["capabilities"] == ["cancel"]
    finally:
        await _stop_console(process)
        api_process.terminate()
        await api_process.wait()


async def test_prompt_selected_api_keeps_instruction_separate_from_user_turn(
    console_workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, workspace, _ = console_workspace
    api_process, api_port, api_log = await _open_api_server(root)
    instruction = "Answer using the verified project policy."
    revision = "00000000-0000-4000-8000-000000000004"
    token = "synthetic-test-token"
    digest = hashlib.sha256(instruction.encode()).hexdigest()
    prompt_process, prompt_port, prompt_log = await _open_prompt_server(
        root, content=instruction, revision=revision, token=token,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-value")
    monkeypatch.setenv("OPENAI_API_BASE", f"http://127.0.0.1:{api_port}/v1")
    monkeypatch.setenv(
        "INTERACT_MODELS_JSON", _models(("openai/example-model", 1.0))
    )
    monkeypatch.setenv("INTERACT_PROMPT_ENDPOINT", f"http://127.0.0.1:{prompt_port}")
    monkeypatch.setenv("INTERACT_PROMPT_ACCOUNT", "tenant-a")
    monkeypatch.setenv("INTERACT_PROMPT_TOKEN", token)
    monkeypatch.delenv("INTERACT_PROMPT_TOKEN_FILE", raising=False)
    cache_path = (root / "prompts.sqlite3").resolve()
    monkeypatch.setenv("INTERACT_PROMPT_CACHE", str(cache_path))
    process = await _open_console(workspace)
    try:
        started = await _exchange(process, {
            "version": 1,
            "request_id": "start",
            "method": "start",
            "request": {
                "route_id": "openai:api",
                "prompt": "What changed in the repository?",
                "prompt_selection": {
                    "key": {"namespace": "interact", "slug": "system"},
                    "channel": "stable",
                    "digest": digest,
                },
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        prompt_requests = (
            [json.loads(line) for line in prompt_log.read_text().splitlines()]
            if prompt_log.exists() else []
        )
        cache_stat = cache_path.stat() if cache_path.exists() else None
        diagnostic = {
            "response": started,
            "prompt_requests": prompt_requests,
            "console_returncode": process.returncode,
            "api_request_count": len(api_log.read_text().splitlines()) if api_log.exists() else 0,
            "cache_exists": cache_stat is not None,
            "cache_size": cache_stat.st_size if cache_stat is not None else 0,
            "cache_inode": cache_stat.st_ino if cache_stat is not None else 0,
        }
        assert started["ok"] is True, diagnostic
        await _event(process, "done")
        request = json.loads(api_log.read_text().splitlines()[0])
        assert request["messages"] == [
            {"role": "system", "content": instruction},
            {"role": "user", "content": "What changed in the repository?"},
        ]
        assert started["run"]["prompt"] == {
            "key": {"namespace": "interact", "slug": "system"},
            "channel": "stable",
            "revision": revision,
            "digest": digest,
        }
        assert prompt_requests == [
            {"path": "/v1/catalog", "status": 200},
            {"path": f"/v1/revisions/interact/system/{digest}", "status": 200},
        ]
    finally:
        await _stop_console(process)
        for server in (api_process, prompt_process):
            server.terminate()
            await server.wait()


async def test_explicit_api_active_history_includes_assistant_response(
    console_workspace, monkeypatch
) -> None:
    root, workspace, _ = console_workspace
    api_process, port, api_log = await _open_api_server(root)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-value")
    monkeypatch.setenv("OPENAI_API_BASE", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setenv("INTERACT_MODELS_JSON", _models(("openai/example-model", 1.0)))
    process = await _open_console(workspace)
    try:
        started = await _exchange(process, {
            "version": 1, "request_id": "start", "method": "start",
            "request": {
                "route_id": "openai:api", "prompt": "first",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        await _event(process, "done")
        continued = await _exchange(process, {
            "version": 1, "request_id": "continue", "method": "send",
            "run_id": started["run"]["run_id"], "prompt": "second",
        })
        assert continued["ok"] is True
        assert continued["run"]["finished_at"] is None
        await _event(process, "done")
        requests = [json.loads(line) for line in api_log.read_text().splitlines()]
        assert requests[1]["messages"] == [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "synthetic api answer"},
            {"role": "user", "content": "second"},
        ]
    finally:
        await _stop_console(process)
        api_process.terminate()
        await api_process.wait()


async def test_explicit_api_cancel_stops_inflight_completion(
    console_workspace, monkeypatch
) -> None:
    root, workspace, _ = console_workspace
    api_process, port, api_log = await _open_api_server(root, delay=1.0)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-value")
    monkeypatch.setenv("OPENAI_API_BASE", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setenv("INTERACT_MODELS_JSON", _models(("openai/example-model", 1.0)))
    process = await _open_console(workspace)
    try:
        started = await _exchange(process, {
            "version": 1, "request_id": "start", "method": "start",
            "request": {
                "route_id": "openai:api", "prompt": "wait",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        for _ in range(100):
            if api_log.exists():
                break
            await asyncio.sleep(0.02)
        assert api_log.exists()
        cancelled = await _exchange(process, {
            "version": 1, "request_id": "cancel", "method": "cancel",
            "run_id": started["run"]["run_id"],
        })
        assert cancelled["ok"] is True
        await _event(process, "cancelled")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_event(process, "text", "done", "error"), timeout=1.25)
    finally:
        await _stop_console(process)
        api_process.terminate()
        await api_process.wait()


@pytest.mark.parametrize("terminal", ["completed", "provider_unavailable"])
async def test_explicit_api_terminal_races_never_record_false_cancellation(
    console_workspace, monkeypatch, terminal: str
) -> None:
    root, workspace, binary_dir = console_workspace
    api_process, port, _ = await _open_api_server(root)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-value")
    monkeypatch.setenv("OPENAI_API_BASE", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setenv("INTERACT_MODELS_JSON", _models(("openai/example-model", 1.0)))
    if terminal == "provider_unavailable":
        api_process.terminate()
        await api_process.wait()
    process = await _open_console(workspace)
    try:
        started = await _exchange(process, {
            "version": 1, "request_id": "start", "method": "start",
            "request": {
                "route_id": "openai:api", "prompt": "terminal race",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        event = await _event(process, "done", "error")
        assert event["event"]["status"] == (
            "completed" if terminal == "completed" else "provider_failed"
        )
        cancelled = await _exchange(process, {
            "version": 1, "request_id": "late-cancel", "method": "cancel",
            "run_id": started["run"]["run_id"],
        })
        assert cancelled["ok"] is False
        assert cancelled["error_code"] == "conflict"
        assert all(record["method"] != "thread/start" for record in _codex_log(binary_dir))
    finally:
        await _stop_console(process)
        if api_process.returncode is None:
            api_process.terminate()
            await api_process.wait()


async def test_session_failure_never_falls_through_to_api(
    console_workspace, monkeypatch
) -> None:
    root, workspace, binary_dir = console_workspace
    _scenario(binary_dir, mode="eof", models=["openai/example-model"])
    api_process, port, api_log = await _open_api_server(root)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-value")
    monkeypatch.setenv("OPENAI_API_BASE", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setenv("INTERACT_MODELS_JSON", _models(("openai/example-model", 1.0)))
    process = await _open_console(workspace)
    try:
        response = await _exchange(process, {
            "version": 1,
            "request_id": "start",
            "method": "start",
            "request": {
                "route_id": "codex:local_session",
                "prompt": "stay on the selected session route",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        assert response["ok"] is True
        failed = await _event(process, "error")
        assert failed["event"]["status"] == "provider_failed"
        assert not api_log.exists()
    finally:
        await _stop_console(process)
        api_process.terminate()
        await api_process.wait()


async def test_continuation_conflict_cancel_and_process_restart_resume(
    console_workspace
) -> None:
    _, workspace, binary_dir = console_workspace
    _scenario(binary_dir, mode="hold", models=["openai/example-model"])
    process = await _open_console(workspace)
    started = await _exchange(process, {
        "version": 1,
        "request_id": "start",
        "method": "start",
        "request": {
            "route_id": "codex:local_session",
            "prompt": "hold this turn",
            "selection": {"model": "openai/example-model"},
            "workspace_root": str(workspace),
        },
    })
    run_id = started["run"]["run_id"]
    conflict = await _exchange(process, {
        "version": 1,
        "request_id": "conflict",
        "method": "send",
        "run_id": run_id,
        "prompt": "this must not race",
    })
    assert conflict["ok"] is False
    assert conflict["error_code"] == "conflict"
    cancelled = await _exchange(process, {
        "version": 1,
        "request_id": "cancel",
        "method": "cancel",
        "run_id": run_id,
    })
    assert cancelled["ok"] is True
    await _event(process, "cancelled")
    await _stop_console(process)

    _scenario(binary_dir, mode="resume_failure", models=["openai/example-model"])
    failed_process = await _open_console(workspace)
    try:
        failed = await _exchange(failed_process, {
            "version": 1,
            "request_id": "failed-resume",
            "method": "send",
            "run_id": run_id,
            "prompt": "surface the failed continuation",
        })
        assert failed["ok"] is False
        assert failed["error_code"] == "provider_failed"
        stored = json.loads(
            (Path.home() / ".interact" / "out" / "agents" / f"{run_id}.json").read_text()
        )
        assert stored["status"] not in ("starting", "running")
    finally:
        await _stop_console(failed_process)

    _scenario(binary_dir, mode="hold", models=["openai/example-model"])
    resumed_process = await _open_console(workspace)
    try:
        resumed = await _exchange(resumed_process, {
            "version": 1,
            "request_id": "resume",
            "method": "send",
            "run_id": run_id,
            "prompt": "continue after the host restarted",
        })
        assert resumed["ok"] is True
        assert resumed["run"]["pid"] != started["run"]["pid"]
        assert resumed["run"]["status"] == "running"
        assert any(record["method"] == "thread/resume" for record in _codex_log(binary_dir))
        await _exchange(resumed_process, {
            "version": 1,
            "request_id": "cancel-again",
            "method": "cancel",
            "run_id": run_id,
        })
    finally:
        await _stop_console(resumed_process)


# ── A resumed conversation re-resolves against the policy AND the cooldown memory ─────────
# Same bug, same fix, as the CLI-subprocess `agent_spawn`/message path: `_send` used to replay
# `run.model` verbatim on reconnect instead of walking `ConversationRoute.resolve` fresh, so a
# model that had gone into quota cooldown since the original `start` was retried unchanged.


async def test_resumed_conversation_skips_a_cooled_model_and_uses_the_next_ranked_one(
    console_workspace, monkeypatch
) -> None:
    from interact.agents import quota

    _, workspace, binary_dir = console_workspace
    model_ids = ["openai/expensive-example", "openai/cheap-example"]
    monkeypatch.setenv("INTERACT_MODELS_JSON", _models(
        (model_ids[0], 10.0), (model_ids[1], 0.5)
    ))
    _scenario(binary_dir, mode="hold", models=model_ids)
    process = await _open_console(workspace)
    started = await _exchange(process, {
        "version": 1,
        "request_id": "start",
        "method": "start",
        "request": {
            "route_id": "codex:local_session",
            "prompt": "pick the cheapest for now",
            "selection": {"criterion": "cap.llm"},
            "workspace_root": str(workspace),
        },
    })
    assert started["ok"] is True and started["run"]["model"] == "openai/cheap-example"
    run_id = started["run"]["run_id"]
    await _exchange(process, {
        "version": 1, "request_id": "cancel", "method": "cancel", "run_id": run_id,
    })
    await _event(process, "cancelled")
    await _stop_console(process)

    # The cheap model refused for quota in the meantime — recorded in the SAME memory a
    # CLI-subprocess agent run writes to (`quota._path()` follows this fixture's own HOME).
    quota.record_refusal("codex", "openai/cheap-example")

    _scenario(binary_dir, mode="hold", models=model_ids)
    resumed_process = await _open_console(workspace)
    try:
        resumed = await _exchange(resumed_process, {
            "version": 1, "request_id": "resume", "method": "send",
            "run_id": run_id, "prompt": "continue after the host restarted",
        })
        assert resumed["ok"] is True
        assert resumed["run"]["model"] == "openai/expensive-example"
        assert any(record["method"] == "thread/resume" for record in _codex_log(binary_dir))
        await _exchange(resumed_process, {
            "version": 1, "request_id": "cancel-again", "method": "cancel", "run_id": run_id,
        })
    finally:
        await _stop_console(resumed_process)


async def test_resumed_conversation_refuses_when_every_candidate_is_cooled(
    console_workspace, monkeypatch
) -> None:
    from interact.agents import quota

    _, workspace, binary_dir = console_workspace
    model_ids = ["openai/expensive-example", "openai/cheap-example"]
    monkeypatch.setenv("INTERACT_MODELS_JSON", _models(
        (model_ids[0], 10.0), (model_ids[1], 0.5)
    ))
    _scenario(binary_dir, mode="hold", models=model_ids)
    process = await _open_console(workspace)
    started = await _exchange(process, {
        "version": 1,
        "request_id": "start",
        "method": "start",
        "request": {
            "route_id": "codex:local_session",
            "prompt": "pick the cheapest for now",
            "selection": {"criterion": "cap.llm"},
            "workspace_root": str(workspace),
        },
    })
    assert started["ok"] is True
    run_id = started["run"]["run_id"]
    await _exchange(process, {
        "version": 1, "request_id": "cancel", "method": "cancel", "run_id": run_id,
    })
    await _event(process, "cancelled")
    await _stop_console(process)

    quota.record_refusal("codex", "openai/cheap-example")
    quota.record_refusal("codex", "openai/expensive-example")

    resumed_process = await _open_console(workspace)
    try:
        resumed = await _exchange(resumed_process, {
            "version": 1, "request_id": "resume", "method": "send",
            "run_id": run_id, "prompt": "continue after the host restarted",
        })
        assert resumed["ok"] is False and resumed["error_code"] == "unavailable"
        assert "cooldown" in resumed["error"] and "new conversation" in resumed["error"]
        # Never actually asked the provider to resume the dead conversation.
        assert not any(record["method"] == "thread/resume" for record in _codex_log(binary_dir))
    finally:
        await _stop_console(resumed_process)


async def test_interaction_request_waits_for_and_forwards_the_user_decision(
    console_workspace
) -> None:
    _, workspace, binary_dir = console_workspace
    provider_request_ids = ["opaque approval/id", 1, "1"]
    _scenario(
        binary_dir,
        mode="approval",
        models=["openai/example-model"],
        approval_ids=provider_request_ids,
    )
    process = await _open_console(workspace)
    try:
        started = await _exchange(process, {
            "version": 1,
            "request_id": "start",
            "method": "start",
            "request": {
                "route_id": "codex:local_session",
                "prompt": "request an approval",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        approvals = [await _event(process, "interaction", "error") for _ in provider_request_ids]
        assert all(item["event"]["kind"] == "interaction" for item in approvals)
        local_approval_ids = [item["event"]["interaction"]["id"] for item in approvals]
        assert len(set(local_approval_ids)) == len(provider_request_ids)
        assert all("/" not in item and " " not in item for item in local_approval_ids)
        assert not any("result" in record and record["id"] in provider_request_ids
                       for record in _codex_log(binary_dir))
        for index, local_approval_id in enumerate(local_approval_ids):
            forwarded = await _exchange(process, {
                "version": 1,
                "request_id": f"approve-{index}",
                "method": "interaction",
                "run_id": started["run"]["run_id"],
                "submission": {
                    "interaction_id": local_approval_id,
                    "values": {"decision": "decline"},
                },
            })
            assert forwarded["ok"] is True
        await _event(process, "done")
        responses = [record for record in _codex_log(binary_dir)
                     if record.get("result") == {"decision": "decline"}]
        assert [record["id"] for record in responses] == provider_request_ids
    finally:
        await _stop_console(process)


async def test_interaction_is_owned_by_its_run_before_provider_mutation(
    console_workspace,
) -> None:
    _, workspace, binary_dir = console_workspace
    _scenario(
        binary_dir,
        mode="approval",
        models=["openai/example-model"],
        approval_ids=["owner-approval"],
    )
    process = await _open_console(workspace)
    try:
        started = await _exchange(process, {
            "version": 1, "request_id": "start", "method": "start",
            "request": {
                "route_id": "codex:local_session", "prompt": "request owner approval",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        interaction = await _event(process, "interaction")
        interaction_id = interaction["event"]["interaction"]["id"]
        owner_run_id = started["run"]["run_id"]
        other_run_id = "thread-other"
        reg.save_run(reg.AgentRun(
            run_id=other_run_id,
            kind="conversation",
            provider="codex",
            name="other conversation",
            cwd=str(workspace),
            project=reg.project_for(str(workspace)),
            model="openai/example-model",
            root_run_id=other_run_id,
            provider_session_id=other_run_id,
            connection="local_session",
            started_at=1,
            status="waiting",
        ))
        before = [record for record in _codex_log(binary_dir) if "result" in record]
        owner_before = reg.get_run(owner_run_id)
        other_before = reg.get_run(other_run_id)
        assert owner_before is not None and other_before is not None
        owner_events_before = reg.events_path(owner_run_id).read_bytes()
        other_events_path = reg.events_path(other_run_id)
        other_events_before = (
            other_events_path.read_bytes() if other_events_path.is_file() else None
        )

        cross_run = await _exchange(process, {
            "version": 1, "request_id": "cross-run", "method": "interaction",
            "run_id": other_run_id,
            "submission": {
                "interaction_id": interaction_id,
                "values": {"decision": "decline"},
            },
        })

        assert cross_run["ok"] is False
        assert cross_run["error_code"] == "not_found"
        assert [record for record in _codex_log(binary_dir) if "result" in record] == before
        assert reg.get_run(owner_run_id).model_dump() == owner_before.model_dump()
        assert reg.get_run(other_run_id).model_dump() == other_before.model_dump()
        assert reg.events_path(owner_run_id).read_bytes() == owner_events_before
        assert (
            other_events_path.read_bytes() if other_events_path.is_file() else None
        ) == other_events_before

        owner = await _exchange(process, {
            "version": 1, "request_id": "owner", "method": "interaction",
            "run_id": owner_run_id,
            "submission": {
                "interaction_id": interaction_id,
                "values": {"decision": "decline"},
            },
        })
        assert owner["ok"] is True
        responses = [record for record in _codex_log(binary_dir) if "result" in record]
        assert responses == [
            {"id": "owner-approval", "method": None, "result": {"decision": "decline"}}
        ]
    finally:
        await _stop_console(process)


async def test_collaboration_is_discrete_idempotent_and_enriched_out_of_order(
    console_workspace
) -> None:
    root, workspace, binary_dir = console_workspace
    _scenario(binary_dir, mode="collaboration", models=["openai/example-model"])
    process = await _open_console(workspace)
    stdout = process.stdout
    assert stdout is not None
    try:
        await _exchange(process, {
            "version": 1,
            "request_id": "start",
            "method": "start",
            "request": {
                "route_id": "codex:local_session",
                "prompt": "launch one synthetic child",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        events = []
        while not any(message["event"]["kind"] in ("done", "error") for message in events):
            line = await asyncio.wait_for(stdout.readline(), timeout=10)
            events.append(json.loads(line))
        assert events[-1]["event"]["kind"] == "done", events[-1]["event"]
        child_events = [message for message in events
                        if message["event"]["agent_run_id"] == "thread-child"]
        assert len([message for message in child_events
                    if message["event"]["kind"] == "spawn"
                    if message["event"]["event_id"].endswith(":completed")]) == 1
        child_path = root / "home" / ".interact" / "out" / "agents" / "thread-child.json"
        child = json.loads(child_path.read_text())
        assert child["parent_run_id"] == "thread-root"
        assert child["root_run_id"] == "thread-root"
        assert child["spawned_by_event_id"].endswith(":started")
        assert child["task"] == "inspect the synthetic result"
        assert child["requested_model"] == "openai/example-child"
        assert child["status"] == "done"
        assert child["tools"] == ["commandExecution"]
        assert child["input_tokens"] == 11
        assert child["output_tokens"] == 7
        assert child["started_at"] == 1.0
        assert child["finished_at"] == 2.0
        transcript = [json.loads(line) for line in child_path.with_suffix(".jsonl").read_text().splitlines()]
        assert any(event["kind"] == "text" and event["text"] == "synthetic child answer"
                   for event in transcript)
        assert any(event["kind"] == "tool" and event["tool"] == "commandExecution"
                   for event in transcript)
        tool_pair = [event for event in transcript
                     if event["kind"] in ("tool", "tool_result")]
        assert [event["kind"] for event in tool_pair] == ["tool", "tool_result"]
        assert tool_pair[0]["tool_id"] == tool_pair[1]["tool_id"] == "child-command"
        root_transcript = [
            json.loads(line)
            for line in (root / "home" / ".interact" / "out" / "agents" / "thread-root.jsonl")
            .read_text().splitlines()
        ]
        assert any(event["kind"] == "other"
                   and event["raw_type"] == "thread/syntheticFuture"
                   for event in root_transcript)
        assert any(event["kind"] == "other"
                   and event["raw_type"] == "item/syntheticFutureItem/started"
                   for event in root_transcript)
    finally:
        await _stop_console(process)


@pytest.mark.parametrize(
    ("started_id", "completed_id"),
    [("tool-a", "tool-b"), ("tool-a", 7)],
    ids=["uncorrelated", "malformed"],
)
def test_codex_tool_identity_never_correlates_different_or_malformed_items(
    tmp_path: Path, started_id: str, completed_id: object,
) -> None:
    transport = _CodexTransport(provider=CodexProvider(), workspace_root=tmp_path)
    run = reg.AgentRun(run_id="run", provider="codex", name="run", cwd=str(tmp_path))
    started = transport._normalize_item(run, "thread", {
        "turnId": "turn", "startedAtMs": 1,
        "item": {"type": "commandExecution", "id": started_id, "command": "first"},
    }, "turn", "item/started")[0].event
    if not isinstance(completed_id, str):
        with pytest.raises(TypeError, match="malformed"):
            transport._normalize_item(run, "thread", {
                "turnId": "turn", "completedAtMs": 2,
                "item": {"type": "commandExecution", "id": completed_id,
                         "aggregatedOutput": "result"},
            }, "turn", "item/completed")
        return
    completed = transport._normalize_item(run, "thread", {
        "turnId": "turn", "completedAtMs": 2,
        "item": {"type": "commandExecution", "id": completed_id,
                 "aggregatedOutput": "result"},
    }, "turn", "item/completed")[0].event
    assert started.tool_id == started_id
    assert completed.tool_id == completed_id
    assert started.tool_id != completed.tool_id


async def test_provider_run_identity_collisions_never_overwrite_existing_records(
    console_workspace,
) -> None:
    _, workspace, binary_dir = console_workspace
    reg.save_run(reg.AgentRun(
        run_id="thread-child",
        provider="claude",
        name="existing root",
        cwd=str(workspace),
        root_run_id="thread-child",
        status="waiting",
    ))

    with pytest.raises(ValueError, match="collision"):
        reg.upsert_provider_child(
            run_id="thread-child",
            provider="codex",
            parent_run_id="thread-root",
            root_run_id="thread-root",
            spawned_by_event_id="spawn-1",
            cwd=str(workspace),
            task="must not overwrite",
            requested_model=None,
            status="running",
        )

    _scenario(
        binary_dir,
        mode="success",
        models=["openai/example-model"],
        thread_id="thread-mismatched-session",
        session_id="different-session-tree",
    )
    process = await _open_console(workspace)
    try:
        mismatch = await _exchange(process, {
            "version": 1,
            "request_id": "session-mismatch",
            "method": "start",
            "request": {
                "route_id": "codex:local_session",
                "prompt": "reject an unrelated provider session tree",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        assert mismatch["ok"] is False
        assert mismatch["error_code"] == "provider_failed"
        assert reg.get_run("thread-mismatched-session") is None
    finally:
        await _stop_console(process)

    reg.save_run(reg.AgentRun(
        run_id="thread-root",
        provider="claude",
        name="unrelated existing run",
        cwd=str(workspace),
        root_run_id="thread-root",
        status="waiting",
    ))
    _scenario(
        binary_dir,
        mode="success",
        models=["openai/example-model"],
        thread_id="thread-root",
    )
    process = await _open_console(workspace)
    try:
        collision = await _exchange(process, {
            "version": 1,
            "request_id": "root-collision",
            "method": "start",
            "request": {
                "route_id": "codex:local_session",
                "prompt": "must not replace an existing run",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        assert collision["ok"] is False
        assert collision["error_code"] == "provider_failed"
        preserved = reg.get_run("thread-root")
        assert preserved is not None
        assert preserved.provider == "claude"
        assert preserved.name == "unrelated existing run"
    finally:
        await _stop_console(process)


@pytest.mark.parametrize(
    ("event", "expected", "terminal"),
    [
        (AgentEvent(kind="interaction", status="interaction_required"), "waiting", False),
        (AgentEvent(kind="done", status="completed", at=10), "done", True),
        (AgentEvent(kind="error", status="failed", at=10), "failed", True),
        (AgentEvent(kind="error", status="provider_failed", at=10), "failed", True),
        (AgentEvent(kind="cancelled", status="cancelled", at=10), "cancelled", True),
    ],
    ids=["interaction", "done", "failed", "provider-failed", "cancelled"],
)
def test_provider_child_events_fold_their_own_lifecycle(
    console_workspace, event: AgentEvent, expected: str, terminal: bool
) -> None:
    _, workspace, _ = console_workspace
    run_id = f"child-{event.kind}"
    reg.save_run(reg.AgentRun(
        run_id=run_id,
        kind="provider_child",
        provider="codex",
        name="provider child",
        cwd=str(workspace),
        parent_run_id="thread-root",
        root_run_id="thread-root",
        status="running",
    ))

    reg.append_event(run_id, event)

    stored = reg.get_run(run_id)
    assert stored is not None
    assert stored.status == expected
    if terminal:
        finished_at = stored.finished_at
        assert finished_at == 10
        for stale in (
            AgentEvent(kind="started", status="running", at=5),
            AgentEvent(kind="interaction", status="interaction_required", at=5),
        ):
            reg.append_event(run_id, stale)
            stored = reg.get_run(run_id)
            assert stored is not None
            assert stored.status == expected
            assert stored.finished_at == finished_at


@pytest.mark.parametrize(
    "mode", ["malformed", "known_malformed", "oversized", "eof", "collaboration_eof"]
)
async def test_provider_stream_failures_are_bounded_and_redacted(
    console_workspace, mode
) -> None:
    _, workspace, binary_dir = console_workspace
    _scenario(binary_dir, mode=mode, models=["openai/example-model"])
    process = await _open_console(workspace)
    try:
        started = await _exchange(process, {
            "version": 1,
            "request_id": "start",
            "method": "start",
            "request": {
                "route_id": "codex:local_session",
                "prompt": "private synthetic prompt that must not enter diagnostics",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        assert started["ok"] is True
        failed = await _event(process, "error")
        detail = failed["event"]["text"]
        assert len(detail) <= 500
        assert "private synthetic prompt" not in detail
        assert str(workspace) not in detail
        assert "token" not in detail.lower()
        if mode == "collaboration_eof":
            child = reg.get_run("thread-child")
            assert child is not None
            assert child.status == "failed"
    finally:
        await _stop_console(process)


async def test_schema_invalid_provider_request_fails_pending_rpc_and_reaps_process(
    console_workspace,
) -> None:
    _, workspace, binary_dir = console_workspace
    _scenario(
        binary_dir,
        mode="captured_schema_invalid",
        models=["openai/example-model"],
    )
    process = await _open_console(workspace)
    try:
        started = await _exchange(process, {
            "version": 1, "request_id": "start", "method": "start",
            "request": {
                "route_id": "codex:local_session", "prompt": "hold before cancellation",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        cancelled = await asyncio.wait_for(_exchange(process, {
            "version": 1, "request_id": "cancel", "method": "cancel",
            "run_id": started["run"]["run_id"],
        }), timeout=5)

        assert cancelled["ok"] is False
        assert cancelled["error_code"] == "provider_failed"
        failed = await _event(process, "error")
        assert failed["event"]["status"] == "provider_failed"
        stored = reg.get_run(started["run"]["run_id"])
        assert stored is not None and stored.status == "failed"
        assert (binary_dir / "codex.exit").read_text() == "closed\n"
        interrupt = next(
            record for record in _codex_log(binary_dir)
            if record["method"] == "turn/interrupt"
        )
        assert not any(
            record["id"] == interrupt["id"] and "result" in record
            for record in _codex_log(binary_dir)
        )
        healthy = await _exchange(process, {
            "version": 1, "request_id": "healthy", "method": "initialize",
        })
        assert healthy["ok"] is True
    finally:
        await _stop_console(process)


async def test_workspace_symlink_escape_is_refused_without_path_disclosure(
    console_workspace
) -> None:
    root, workspace, binary_dir = console_workspace
    _scenario(
        binary_dir,
        mode="success",
        models=["openai/example-model"],
        thread_id="../../outside-owned",
    )
    outside = root / "outside"
    outside.mkdir()
    link = workspace / "linked-outside"
    link.symlink_to(outside.resolve(), target_is_directory=True)
    process = await _open_console(workspace)
    try:
        response = await _exchange(process, {
            "version": 1,
            "request_id": "start",
            "method": "start",
            "request": {
                "route_id": "codex:local_session",
                "prompt": "never leave the allowed workspace",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(link),
            },
        })
        assert response["ok"] is False
        assert response["error_code"] == "invalid_request"
        assert str(outside.resolve()) not in response["error"]
        assert str(link) not in response["error"]

        hostile = await _exchange(process, {
            "version": 1,
            "request_id": "hostile-provider-id",
            "method": "start",
            "request": {
                "route_id": "codex:local_session",
                "prompt": "reject traversal from the provider boundary",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        assert hostile["ok"] is False
        assert hostile["error_code"] == "provider_failed"
        assert not (root / "home" / ".interact" / "outside-owned.json").exists()
    finally:
        await _stop_console(process)


@pytest.mark.parametrize(
    "payload",
    [b"{not-json\n", b'{"version":1,"request_id":"huge","method":"catalog","padding":"' + b"x" * 140_000 + b'"}\n'],
    ids=["malformed", "oversized"],
)
async def test_host_input_failures_are_bounded_and_the_process_survives(
    console_workspace, payload
) -> None:
    _, workspace, _ = console_workspace
    process = await _open_console(workspace)
    stdin = process.stdin
    stdout = process.stdout
    assert stdin is not None
    assert stdout is not None
    try:
        stdin.write(payload)
        await stdin.drain()
        line = await asyncio.wait_for(stdout.readline(), timeout=10)
        response = json.loads(line)
        assert response["ok"] is False
        assert response["error_code"] == "invalid_request"
        assert len(response["error"]) <= 500
        followup = await _exchange(process, {
            "version": 1, "request_id": "healthy", "method": "initialize",
        })
        assert followup["ok"] is True
    finally:
        await _stop_console(process)


async def test_schema_derived_codex_interactions_preserve_fields_and_encode_each_response(
    console_workspace,
) -> None:
    """One process table covers heterogeneous installed-schema request shapes and disclosure."""
    _, workspace, binary_dir = console_workspace
    _scenario(binary_dir, mode="heterogeneous_interactions", models=["openai/example-model"])
    process = await _open_console(workspace)
    try:
        started = await _exchange(process, {
            "version": 1, "request_id": "start", "method": "start",
            "request": {
                "route_id": "codex:local_session", "prompt": "exercise interactions",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        first = await _event(process, "interaction", "error")
        assert first["event"]["kind"] == "interaction"
        interactions = [first, *[
            await _event(process, "interaction", "error") for _ in range(2)
        ]]
        assert all(message["event"]["kind"] == "interaction" for message in interactions)
        assert [message["event"]["interaction"]["kind"] for message in interactions] == [
            "command_approval", "file_change_approval", "user_input",
        ]
        assert [
            (field["key"], field["kind"], field["required"], field["options"])
            for field in interactions[2]["event"]["interaction"]["fields"]
        ] == [
            ("choice", "choice", True, ["A", "B"]),
            ("notes", "text", True, []),
        ]
        for index, message in enumerate(interactions[:2], start=1):
            response = await _exchange(process, {
                "version": 1, "request_id": f"reply-{index}", "method": "interaction",
                "run_id": started["run"]["run_id"],
                "submission": {
                    "interaction_id": message["event"]["interaction"]["id"],
                    "values": {"decision": "decline"},
                },
            })
            assert response["ok"] is True
        user_input = interactions[2]["event"]["interaction"]
        before_invalid = [record for record in _codex_log(binary_dir) if "result" in record]
        for index, invalid_values in enumerate((
            {"choice": "A"},
            {"choice": "A", "notes": "details", "unknown": "value"},
            {"choice": "A", "notes": True},
            {"choice": "outside", "notes": "details"},
        )):
            refused = await _exchange(process, {
                "version": 1, "request_id": f"invalid-{index}", "method": "interaction",
                "run_id": started["run"]["run_id"],
                "submission": {
                    "interaction_id": user_input["id"],
                    "values": invalid_values,
                },
            })
            assert refused["ok"] is False
            assert refused["error_code"] == "invalid_request"
            assert [record for record in _codex_log(binary_dir) if "result" in record] == before_invalid
        answered = await _exchange(process, {
            "version": 1, "request_id": "reply-3", "method": "interaction",
            "run_id": started["run"]["run_id"],
            "submission": {
                "interaction_id": user_input["id"],
                "values": {"choice": "A", "notes": "details"},
            },
        })
        assert answered["ok"] is True
        encoded = [record for record in _codex_log(binary_dir) if "result" in record]
        assert len(encoded) == 3
        assert encoded[-1]["id"] == "interaction-3"
        assert encoded[-1]["result"] == {"answers": {
            "choice": {"answers": ["A"]},
            "notes": {"answers": ["details"]},
        }}
        rejected = [record for record in _codex_log(binary_dir) if "error" in record]
        assert rejected[-1]["id"] == "interaction-4"
        assert rejected[-1]["error"] == {
            "code": -32601, "message": "Method not supported by this client.",
        }
        terminal = await _event(process, "done", "error")
        assert terminal["event"]["kind"] == "done"
    finally:
        await _stop_console(process)


async def _fragmented_two_turn_result(console_workspace):
    """Cross the shared process seam once for either projection invariant."""
    root, workspace, binary_dir = console_workspace
    _scenario(binary_dir, mode="fragmented_usage", models=["openai/example-model"])
    process = await _open_console(workspace)
    try:
        started = await _exchange(process, {
            "version": 1, "request_id": "start", "method": "start",
            "request": {
                "route_id": "codex:local_session", "prompt": "first",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        await _event(process, "done")
        continued = await _exchange(process, {
            "version": 1, "request_id": "continue", "method": "send",
            "run_id": started["run"]["run_id"], "prompt": "second",
        })
        assert continued["ok"] is True
        await _event(process, "done")
        run_id = started["run"]["run_id"]
        events = [json.loads(line) for line in (
            root / "home" / ".interact" / "out" / "agents" / f"{run_id}.jsonl"
        ).read_text().splitlines()]
        stored = reg.get_run(run_id)
        assert stored is not None
        return events, stored
    finally:
        await _stop_console(process)


async def test_fragmented_messages_coalesce_to_authoritative_items(console_workspace) -> None:
    """Each acknowledged turn persists its prompt before one authoritative answer."""
    events, _ = await _fragmented_two_turn_result(console_workspace)
    transcript = [event for event in events if event["kind"] in ("prompt", "text")]
    assert [(event["kind"], event["turn_id"], event["text"]) for event in transcript] == [
        ("prompt", "turn-1", "first"),
        ("text", "turn-1", "synthetic fragmented answer"),
        ("prompt", "turn-2", "second"),
        ("text", "turn-2", "synthetic fragmented answer"),
    ]


async def test_cumulative_usage_survives_turns_and_duplicate_snapshots(
    console_workspace,
) -> None:
    """The second turn applies cumulative input/output/cache totals exactly once."""
    _, stored = await _fragmented_two_turn_result(console_workspace)
    assert (stored.input_tokens, stored.output_tokens) == (20, 10)
    assert (stored.cached_input_tokens, stored.cache_write_input_tokens) == (4, 2)


async def test_cumulative_usage_survives_console_process_restart(
    console_workspace,
) -> None:
    """A restarted adapter cursor must not erase or double-count the persisted first turn."""
    _, workspace, binary_dir = console_workspace
    _scenario(binary_dir, mode="fragmented_usage", models=["openai/example-model"])
    first = await _open_console(workspace)
    try:
        started = await _exchange(first, {
            "version": 1, "request_id": "start", "method": "start",
            "request": {
                "route_id": "codex:local_session", "prompt": "first",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        await _event(first, "done")
    finally:
        await _stop_console(first)

    second = await _open_console(workspace)
    try:
        continued = await _exchange(second, {
            "version": 1, "request_id": "continue", "method": "send",
            "run_id": started["run"]["run_id"], "prompt": "second",
        })
        assert continued["ok"] is True
        await _event(second, "done")
        stored = reg.get_run(started["run"]["run_id"])
        assert stored is not None
        assert (stored.input_tokens, stored.output_tokens, stored.cached_input_tokens,
                stored.cache_write_input_tokens) == (20, 10, 4, 2)
    finally:
        await _stop_console(second)


@pytest.mark.parametrize("terminal", ["done", "failed", "cancelled"])
@pytest.mark.parametrize("kind", ["conversation", "provider_child"])
def test_terminal_runs_ignore_late_same_turn_frames(
    console_workspace,
    terminal: Literal["done", "failed", "cancelled"],
    kind: Literal["conversation", "provider_child"],
) -> None:
    """The lifecycle table covers every terminal root/child state without per-row tests."""
    _, workspace, _ = console_workspace
    run_id = f"{kind}-{terminal}"
    reg.save_run(reg.AgentRun(
        run_id=run_id, kind=kind, provider="codex", name=kind, cwd=str(workspace),
        root_run_id=run_id if kind == "conversation" else "root",
        parent_run_id=None if kind == "conversation" else "root",
        provider_turn_id="turn-1", status="running",
    ))
    terminal_event = {
        "done": AgentEvent(kind="done", status="completed", turn_id="turn-1", at=10),
        "failed": AgentEvent(kind="error", status="failed", turn_id="turn-1", at=10),
        "cancelled": AgentEvent(kind="cancelled", status="cancelled", turn_id="turn-1", at=10),
    }[terminal]
    reg.append_event(run_id, terminal_event)
    for late in (
        AgentEvent(kind="started", status="running", turn_id="turn-1", at=11),
        AgentEvent(kind="text", text="late", status="running", turn_id="turn-1", at=12),
        AgentEvent(kind="tool", tool="late", status="running", turn_id="turn-1", at=13),
    ):
        reg.append_event(run_id, late)
    stored = reg.get_run(run_id)
    assert stored is not None
    assert stored.status == terminal
    assert stored.finished_at == 10
    if kind == "conversation":
        reg.append_event(run_id, AgentEvent(
            kind="started", status="running", turn_id="turn-2", at=20,
        ))
        reopened = reg.get_run(run_id)
        assert reopened is not None and reopened.status == "running"
    else:
        reg.append_event(run_id, AgentEvent(
            kind="started", status="running", turn_id="child-turn-2", at=20,
        ))
        permanent = reg.get_run(run_id)
        assert permanent is not None and permanent.status == terminal


def test_new_root_turn_ignores_stale_frames_from_prior_turn(console_workspace) -> None:
    """A prior-turn frame arriving after reopen cannot mutate the new turn's snapshot."""
    _, workspace, _ = console_workspace
    run_id = "conversation-stale-prior-turn"
    reg.save_run(reg.AgentRun(
        run_id=run_id, kind="conversation", provider="codex", name="root",
        cwd=str(workspace), root_run_id=run_id, provider_turn_id="turn-1",
        status="done", finished_at=10, input_tokens=7, last="turn one complete",
    ))
    reg.append_event(run_id, AgentEvent(
        kind="started", status="running", turn_id="turn-2", at=20,
    ))
    before = reg.get_run(run_id)
    assert before is not None
    reg.append_event(run_id, AgentEvent(
        kind="text", text="stale prior answer", status="running", turn_id="turn-1",
        input_tokens=100, at=21,
    ))
    after = reg.get_run(run_id)
    assert after is not None
    assert after.model_dump() == before.model_dump()


def test_registry_append_does_not_replay_existing_jsonl(
    console_workspace, monkeypatch,
) -> None:
    """The real append_event path must stay O(new event), independent of history length."""
    _, workspace, _ = console_workspace
    run_id = "append-scale"
    reg.save_run(reg.AgentRun(
        run_id=run_id, kind="conversation", provider="synthetic", name="root",
        cwd=str(workspace), root_run_id=run_id, status="running",
    ))
    history = reg.events_path(run_id)
    history.write_text("".join(
        AgentEvent(kind="tool", event_id=f"event-{index}", tool="read").model_dump_json() + "\n"
        for index in range(10_000)
    ))
    original_read_text = Path.read_text
    replayed_bytes = 0

    def measured_read_text(path: Path, *args, **kwargs):
        nonlocal replayed_bytes
        text = original_read_text(path, *args, **kwargs)
        if path == history:
            replayed_bytes += len(text.encode())
        return text

    monkeypatch.setattr(Path, "read_text", measured_read_text)
    reg.append_event(run_id, AgentEvent(kind="done", event_id="terminal", status="completed"))
    assert replayed_bytes == 0, "append_event must not deserialize prior JSONL for an append"


def test_codex_schema_capture_drives_complete_request_policy_without_handwritten_table() -> None:
    """Captured upstream refs—not a provider-method constant—own request/result completeness."""
    fixture = CODEX_SCHEMA_CAPTURE
    manifest_path = fixture / "manifest.json"
    server_request_path = fixture / "ServerRequest.json"
    assert manifest_path.is_file() and server_request_path.is_file(), (
        "the compact tracked upstream schema capture and provenance manifest are required"
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["portable_bundle_sha256"].startswith("9178ce22")
    assert manifest["source_version"] == "codex-cli 0.150.0-alpha.8"
    assert manifest["experimental_api"] is False
    request_schema = json.loads(server_request_path.read_text())
    refs = [variant["$ref"] for variant in request_schema["oneOf"]]
    assert refs and len(refs) == len(set(refs))
    for relative in refs:
        assert (fixture / relative.removeprefix("./")).is_file()
    generated = Path("src/interact/agents/codex_schema.py")
    assert generated.is_file()
    source = generated.read_text()
    assert hashlib.sha256(server_request_path.read_bytes()).hexdigest() in source
    assert "SUPPORTED_REQUEST_HANDLERS" in source and "REJECTED_REQUEST_POLICIES" in source


@pytest.fixture(scope="session")
def wheel_build_cache() -> Path:
    """Resolve the build tool's cache before per-test HOME/XDG isolation relocates it."""
    result = subprocess.run(
        ["uv", "cache", "dir"], capture_output=True, text=True, check=True, timeout=10,
    )
    return Path(result.stdout.strip())


def test_codex_schema_capture_imports_from_an_offline_wheel(wheel_build_cache: Path) -> None:
    """Runtime validation must be package-owned rather than reaching back into tests."""
    root = Path("out/tests/codex-schema-wheel") / str(uuid.uuid4())
    wheelhouse = root / "wheelhouse"
    unpacked = root / "unpacked"
    wheelhouse.mkdir(parents=True)
    unpacked.mkdir()
    try:
        built = subprocess.run(
            [
                "uv",
                "build",
                "--offline",
                "--cache-dir",
                str(wheel_build_cache),
                "--wheel",
                "--out-dir",
                str(wheelhouse.resolve()),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert built.returncode == 0, built.stderr
        wheel = next(wheelhouse.glob("interact-*.whl"))
        shutil.unpack_archive(wheel, unpacked, "zip")

        source = (unpacked / "interact/agents/codex_schema.py").read_text()
        assert "tests/" not in source and '"tests"' not in source
        assert (unpacked / "interact/agents/codex_app_server_schema/manifest.json").is_file()
        imported = subprocess.run(
            [sys.executable, "-m", "interact.agents.codex_schema"],
            cwd=unpacked,
            # Windows needs SYSTEMROOT to load its socket layer and USERPROFILE to name a home.
            env={
                **{key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "USERPROFILE") if key in os.environ},
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            text=True,
            capture_output=True,
            check=False,
        )

        assert imported.returncode == 0, imported.stderr
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize("request_id", ["request-string", 17])
def test_codex_schema_boundary_validates_derived_requests_results_and_rejections(
    request_id: str | int,
) -> None:
    """Every captured method has one schema-valid success or an ID-preserving JSON-RPC refusal."""
    fixture = CODEX_SCHEMA_CAPTURE
    manifest_path = fixture / "manifest.json"
    server_request_path = fixture / "ServerRequest.json"
    assert manifest_path.is_file() and server_request_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    server_request = json.loads(server_request_path.read_text())
    server_request["$id"] = server_request_path.resolve().as_uri()
    schema_registry = Registry().with_resources(
        (
            path.resolve().as_uri(),
            Resource.from_contents(json.loads(path.read_text()), default_specification=DRAFT7),
        )
        for path in fixture.rglob("*.json")
        if path.name != "manifest.json"
    )
    request_validator = Draft7Validator(server_request, registry=schema_registry)
    entries = manifest["server_requests"]
    assert isinstance(entries, list) and entries
    derived_refs = {variant["$ref"] for variant in server_request["oneOf"]}
    assert {entry["request_ref"] for entry in entries} == derived_refs
    supported = {entry["method"] for entry in entries if entry["policy"] == "supported"}
    rejected = {entry["method"] for entry in entries if entry["policy"] == "reject"}
    assert supported == {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/tool/requestUserInput",
    }
    assert supported.isdisjoint(rejected) and supported | rejected == {
        entry["method"] for entry in entries
    }

    module_path = Path("src/interact/agents/codex_schema.py")
    spec = importlib.util.spec_from_file_location("interact_codex_schema_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for entry in entries:
        frame = {**entry["representative_request"], "id": request_id}
        request_validator.validate(frame)
        malformed = json.loads(json.dumps(frame))
        required_params = entry["required_params"]
        if required_params:
            malformed["params"].pop(required_params[0])
        else:
            malformed.pop("params")
        with pytest.raises(JsonSchemaValidationError):
            request_validator.validate(malformed)
        normalized = module.validate_request(frame)
        if entry["policy"] == "supported":
            result = module.encode_supported_response(
                normalized, entry["representative_interaction_values"],
            )
            response_schema = json.loads((fixture / entry["response_ref"]).read_text())
            Draft7Validator(response_schema, registry=schema_registry).validate(result)
            if entry["method"] == "item/tool/requestUserInput":
                assert all(
                    isinstance(answer["answers"], list)
                    and all(isinstance(value, str) for value in answer["answers"])
                    for answer in result["answers"].values()
                )
        else:
            error = module.reject_unsupported(normalized)
            assert error == {
                "id": request_id,
                "error": {"code": -32601, "message": "Method not supported by this client."},
            }


async def test_repeated_cancel_reuses_acknowledgement_and_interrupts_once(
    console_workspace,
) -> None:
    """The real fake process proves idempotency at the transport boundary, not in a mock."""
    _, workspace, binary_dir = console_workspace
    _scenario(binary_dir, mode="hold", models=["openai/example-model"])
    process = await _open_console(workspace)
    try:
        started = await _exchange(process, {
            "version": 1, "request_id": "start", "method": "start",
            "request": {
                "route_id": "codex:local_session", "prompt": "hold",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        replies = []
        for request_id in ("cancel-1", "cancel-2"):
            replies.append(await _exchange(process, {
                "version": 1, "request_id": request_id, "method": "cancel",
                "run_id": started["run"]["run_id"],
            }))
        assert all(reply["ok"] is True for reply in replies)
        assert replies[0]["run"]["status"] == replies[1]["run"]["status"] == "cancelled"
        assert sum(record["method"] == "turn/interrupt" for record in _codex_log(binary_dir)) == 1

        continued = await _exchange(process, {
            "version": 1, "request_id": "continue", "method": "send",
            "run_id": started["run"]["run_id"], "prompt": "hold again",
        })
        assert continued["ok"] is True
        third = await _exchange(process, {
            "version": 1, "request_id": "cancel-3", "method": "cancel",
            "run_id": started["run"]["run_id"],
        })
        assert third["ok"] is True
        interrupts = [record for record in _codex_log(binary_dir)
                      if record["method"] == "turn/interrupt"]
        assert [record["params"]["turnId"] for record in interrupts] == ["turn-1", "turn-2"]
    finally:
        await _stop_console(process)


@pytest.mark.parametrize(
    ("command", "safe_context", "sensitive_text"),
    [
        (
            "tool --api-key placeholder-cli-value --safe yes",
            "--safe",
            "placeholder-cli-value",
        ),
        (
            "tool --api-key=placeholder-assignment-value --safe=yes",
            "--safe=yes",
            "placeholder-assignment-value",
        ),
        (
            "tool --auth-token 'placeholder quoted value' --safe yes",
            "--safe",
            "placeholder quoted value",
        ),
        (
            "tool --authorization placeholder-header-value --safe yes",
            "--safe",
            "placeholder-header-value",
        ),
        (
            ("tool --header 'Authoriza" "tion: Bear" "er placeholder-http-header-value' "
             "--safe yes"),
            "--safe",
            "placeholder-http-header-value",
        ),
        (
            "OPENAI_API_KEY=placeholder-environment-value tool --safe yes",
            "--safe",
            "placeholder-environment-value",
        ),
        ("tool --api-key", "--api-key", None),
        (
            ("tool https://placeholder-user:"
             "placeholder-uri-value@example.invalid/path --safe yes"),
            "--safe",
            "placeholder-uri-value",
        ),
    ],
)
async def test_cli_secret_forms_are_redacted_without_hiding_safe_context(
    console_workspace,
    command: str,
    safe_context: str,
    sensitive_text: str | None,
) -> None:
    """One realistic tool-event table covers argv, header-like and URI secret forms."""
    root, workspace, binary_dir = console_workspace
    _scenario(
        binary_dir, mode="collaboration", models=["openai/example-model"],
        child_command=command,
    )
    process = await _open_console(workspace)
    try:
        await _exchange(process, {
            "version": 1, "request_id": "start", "method": "start",
            "request": {
                "route_id": "codex:local_session", "prompt": "redact",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        await _event(process, "done")
        events = (root / "home" / ".interact" / "out" / "agents" / "thread-child.jsonl").read_text()
        if sensitive_text is not None:
            assert sensitive_text not in events
        assert safe_context in events
        assert "redacted" in events or command.endswith("--api-key")
    finally:
        await _stop_console(process)


@pytest.mark.parametrize("mode", ["malformed", "known_malformed", "oversized", "eof"])
async def test_protocol_failure_immediately_reaps_owned_provider_process(
    console_workspace, mode: str,
) -> None:
    """The existing failure matrix now checks ownership cleanup before console shutdown."""
    _, workspace, binary_dir = console_workspace
    _scenario(binary_dir, mode=mode, models=["openai/example-model"])
    process = await _open_console(workspace)
    try:
        await _exchange(process, {
            "version": 1, "request_id": "start", "method": "start",
            "request": {
                "route_id": "codex:local_session", "prompt": "fail",
                "selection": {"model": "openai/example-model"},
                "workspace_root": str(workspace),
            },
        })
        await _event(process, "error")
        pid = int((binary_dir / "codex.pid").read_text())
        for _ in range(100):
            if not process_alive(pid):
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("provider process remained live after protocol failure")
        assert (binary_dir / "codex.exit").read_text() == "closed\n"
    finally:
        await _stop_console(process)


def test_transport_and_cli_import_contracts_are_explicit_and_lightweight() -> None:
    """Architecture and entrypoint invariants are executable without importing provider code."""
    transport = Path("src/interact/agents/transport.py")
    assert transport.exists(), "one private capability-typed transport owner must exist"
    host_source = Path("src/interact/agents/host.py").read_text()
    host_tree = ast.parse(host_source)
    imported_modules = {
        node.module
        for node in ast.walk(host_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not imported_modules.intersection({
        "interact.agents.codex_transport",
        "interact.agents.completion_transport",
        "interact.agents.providers",
        "interact.models",
    }), "the host must receive a route-to-transport registry without concrete provider imports"
    assert "run.connection ==" not in host_source
    assert "route.connection ==" not in host_source
    assert "transport_registry" in _ConversationHost.model_fields
    tree = ast.parse(Path("src/interact/cli/app.py").read_text())
    local_imports = [node for node in ast.walk(tree)
                     if isinstance(node, (ast.Import, ast.ImportFrom)) and node.col_offset > 0]
    assert not local_imports


def test_cli_import_and_help_do_not_load_or_discover_conversation_dependencies() -> None:
    """A real interpreter control catches import side effects that an AST cannot observe."""
    environment = {
        **os.environ,
        "UV_OFFLINE": "1",
        "OLLAMA_DISCOVERY": "0",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "INTERACT_MODELS_JSON": "{}",
    }
    imported = subprocess.run(
        [sys.executable, "-c", (
            "import sys; import interact.cli.app; "
            "forbidden={'interact.agents.host','interact.models'}; "
            "loaded=sorted(forbidden.intersection(sys.modules)); "
            "raise SystemExit('loaded:'+','.join(loaded) if loaded else 0)"
        )],
        cwd=PROJECT_ROOT, env=environment, text=True, capture_output=True, timeout=10,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr
    helped = subprocess.run(
        ["uv", "run", "interact", "--help"],
        cwd=PROJECT_ROOT, env=environment, text=True, capture_output=True, timeout=10,
        check=False,
    )
    assert helped.returncode == 0, helped.stderr
    assert "agents" in helped.stdout and "console" not in helped.stderr


@pytest.mark.asyncio
async def test_console_initializes_before_media_cost_discovery(console_workspace) -> None:
    """The real console must answer initialize without contacting the slow media-cost seam."""
    _, workspace, _ = console_workspace
    endpoint = await asyncio.create_subprocess_exec(
        sys.executable,
        str((FIXTURES / "slow_cost_map_server.py").resolve()),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert endpoint.stdout is not None
    port = (await asyncio.wait_for(endpoint.stdout.readline(), timeout=2)).decode().strip()
    environment = dict(os.environ)
    environment.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    environment["LITELLM_MODEL_COST_MAP_URL"] = f"http://127.0.0.1:{port}/cost-map"
    process = await asyncio.create_subprocess_exec(
        "uv", "run", "--project", str(PROJECT_ROOT),
        "interact", "agents", "console",
        "--workspace-root", str(workspace),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        cwd=workspace,
    )
    try:
        response = await _exchange(process, {
            "version": 1,
            "request_id": "startup",
            "method": "initialize",
        })
        assert response["ok"] is True
        assert endpoint.returncode is None
        assert endpoint.stderr is not None
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(endpoint.stderr.readline(), timeout=0.1)
    finally:
        await _stop_console(process)
        endpoint.terminate()
        await endpoint.wait()


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["subscription_quota"], "subscription_quota"),
        (["usage_credit"], "usage_credit"),
        (["metered_api"], "metered_api"),
        (["local_compute"], "local_compute"),
        (["unknown"], "unknown"),
        (["subscription_quota", "metered_api"], "mixed"),
    ],
)
def test_billing_aggregation_preserves_every_charge_path(paths, expected) -> None:
    """One domain table prevents an aggregate from selecting one child's billing claim."""
    runs = [reg.AgentRun(
        run_id=f"run-{index}", provider="synthetic", name="run",
        charge_path=path, cost_certainty="known" if path != "unknown" else "unknown",
    ) for index, path in enumerate(paths)]
    summary = reg.billing_summary(runs)
    assert summary.charge_path == expected
    assert set(summary.paths) == set(paths)


async def test_a_route_that_can_never_work_is_not_offered_as_a_route(tmp_path: Path) -> None:
    """"Why have the routes in vscode if i can't use them?"

    Two of the three "unavailable" rows were MANUFACTURED — the catalog built them for the sole
    purpose of refusing them. Nothing the owner can do makes a policy-blocked consumer session
    available, so it is not a route in any useful sense; rendered beside a real one it reads as a
    third broken feature. `codex · session` stays, because "Codex CLI is not installed" names an
    action he can take.
    """
    host = _ConversationHost(
        workspace_root=tmp_path,
        transport_registry=_TransportRegistry(workspace_root=tmp_path),
        config=Config(),
    )
    catalog = await host.catalog()
    blocked = [route for route in catalog.routes if route.availability == "policy_blocked"]
    assert not blocked, (
        "a row nothing can ever make available belongs in the policy, not the route list: "
        + ", ".join(route.id for route in blocked)
    )
    assert any(route.connection == "local_session" for route in catalog.routes), (
        "the routes that CAN work are untouched"
    )
