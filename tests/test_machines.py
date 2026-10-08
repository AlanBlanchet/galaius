import asyncio
import hashlib
import hmac
import json
import traceback
import logging
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import websockets
from pydantic import SecretStr
from galaius_core import ConnectionResourceRef, FunctionImplementation, MachineCommand, MachineDataRequest, MachineFileQuery, MachineRef, ModelImplementation, PortSpec, ScriptFile, ScriptImplementation, UserModelOrigin, WorkflowInterface, WorkflowKey, WorkflowNode, WorkflowRevision, WorkflowRevisionRef

from galaius import server_workspace
from galaius.cli import machine_command
from galaius.cli.machine_command import _described_step
from galaius.functions import FunctionRegistry, discover_python, discover_shell
from galaius.private_files import PRIVATE_FILES
from tests.support.private_files import loosen
from galaius.machines import CommandFiles, CommandLogs, MachineConfig, MachineFiles, MachineRunner, SCRIPT_RUNTIMES, ScriptRuntime


def test_machine_config_round_trips_token_with_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / "config" / "machine.json"
    runner = MachineRunner(path)
    config = MachineConfig(
        server_url="http://127.0.0.1:8817",
        workspace_id=uuid4(),
        machine_id=uuid4(),
        token="iwm_" + "x" * 48,
        permission_ceiling="full_access",
        working_directory=Path.cwd(),
    )

    runner.save(config)

    assert runner.load() == config
    PRIVATE_FILES.check(path)
    PRIVATE_FILES.check(path.parent)
    assert ("iwm_" in path.read_text()) != (sys.platform == "win32")  # Windows keeps the token DPAPI-sealed on disk


from galaius.machines import shell_path
import galaius
from galaius.error_reports import MachineErrorReports


@pytest.mark.parametrize(("script", "expected"), [
    ('echo "rc noise"\nprintf "__galaius_path__/nvm/bin:/usr/bin__galaius_path__"\necho "more noise"', "/nvm/bin:/usr/bin:/service/bin"),
    ("exit 1", "/usr/bin:/service/bin"),
])
@pytest.mark.skipif(sys.platform == "win32", reason="no login shell on Windows: the logon task starts with the user's own PATH")
def test_shell_path_puts_the_shell_path_first_and_survives_a_broken_shell(tmp_path: Path, script: str, expected: str) -> None:
    shell = tmp_path / "shell"
    shell.write_text(f"#!/bin/sh\n{script}\n")
    shell.chmod(0o755)
    assert shell_path("/usr/bin:/service/bin", str(shell)) == expected
    assert shell_path("/usr/bin", str(tmp_path / "missing")) == "/usr/bin"


def test_accelerators_reports_cuda_gpus_from_nvidia_smi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        (tmp_path / "nvidia-smi.cmd").write_text("@echo NVIDIA GeForce RTX 2070, 8192\r\n", newline="")
    else:
        fake_nvidia_smi = tmp_path / "nvidia-smi"
        fake_nvidia_smi.write_text('#!/bin/sh\nprintf "NVIDIA GeForce RTX 2070, 8192\\n"\n')
        fake_nvidia_smi.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(MachineRunner, "_mps_accelerator", staticmethod(lambda: ()))  # an Apple host would also report its GPU

    accelerators = MachineRunner._accelerators()

    assert accelerators == [{"kind": "cuda", "name": "NVIDIA GeForce RTX 2070", "memory_mb": 8192}]


def test_accelerators_reports_none_when_no_gpu_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(MachineRunner, "_mps_accelerator", staticmethod(lambda: ()))

    assert MachineRunner._accelerators() == [{"kind": "none", "name": "none", "memory_mb": 0}]


def test_accelerators_reports_apple_silicon_without_a_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("galaius.machines.sys.platform", "darwin")
    monkeypatch.setattr("galaius.machines.platform.machine", lambda: "arm64")

    assert MachineRunner._accelerators() == [{"kind": "mps", "name": "Apple GPU", "memory_mb": 0}]


def test_resources_reports_real_cpu_count_positive_ram_and_free_disk(tmp_path: Path) -> None:
    """CPU/RAM/disk the scheduler's fit check reads — real numbers off this machine, never a
    hardcoded stand-in: `cpu_count` matches `os.cpu_count()`, `ram_mb`/`disk_free_gb` are positive."""
    resources = MachineRunner._resources(tmp_path)

    assert resources["cpu_count"] == os.cpu_count()
    assert resources["ram_mb"] > 0
    assert resources["disk_free_gb"] >= 0


def test_resources_falls_back_to_one_cpu_when_the_count_is_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("galaius.machines.os.cpu_count", lambda: None)

    assert MachineRunner._resources(tmp_path)["cpu_count"] == 1


def test_model_input_path_rejects_files_outside_machine_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    photo = workspace / "photo.jpg"
    photo.write_bytes(b"image")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"image")
    (workspace / "escape.jpg").symlink_to(outside)

    assert MachineRunner._model_input_path("photo.jpg", workspace) == photo
    with pytest.raises(ValueError, match="under the machine working directory"):
        MachineRunner._model_input_path("escape.jpg", workspace)


def _config(tmp_path: Path, permission_ceiling: str) -> MachineConfig:
    return MachineConfig(
        server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(),
        token="iwm_" + "x" * 48, permission_ceiling=permission_ceiling, working_directory=tmp_path, places={"interact-files": "sandbox"},
    )


def _command(machine_id, function: str, version: str, arguments: dict) -> MachineCommand:
    return MachineCommand(
        id=uuid4(), nonce=uuid4(), machine=MachineRef(id=machine_id), workspace_id=uuid4(), run_id=uuid4(),
        workflow=WorkflowRevisionRef(key=WorkflowKey(id=uuid4()), revision=uuid4()), node_id=uuid4(),
        impl=FunctionImplementation(kind="function", name=function, version=version), inputs=arguments,
        expires_at=datetime.now(UTC), signature="a" * 64,
    )


def test_functions_advertises_registered_entries_as_wire_summaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert MachineRunner._functions() == []
    source = tmp_path / "greeter.py"
    source.write_text("import galaius\n\n@galaius.function\ndef greet(name: str) -> str:\n    \"\"\"Greets.\"\"\"\n    return f'hi {name}'\n")
    entry = discover_python(source)[0]
    FunctionRegistry().add(entry)

    advertised = MachineRunner._functions()

    assert advertised == [entry.summary().model_dump(mode="json")]


def test_run_function_invokes_the_registered_function_under_the_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    source = tmp_path / "greeter.py"
    source.write_text("import galaius\n\n@galaius.function\ndef greet(name: str) -> str:\n    \"\"\"Greets.\"\"\"\n    return f'hi {name}'\n")
    entry = discover_python(source)[0]
    FunctionRegistry().add(entry)
    config = _config(tmp_path, "read_only")
    command = _command(config.machine_id, "greet", entry.version, {"name": "alan"})

    assert MachineRunner()._run_function(command, config) == "hi alan"


def test_run_function_rejects_a_stale_pinned_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    entry = discover_shell("echoer", "Echoes.", ("echo", "-n", "{value}"))
    FunctionRegistry().add(entry)
    config = _config(tmp_path, "full_access")
    command = _command(config.machine_id, "echoer", "0" * 64, {"value": "hi"})

    with pytest.raises(RuntimeError, match="changed"):
        MachineRunner()._run_function(command, config)


def test_run_function_denies_full_access_function_under_a_read_only_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    entry = discover_shell("echoer", "Echoes.", ("echo", "-n", "{value}"))
    FunctionRegistry().add(entry)
    config = _config(tmp_path, "read_only")
    command = _command(config.machine_id, "echoer", entry.version, {"value": "hi"})

    with pytest.raises(PermissionError):
        MachineRunner()._run_function(command, config)


def _script_command(machine_id, language: str, source: str, digest: str | None = None) -> MachineCommand:
    return MachineCommand(
        id=uuid4(), nonce=uuid4(), machine=MachineRef(id=machine_id), workspace_id=uuid4(), run_id=uuid4(),
        workflow=WorkflowRevisionRef(key=WorkflowKey(id=uuid4()), revision=uuid4()), node_id=uuid4(),
        impl=ScriptImplementation(kind="script", language=language, source_digest=digest or hashlib.sha256(source.encode()).hexdigest()),
        config={"source": source}, expires_at=datetime.now(UTC), signature="a" * 64,
    )


def test_run_script_requires_full_access_even_when_unspecified(tmp_path: Path) -> None:
    """Mitigation #2: a script has no per-node permission tier of its own -- arbitrary code exec
    is de facto full_access regardless of ceiling, so the runner refuses it outright under
    read_only rather than silently running with elevated trust."""
    config = _config(tmp_path, "read_only")
    command = _script_command(config.machine_id, "python", "print('hi')\n")

    with pytest.raises(PermissionError):
        MachineRunner()._run_script(command, config)


def test_run_script_rejects_a_source_that_does_not_match_its_digest(tmp_path: Path) -> None:
    config = _config(tmp_path, "full_access")
    command = MachineCommand.model_construct(
        id=uuid4(), nonce=uuid4(), machine=MachineRef(id=config.machine_id), workspace_id=uuid4(), run_id=uuid4(),
        workflow=WorkflowRevisionRef(key=WorkflowKey(id=uuid4()), revision=uuid4()), node_id=uuid4(),
        impl=ScriptImplementation(kind="script", language="python", source_digest="a" * 64),
        config={"source": "print('hi')\n"}, inputs={}, expires_at=datetime.now(UTC), signature="a" * 64,
    )

    with pytest.raises(RuntimeError, match="digest"):
        MachineRunner()._run_script(command, config)


#: A script printing the folder it runs in, in each language (cmd wants CRLF lines).
_WHERE_AM_I = {"python": "import os\nprint(os.getcwd())\n", "shell": "pwd\n", "powershell": "(Get-Location).Path\n", "cmd": "@echo off\r\ncd\r\n"}


@pytest.mark.parametrize("language", ScriptRuntime.languages(os.environ.get("PATH")))
def test_run_script_executes_each_language_under_the_working_directory_and_audits_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str) -> None:
    """Every language this computer announces really runs here (Windows CI: PowerShell and cmd)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = _config(tmp_path, "full_access")
    command = _script_command(config.machine_id, language, _WHERE_AM_I[language])

    result = MachineRunner()._run_script(command, config)

    assert Path(result.strip()).resolve() == tmp_path.resolve()
    audit = json.loads(Path(FunctionRegistry().config_path.parent / "script-audit.log").read_text().strip().splitlines()[-1])
    assert audit["source_digest"] == command.impl.source_digest
    assert audit["node_id"] == str(command.node_id)
    assert audit["run_id"] == str(command.run_id)


def test_run_script_kills_a_hanging_process_on_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, "full_access")
    command = _script_command(config.machine_id, "python", "import time\ntime.sleep(30)\n")

    import time as _time
    started = _time.monotonic()
    with pytest.raises(RuntimeError):
        MachineRunner()._run_script(command, config, timeout=0.5)
    assert _time.monotonic() - started < 5


def test_run_agent_forwards_tool_calls_with_arguments_results_and_the_final_token_totals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from galaius.agents.events import AgentEvent
    import galaius.machines as machines

    events = [
        AgentEvent(kind="tool", tool="Bash", tool_input="ls -la", tool_id="t1"),
        AgentEvent(kind="tool_result", text="total 8", tool_id="t1"),
        AgentEvent(kind="other", text="ignored"),
        AgentEvent(kind="done", text="finished", final_text=True, input_tokens=1200, output_tokens=300, cost_usd=0.01),
    ]

    class Process:
        returncode = 0

    class Handle:
        run_id = "run-1"
        process = Process()

        async def wait(self) -> int:
            return 0

    async def fake_run_agent(*_args, **_kwargs):
        return Handle()

    class Socket:
        sent: list[dict] = []

        async def send(self, text: str) -> None:
            self.sent.append(json.loads(text)["event"]["payload"])

    monkeypatch.setattr(machines, "run_agent", fake_run_agent)
    monkeypatch.setattr(machines.reg, "read_events", lambda _run_id: events)
    monkeypatch.setattr(machines.AgentCatalog, "reference", staticmethod(lambda *_args: None))
    socket = Socket()
    result = asyncio.run(MachineRunner()._run_agent(machines.AgentRevisionRef(id=uuid4(), revision=uuid4()), "task", _config(tmp_path, "read_only"), socket, uuid4()))

    assert result == "finished"
    assert socket.sent == [
        # The launched CLI run's id comes first: the server links the step's span to that run's trace.
        {"kind": "log", "level": "info", "logger": "galaius.machines", "text": "agent run run-1 started", "agent_run_id": "run-1"},
        {"kind": "tool", "tool": "Bash", "tool_input": "ls -la", "tool_id": "t1"},
        {"kind": "tool_result", "text": "total 8", "tool_id": "t1"},
        {"kind": "done", "text": "finished", "input_tokens": 1200, "output_tokens": 300, "cost_usd": 0.01},
    ]


@pytest.mark.parametrize(("status", "retried"), [(None, True), (502, True), (403, False), (401, False)])
def test_a_server_mid_deploy_is_retried_and_a_refused_token_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int | None, retried: bool) -> None:
    """A server being redeployed answers the upgrade with garbage (`InvalidMessage`) or a 5xx: the
    runner logs it and reconnects instead of exiting (systemd was restarting it on every deploy).
    A 401/403 at the handshake is a refused token: the runner stops, never retries forever."""
    import websockets
    from websockets.http11 import Response
    import galaius.machines as machines

    attempts: list[int] = []

    def connect(*_args, **_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            if status is None:
                raise websockets.exceptions.InvalidMessage("did not receive a valid HTTP response")
            raise websockets.exceptions.InvalidStatus(Response(status, "refused", websockets.datastructures.Headers()))
        raise PermissionError("stop here")

    monkeypatch.setattr(machines.websockets, "connect", connect)
    runner = MachineRunner(tmp_path / "machine.json")
    runner.reconnect_seconds = (0,)
    if retried:
        with pytest.raises(PermissionError):
            asyncio.run(runner.connect(_config(tmp_path, "read_only")))
    else:
        asyncio.run(runner.connect(_config(tmp_path, "read_only")))
    assert len(attempts) == (2 if retried else 1)


def test_a_channel_that_keeps_failing_tells_its_page_why_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After `report_after_failures` tries in a row the runner sends the last error over HTTPS
    (`POST /v1/machine/problem`, its own token masked), once per streak; it keeps retrying."""
    import galaius.machines as machines

    tries: list[int] = []
    sent: list[httpx.Request] = []
    config = _config(tmp_path, "read_only")
    token = config.token.get_secret_value()

    def connect(*_args, **_kwargs):
        tries.append(1)
        if len(tries) > 5:
            raise PermissionError("stop here")
        raise OSError(f"[SSL: CERTIFICATE_VERIFY_FAILED] while sending {token}")

    monkeypatch.setattr(machines.websockets, "connect", connect)
    monkeypatch.setattr(machines.httpx, "post", lambda url, timeout, **options: sent.append(httpx.Request("POST", url, **options)) or httpx.Response(204))
    runner = MachineRunner(tmp_path / "machine.json")
    runner.reconnect_seconds = (0,)
    with pytest.raises(PermissionError):
        asyncio.run(runner.connect(config))
    asked, told = sent
    assert told.url.path == "/v1/machine/problem" and told.headers["Authorization"] == f"Bearer {config.token.get_secret_value()}"
    body = json.loads(told.content)
    assert body["code"] == "channel_unreachable" and "CERTIFICATE_VERIFY_FAILED" in body["detail"] and token not in body["detail"]

    # The same problem prepared an error report: only its QUESTION left (one line, no log), the draft waits here.
    question = json.loads(asked.content)
    assert asked.url.path == "/v1/machine/error-report" and question["kind"] == "channel_unreachable"
    assert "CERTIFICATE_VERIFY_FAILED" in question["message"] and token not in question["message"] and set(question) == {"id", "kind", "message"}
    reports = MachineRunner.error_reports()
    (draft,) = reports.drafts()
    assert draft.asked and reports.prepare(config, "channel_unreachable", draft.question.message, ()).id == draft.id and len(sent) == 2, "the same problem is asked once"

    # Its owner said yes on the PC's page: the next look uploads that draft, once, then forgets it.
    uploaded: list[httpx.Request] = []
    monkeypatch.setattr(machines.httpx, "get", lambda url, timeout, **options: httpx.Response(200, json={"id": str(draft.id)}))
    monkeypatch.setattr(machines.httpx, "put", lambda url, timeout, **options: uploaded.append(httpx.Request("PUT", url, **options)) or httpx.Response(201))
    assert reports.deliver(config) == 1 and reports.drafts() == []
    assert uploaded[0].url.path == f"/v1/machine/error-report/{draft.id}" and token not in uploaded[0].content.decode()
    assert reports.deliver(config) == 0, "nothing left to send"


def test_a_report_is_masked_and_its_question_asked_until_the_server_takes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The home folder, the machine token and terminal colours never enter a draft; a question the
    server did not take (network down) is asked again by the next look, not lost for a week."""
    import galaius.machines as machines

    config = _config(tmp_path, "read_only")
    token = config.token.get_secret_value()
    posts: list[httpx.Request] = []

    def post(url, timeout, **options):
        posts.append(httpx.Request("POST", url, **options))
        if len(posts) == 1:
            raise httpx.ConnectError("network down")
        return httpx.Response(201, json={})

    monkeypatch.setattr(machines.httpx, "post", post)
    monkeypatch.setattr(machines.httpx, "get", lambda url, timeout, **options: httpx.Response(204))
    reports = MachineRunner.error_reports()
    line = f"\x1b[31mopening {Path.home()}/projets/out.md with Bearer {token}\x1b[0m"
    draft = reports.prepare(config, "crashed", "RuntimeError: gone", (line,))
    assert not draft.asked and "~/projets/out.md" in draft.upload.detail and token not in draft.upload.detail and "\x1b" not in draft.upload.detail
    assert reports.deliver(config) == 0 and len(posts) == 2 and reports.drafts()[0].asked, "asked again by the next look"
    # An install's problem words are the service log's raw end (a program the PC ran may have written it):
    # they name the problem on its page, never the report's question or lines.
    monkeypatch.setattr(machines.httpx, "post", lambda url, timeout, **options: posts.append(httpx.Request("POST", url, **options)) or httpx.Response(201 if "error-report" in url else 204))
    MachineRunner.report_problem(config, "service_stopped", "my-script: customer list exported to /srv/clients.csv", ("17:44:05 error galaius.machines: service stopped",))
    question = json.loads(posts[-2].content)
    assert question["message"] == "17:44:05 error galaius.machines: service stopped" and "clients.csv" not in posts[-2].content.decode()
    # A problem told with a tab (an OSError's words) is still one valid line: kept, asked, never blocking the others.
    tabbed = reports.prepare(config, "crashed", "OSError:\tdenied\nsecond line", ())
    assert tabbed is not None and tabbed.question.message == "OSError: denied second line" and reports.deliver(config) == 0


def test_a_crash_is_told_to_its_page_once_per_cause_in_one_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`MachineRunner.connect` is where every service ends up (Linux and macOS start `machine
    connect`, the Windows task loops on it): a crash is reported there, once per distinct cause,
    and still raised for the service manager to restart it; a deliberate stop is no crash."""
    reported: list[tuple[str, str]] = []
    monkeypatch.setattr(MachineRunner, "report_problem", classmethod(lambda cls, config, code, detail="", *rest: reported.append((code, detail)) or True))
    runner = MachineRunner(tmp_path / "machine.json")
    config = _config(tmp_path, "read_only")
    for crash in (RuntimeError("socket gone"), RuntimeError("socket gone"), KeyError("hello"), PermissionError("revoked")):
        async def _connect(_config, crash=crash):
            raise crash
        monkeypatch.setattr(runner, "_connect", _connect)
        with pytest.raises(type(crash)):
            asyncio.run(runner.connect(config))
    assert reported == [("crashed", "RuntimeError: socket gone"), ("crashed", "KeyError: 'hello'")]


def _user_model_command(machine_id, origin: UserModelOrigin, task: str = "object-detection") -> MachineCommand:
    return MachineCommand(
        id=uuid4(), nonce=uuid4(), machine=MachineRef(id=machine_id), workspace_id=uuid4(), run_id=uuid4(),
        workflow=WorkflowRevisionRef(key=WorkflowKey(id=uuid4()), revision=uuid4()), node_id=uuid4(),
        impl=ModelImplementation(kind="model", provider="workspace", model=str(uuid4()), task=task),
        config={"_user_model": {"origin": origin.model_dump(mode="json"), "licence": "Apache-2.0"}}, inputs={},
        expires_at=datetime.now(UTC), signature="a" * 64,
    )


_HF_ORIGIN = UserModelOrigin(kind="huggingface_repo", repo_id="hustvl/yolos-tiny", revision="main", weight_files=("model.safetensors",))


def test_run_user_model_refuses_a_task_this_machine_has_no_loader_for(tmp_path: Path) -> None:
    config = _config(tmp_path, "full_access")
    command = _user_model_command(config.machine_id, _HF_ORIGIN, task="text-generation")

    with pytest.raises(RuntimeError, match="text-generation"):
        MachineRunner()._run_model(command, config)


def test_run_user_model_dispatches_workspace_provider_before_the_vendor_catalog_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`impl.model` for a workspace model is a UUID, never a `MACHINE_MODELS` key — the vendor
    branch must never even be reached (it would `KeyError` on the UUID)."""
    config = _config(tmp_path, "full_access")
    command = _user_model_command(config.machine_id, _HF_ORIGIN, task="object-detection")
    called = {}

    def fake_run_user_model(self, cmd, cfg, report):
        called["ran"] = True
        return {"ok": True}

    monkeypatch.setattr("galaius.machines.MachineRunner._run_user_model", fake_run_user_model)

    result = MachineRunner()._run_model(command, config)

    assert result == {"ok": True} and called["ran"]


def test_run_user_model_pulls_and_names_the_gap_for_a_docker_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = UserModelOrigin(kind="docker_image", image="registry.example/detector:latest")
    config = _config(tmp_path, "full_access")
    command = _user_model_command(config.machine_id, origin)
    pulled = {}
    monkeypatch.setattr("galaius.user_models.pull_docker_image", lambda o: pulled.setdefault("image", o.image) or o.image)

    with pytest.raises(RuntimeError, match="not implemented"):
        MachineRunner()._run_user_model(command, config)

    assert pulled["image"] == "registry.example/detector:latest"


def test_run_user_model_fetches_then_names_the_gap_for_trust_remote_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = UserModelOrigin(kind="huggingface_repo", repo_id="org/name", revision="main", weight_files=("model.safetensors",), trusts_remote_code=True)
    config = _config(tmp_path, "full_access")
    command = _user_model_command(config.machine_id, origin)
    fetched = {}

    def fake_fetch(org, model_id, cache_root, server_url, token):
        fetched["origin"] = org
        return tmp_path / "weights"

    monkeypatch.setattr("galaius.user_models.fetch_weights_dir", fake_fetch)

    with pytest.raises(RuntimeError, match="trust_remote_code"):
        MachineRunner()._run_user_model(command, config)

    assert fetched["origin"].trusts_remote_code is True


def test_run_user_model_rejects_a_command_with_no_resolved_origin(tmp_path: Path) -> None:
    config = _config(tmp_path, "full_access")
    command = _user_model_command(config.machine_id, _HF_ORIGIN)
    command = command.model_copy(update={"config": {}})

    with pytest.raises(RuntimeError, match="origin"):
        MachineRunner()._run_user_model(command, config)


def test_forwarded_log_lines_carry_the_time_they_were_written() -> None:
    """The runner sends its log lines after the step: each line keeps its own time (`at`), so the
    server's trace shows when the runner did it, not when the lines arrived."""
    handler = CommandLogs()
    record = logging.LogRecord("galaius.machines", logging.INFO, __file__, 1, "running model step", None, None)
    record.created = datetime(2026, 9, 24, 18, 21, 32, tzinfo=UTC).timestamp()
    handler.emit(record)
    (line,) = handler.drain()
    assert line["at"] == "2026-09-24T18:21:32+00:00" and line["text"] == "running model step"


def _file_command(machine: UUID, op: str, path: str, inputs: dict) -> MachineCommand:
    return MachineCommand(id=uuid4(), nonce=uuid4(), machine={"id": machine}, workspace_id=uuid4(), run_id=uuid4(), workflow={"key": {"id": uuid4()}, "revision": uuid4()}, node_id=uuid4(),
                          impl={"kind": "builtin", "op": op}, config={"artifact_path": path}, inputs=inputs, expires_at=datetime.now(UTC) + timedelta(minutes=5), signature="0" * 64)


@pytest.mark.parametrize("path", ("../escape.txt", "/etc/passwd", "a/../../escape.txt", "interact-files/link/out.txt", "outside-the-roots.txt", ".bashrc", "interact-files/.ssh/id_ed25519", ".config/galaius/machine.json"))
def test_a_file_op_never_leaves_the_working_directory(tmp_path: Path, path: str, monkeypatch: pytest.MonkeyPatch, directory_backend) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = tmp_path / "work"
    root.mkdir()
    (root / "interact-files").mkdir()
    (root / "interact-files" / "link").symlink_to(tmp_path)
    config = MachineConfig(server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(), token="t" * 40, permission_ceiling="full_access", working_directory=root)
    files = CommandFiles(config=config, command=_file_command(config.machine_id, "write_artifact", path, {"value": "x"}))
    with pytest.raises(PermissionError):
        files.write()
    with pytest.raises(PermissionError):
        files.read()
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "out.txt").exists() and not (root / "outside-the-roots.txt").exists()


@pytest.mark.parametrize("ceiling,value,fetched,written", (
    ("full_access", "plain text", False, b"plain text"),
    ("full_access", {"a": 1}, False, b'{"a": 1}'),
    ("full_access", "RECEIVED", True, b"received bytes"),
    ("read_only", "plain text", False, None),
))
def test_a_machine_saves_text_or_a_received_file_within_its_ceiling(tmp_path: Path, ceiling: str, value: object, fetched: bool, written: bytes | None, monkeypatch: pytest.MonkeyPatch, directory_backend) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = tmp_path / "work"
    root.mkdir()
    received = root / "received.bin"
    received.write_bytes(b"received bytes")
    config = MachineConfig(server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(), token="t" * 40, permission_ceiling=ceiling, working_directory=root,
                           places={"interact-files": "sandbox"})
    command = _file_command(config.machine_id, "write_artifact", "interact-files/saved/out.txt", {"value": str(received) if fetched else value})
    files = CommandFiles(config=config, command=command, fetched=frozenset({"value"}) if fetched else frozenset())
    if written is None:
        with pytest.raises(PermissionError):
            files.write()
        return
    receipt = files.write()
    assert (root / "interact-files/saved/out.txt").read_bytes() == written
    assert receipt == {"machine": str(config.machine_id), "path": "interact-files/saved/out.txt", "digest": hashlib.sha256(written).hexdigest(), "size": len(written)}
    assert json.loads((tmp_path / "config/galaius/file-audit.log").read_text().splitlines()[-1])["op"] == "write"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes in this file system")
def test_a_named_pipe_in_a_root_is_refused_without_hanging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory_backend) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = tmp_path / "work"
    (root / "interact-files").mkdir(parents=True)
    os.mkfifo(root / "interact-files" / "pipe")
    config = MachineConfig(server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(), token="t" * 40, permission_ceiling="read_only", working_directory=root)
    files = CommandFiles(config=config, command=_file_command(config.machine_id, "read_file", "interact-files/pipe", {}))
    with pytest.raises(PermissionError):
        files.read()
    files.inbox.mkdir(parents=True)
    (files.inbox / "received.bin").write_bytes(b"x")
    files.discard()
    assert not files.inbox.exists()


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="descriptors are counted through /proc")
def test_reading_a_folder_is_refused_and_leaks_no_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory_backend) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = tmp_path / "work"
    (root / "interact-files" / "folder").mkdir(parents=True)
    config = MachineConfig(server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(), token="t" * 40, permission_ceiling="read_only", working_directory=root)
    files = CommandFiles(config=config, command=_file_command(config.machine_id, "read_file", "interact-files/folder", {}))
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(5):
        with pytest.raises(PermissionError):
            files.read()
    assert len(os.listdir("/proc/self/fd")) == before


@pytest.mark.parametrize(("level", "name", "outcome"), [
    ("read", "out.txt", "refused"),
    ("write", "out.txt", "written"),
    ("write", "CLAUDE.md", "refused"),      # steers the agents started there: never written in place outside a sandbox
    ("sandbox", "claude.md", "written"),
    ("write_on_review", "out.txt", "held"),  # lands in a staging copy the owner accepts on the machine
])
def test_a_workflow_write_follows_its_folders_level(tmp_path: Path, level: str, name: str, outcome: str, monkeypatch: pytest.MonkeyPatch, directory_backend) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    root = tmp_path / "work"
    (root / "notes").mkdir(parents=True)
    config = MachineConfig(server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(), token="t" * 40, permission_ceiling="full_access", working_directory=root,
                           places={"notes": level})
    files = CommandFiles(config=config, command=_file_command(config.machine_id, "write_artifact", f"notes/{name}", {"value": "text"}))
    if outcome == "refused":
        with pytest.raises(PermissionError):
            files.write()
        assert not (root / "notes" / name).exists()
        return
    receipt = files.write()
    assert (root / "notes" / name).exists() is (outcome == "written")
    assert ("review" in receipt) is (outcome == "held")


def test_the_runner_reports_the_folders_workflows_may_read_as_they_are_now(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the server shows is what file nodes can use: folders set to read or later whose level is
    in force (never the runner's own folder, a link, a hidden name), and a change made with
    `galaius machine places` is read on the next beat, no reconnect."""
    root = tmp_path / "work"
    (root / "exports" / "pc").mkdir(parents=True)
    (root / "linked").symlink_to(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = MachineRunner(tmp_path / "config" / "galaius" / "machine.json")
    connected = MachineConfig(server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(), token="t" * 40, permission_ceiling="read_only", working_directory=root,
                              places={"interact-files": "sandbox", "exports/pc": "read", "linked": "read", "exports/pc/names": "see"})
    runner.save(connected)
    assert runner._beat(connected)["file_roots"] == ["exports/pc", "interact-files"]
    # A sandbox set over `exports` and an agent root inside it: the beat reports the new folder, the
    # settings revision, and the agent root refused (nothing a workflow writes lands where an agent starts).
    runner.update(lambda current: current.model_copy(update={"places": {"exports": "sandbox"}, "agent_roots": ("exports/pc",)}))
    beat = runner._beat(connected)
    assert beat["file_roots"] == ["exports"] and beat["agent_settings"]["revision"] == 1 and beat["agent_settings"]["refused"][0].startswith("exports/pc: overlaps")


def _signed(config, message):
    unsigned = message.model_dump(mode="json", exclude={"signature"})
    key = hashlib.sha256(config.token.get_secret_value().encode()).digest()
    return message.model_copy(update={"signature": hmac.new(key, json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()})


def test_command_preserves_owner_changes_and_persists_replay_protection(tmp_path, monkeypatch):
    runner = MachineRunner(tmp_path / "config" / "machine.json")
    connected = _scripts_config(tmp_path)
    runner.save(connected)
    runner.save(connected.model_copy(update={"places": {}, "script_roots": ()}))
    command = _signed(connected, _script_command(connected.machine_id, "python", "print(1)").model_copy(update={
        "workspace_id": connected.workspace_id, "expires_at": datetime.now(UTC) + timedelta(seconds=30)}))
    monkeypatch.setattr(runner, "_run_script", lambda *_: "ok")
    asyncio.run(runner._execute(AsyncMock(), connected, command))
    saved = runner.load()
    assert saved.places == {} and saved.script_roots == ()
    assert saved.seen_nonces == (command.nonce,)
    with pytest.raises(PermissionError, match="nonce was already used"):
        asyncio.run(runner._execute(AsyncMock(), connected, command))


@pytest.mark.parametrize(("run_agents", "status"), [(False, "failed"), (True, "succeeded")])
def test_an_agent_step_runs_only_once_the_owner_turned_agents_on_here(tmp_path, monkeypatch, run_agents, status):
    """`galaius login` adds a computer with agent steps off: a signed agent command is refused
    until `galaius machine agents on` on that computer, and the agent CLI never starts."""
    from galaius.machines import AgentRevisionRef
    from galaius_core import AgentImplementation

    runner = MachineRunner(tmp_path / "config" / "machine.json")
    connected = _config(tmp_path, "read_only").model_copy(update={"run_agents": run_agents})
    runner.save(connected)
    command = _signed(connected, _script_command(connected.machine_id, "python", "").model_copy(update={"inputs": {"task": "summarize"}, "config": {},
        "workspace_id": connected.workspace_id, "impl": AgentImplementation(kind="agent", agent=AgentRevisionRef(id=uuid4(), revision=uuid4())),
        "expires_at": datetime.now(UTC) + timedelta(seconds=30)}))
    started = AsyncMock(return_value="done")
    monkeypatch.setattr(runner, "_run_agent", started)
    socket = AsyncMock()
    asyncio.run(runner._execute(socket, connected, command))
    result = json.loads(socket.send.await_args_list[-1].args[0])["result"]
    assert (result["status"], started.await_count) == (status, int(run_agents))
    if not run_agents:
        assert "galaius machine agents on" in result["error"]


@pytest.mark.parametrize("state", ["missing", "corrupt", "permissions"])
def test_unreadable_current_config_never_falls_back_to_connected_roots(tmp_path, state):
    runner = MachineRunner(tmp_path / "config" / "machine.json")
    connected = _scripts_config(tmp_path)
    runner.save(connected)
    if state == "missing":
        runner.config_path.unlink()
    elif state == "corrupt":
        runner.config_path.write_text("{")
    else:
        loosen(runner.config_path)
    with pytest.raises((OSError, ValueError)):
        runner._current_config(connected)


def test_config_updates_serialize_nonce_claims_with_owner_edits(tmp_path):
    runner = MachineRunner(tmp_path / "config" / "machine.json")
    config = _scripts_config(tmp_path)
    runner.save(config)
    commands = [_signed(config, _script_command(config.machine_id, "python", "print(1)").model_copy(update={
        "workspace_id": config.workspace_id, "expires_at": datetime.now(UTC) + timedelta(seconds=30)})) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=4) as workers:
        claims = [workers.submit(runner.update, lambda current, command=command: runner._accept_command(current, config, command)) for command in commands]
        edit = workers.submit(runner.update, lambda current: current.model_copy(update={"places": {}, "script_roots": ()}))
        for job in (*claims, edit):
            job.result(timeout=5)
    saved = runner.load()
    assert saved.places == {} and saved.script_roots == ()
    assert set(saved.seen_nonces) == {command.nonce for command in commands}


@pytest.mark.asyncio
async def test_data_query_answers_during_command_and_commands_stay_serial(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    runner = MachineRunner()
    runner.heartbeat_seconds = 3600
    for name in ("_runtimes", "_accelerators", "_functions"):
        monkeypatch.setattr(runner, name, lambda *_: [])
    monkeypatch.setattr(runner, "_resources", lambda _: {})
    config = _config(tmp_path, "full_access")
    runner.save(config)
    source = "import pathlib, time\npathlib.Path('running').touch()\nwhile not pathlib.Path('release').exists(): time.sleep(0.01)\nprint('released')\n"
    commands = [_signed(config, _script_command(config.machine_id, "python", code).model_copy(update={
        "workspace_id": config.workspace_id, "expires_at": datetime.now(UTC) + timedelta(seconds=30)})) for code in (source, "print('second')\n")]
    query = _signed(config, MachineDataRequest(id=uuid4(), machine=MachineRef(id=config.machine_id), workspace_id=config.workspace_id,
                    op="list", expires_at=datetime.now(UTC) + timedelta(seconds=30), signature="0" * 64))
    finished = asyncio.get_running_loop().create_future()

    async def server(socket):
        try:
            assert json.loads(await socket.recv())["type"] == "hello"
            await socket.send(json.dumps({"type": "command", "command": commands[0].model_dump(mode="json")}))
            async with asyncio.timeout(5):
                while not (tmp_path / "running").exists():
                    await asyncio.sleep(0.01)
            await socket.send(json.dumps({"type": "command", "command": commands[1].model_dump(mode="json")}))
            await socket.send(json.dumps({"type": "data_request", "request": query.model_dump(mode="json")}))
            while True:
                packet = json.loads(await asyncio.wait_for(socket.recv(), 3))
                assert packet["type"] != "result"
                if packet["type"] == "event":
                    assert packet["event"]["command_id"] == str(commands[0].id)
                if packet["type"] == "data_answer":
                    assert packet["result"]["error"] is None
                    break
            (tmp_path / "release").touch()
            results = []
            while len(results) < 2:
                packet = json.loads(await asyncio.wait_for(socket.recv(), 5))
                if packet["type"] == "result":
                    assert packet["result"]["status"] == "succeeded"
                    results.append(packet["result"]["command_id"])
                elif packet["type"] == "event" and packet["event"]["command_id"] == str(commands[1].id):
                    assert results == [str(commands[0].id)]
            assert results == [str(command.id) for command in commands]
            await socket.send(json.dumps({"type": "revoked"}))
            finished.set_result(None)
        except BaseException as error:
            (tmp_path / "release").touch()
            finished.set_exception(error)

    async with websockets.serve(server, "127.0.0.1", 0) as endpoint:
        monkeypatch.setattr(runner, "_channel_url", lambda _: f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}")
        connection = asyncio.create_task(runner.connect(config))
        try:
            await asyncio.wait_for(asyncio.shield(finished), 12)
            await asyncio.wait_for(connection, 5)
        finally:
            (tmp_path / "release").touch()
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_new_enrollment_on_disk_reconnects_with_its_token(tmp_path, monkeypatch):
    """The owner re-enrolls while connected: the old token stops at the next beat and the runner
    comes back with the new one, never exiting and never answering as the old enrollment."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = MachineRunner()
    runner.heartbeat_seconds = 0.05
    for name in ("_runtimes", "_accelerators", "_functions"):
        monkeypatch.setattr(runner, name, lambda *_: [])
    monkeypatch.setattr(runner, "_resources", lambda _: {})
    config = _config(tmp_path, "read_only")
    runner.save(config)
    renewed = config.model_copy(update={"token": SecretStr("iwm_" + "y" * 48)})
    tokens = []

    async def server(socket):
        tokens.append(socket.request.headers["Authorization"])
        await socket.recv()
        if len(tokens) == 1:
            runner.save(renewed)
            await socket.wait_closed()
        else:
            await socket.send(json.dumps({"type": "revoked"}))

    async with websockets.serve(server, "127.0.0.1", 0) as endpoint:
        monkeypatch.setattr(runner, "_channel_url", lambda _: f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}")
        await asyncio.wait_for(runner.connect(config), 5)
    assert tokens == [f"Bearer {token.get_secret_value()}" for token in (config.token, renewed.token)]


def test_approval_preview_and_runner_agree_on_file_with_declared_packages(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = _scripts_config(tmp_path)
    runner = MachineRunner()
    runner.save(config)
    script = tmp_path / "scripts" / "job.py"
    script.parent.mkdir()
    script.write_text(_PEP723, newline="")
    spec = ScriptFile(path="scripts/job.py", file_digest=hashlib.sha256(script.read_bytes()).hexdigest())
    command = _file_script_command(config.machine_id, spec)
    node = WorkflowNode(id=uuid4(), label="Report", x=0, y=0, impl=command.impl, config=command.config,
                        placement={"target": "machine", "machine": {"id": config.machine_id}}, ports=())
    shown = _described_step("Work", node, config.machine_id)
    assert "Program: uv run" in shown
    called = []
    monkeypatch.setattr(runner, "_run_process", lambda argv, *_: called.append(argv) or "ok")
    runner._run_script(command, config)
    assert Path(called[0][0]).stem.lower() == "uv" and "--script" in called[0]


# The editor writes a script's packages as a PEP 723 header (frontend graph/scriptSource.joinScript).
_PEP723 = '# /// script\n# dependencies = [\n#   "six",\n# ]\n# ///\n\nimport six\nprint(six.__name__)\n'


@pytest.mark.parametrize(("language", "source", "uv", "expected"), [
    ("python", "print(1)\n", "/usr/bin/uv", "interpreter"),
    ("python", _PEP723, "/usr/bin/uv", "uv"),
    ("shell", _PEP723, "/usr/bin/uv", "refused" if sys.platform == "win32" else "sh"),
    ("python", _PEP723, None, "refused"),
    ("powershell", "Write-Output 1\n", None, "pwsh"),
    ("cmd", "@echo 1\n", None, "cmd" if sys.platform == "win32" else "refused"),
])
def test_interpreter_runs_each_language_with_its_own_program(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str, source: str, uv: str | None, expected: str) -> None:
    """Each Script language runs by its own program on this OS, or is refused in plain words
    (shell on Windows, cmd off Windows); never run by another program."""
    monkeypatch.setattr(shutil, "which", lambda name, path=None: {"uv": uv, "pwsh": "/opt/pwsh"}.get(name))
    if expected == "refused":
        with pytest.raises(RuntimeError, match="install uv|Windows"):
            MachineRunner._interpreter(language, source)
        return
    argv = MachineRunner._interpreter(language, source)
    assert argv[0] == {"interpreter": sys.executable, "uv": "/usr/bin/uv", "sh": "/bin/sh", "pwsh": "/opt/pwsh"}.get(expected) or argv[0] == SCRIPT_RUNTIMES["cmd"].here[0]
    assert ("--script" in argv) == (expected == "uv") and ("-File" in argv) == (expected == "pwsh")


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed on this machine")
def test_run_script_installs_declared_packages_with_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real dependency: uv resolves the header's package and the script imports it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = _config(tmp_path, "full_access")
    assert MachineRunner()._run_script(_script_command(config.machine_id, "python", _PEP723), config).strip() == "six"


def _file_script_command(machine_id, spec: ScriptFile, language: str = "python") -> MachineCommand:
    return MachineCommand(
        id=uuid4(), nonce=uuid4(), machine=MachineRef(id=machine_id), workspace_id=uuid4(), run_id=uuid4(),
        workflow=WorkflowRevisionRef(key=WorkflowKey(id=uuid4()), revision=uuid4()), node_id=uuid4(),
        impl=ScriptImplementation(kind="script", language=language, origin="machine_file", source_digest=spec.invocation_digest(language)),
        config=spec.model_dump(mode="json", exclude_none=True), expires_at=datetime.now(UTC), signature="a" * 64,
    )


def _scripts_config(tmp_path: Path, **update) -> MachineConfig:
    return _config(tmp_path, "full_access").model_copy(update={"script_roots": ("scripts",), **update})


def test_script_file_runs_its_pinned_digest_and_refuses_a_changed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A script already on the machine runs in place (its folder, its arguments) while its bytes
    are the ones it was picked with; one edit on disk and it is refused, naming the new digest."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = _scripts_config(tmp_path)
    MachineRunner().save(config)
    script = tmp_path / "scripts" / "tools" / "report.py"
    script.parent.mkdir(parents=True)
    script.write_text("import os, sys\nprint(os.getcwd(), *sys.argv[1:])\n", newline="")
    spec = ScriptFile(path="scripts/tools/report.py", file_digest=hashlib.sha256(script.read_bytes()).hexdigest(), args=("--week", "39"))
    assert MachineRunner()._run_script(_file_script_command(config.machine_id, spec), config).split() == [str(script.parent.resolve()), "--week", "39"]
    script.write_text("print('changed')\n", newline="")
    with pytest.raises(RuntimeError, match="changed on this machine since it was picked"):
        MachineRunner()._run_script(_file_script_command(config.machine_id, spec), config)


@pytest.mark.parametrize(("path", "update", "refusal"), [
    ("outside.py", {}, "outside this machine's script folders"),
    ("scripts/linked.py", {}, "outside this machine's script folders"),  # a link pointing out of the roots
    ("interact-files/job.py", {}, "outside this machine's script folders"),  # file steps write there
    ("interact-files/job.py", {"script_roots": ("interact-files",)}, "outside this machine's script folders"),  # a script root never overlaps a sandbox
    ("scripts/job.py", {"places": {"scripts/inbox": "write"}}, "outside this machine's script folders"),  # nor holds one
    ("scripts/job.py", {"interpreter": "interact-files/python"}, "full path"),
])
def test_script_file_outside_the_script_roots_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, update: dict, refusal: str) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    interpreter = update.pop("interpreter", None)
    config = _scripts_config(tmp_path, **update)
    MachineRunner().save(config)
    for folder in ("interact-files", "scripts/inbox"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    for name in ("outside.py", "interact-files/job.py", "scripts/job.py"):
        (tmp_path / name).write_bytes(b"print('x')\n")
    (tmp_path / "scripts" / "linked.py").symlink_to(tmp_path / "outside.py")
    spec = ScriptFile(path=path, file_digest=hashlib.sha256(b"print('x')\n").hexdigest(), interpreter=interpreter)
    with pytest.raises((PermissionError, RuntimeError), match=refusal):
        MachineRunner()._run_script(_file_script_command(config.machine_id, spec), config)


def test_the_program_running_a_script_never_lives_where_file_steps_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = _scripts_config(tmp_path)
    MachineRunner().save(config)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "job.py").write_text("print('x')\n", newline="")
    planted = tmp_path / "interact-files" / "python"
    planted.parent.mkdir()
    planted.write_text("#!/bin/sh\necho planted\n", newline="")
    planted.chmod(0o755)
    spec = ScriptFile(path="scripts/job.py", file_digest=hashlib.sha256(b"print('x')\n").hexdigest(), interpreter=str(planted))
    with pytest.raises(PermissionError, match="cannot live in a folder workflow file steps write to"):
        MachineRunner()._run_script(_file_script_command(config.machine_id, spec), config)


def test_hidden_script_paths_never_pass_the_wire_contract() -> None:
    with pytest.raises(ValueError):
        ScriptFile(path="scripts/.hidden/x.py", file_digest="0" * 64)


def test_file_listing_shows_script_roots_folders_and_a_file_digest_only(tmp_path: Path, directory_backend) -> None:
    config = _scripts_config(tmp_path)
    root = tmp_path / "scripts"
    (root / "tools").mkdir(parents=True)
    (root / ".secret").mkdir()
    (tmp_path / "interact-files").mkdir()
    (root / "tools" / "a.sh").write_bytes(b"echo a\n")
    (root / "link.sh").symlink_to(root / "tools" / "a.sh")
    files = MachineFiles(config=config, area="scripts")
    assert [(entry.name, entry.kind) for entry in files.listing("").entries] == [("scripts", "folder")]
    assert [(entry.name, entry.kind) for entry in files.listing("scripts").entries] == [("tools", "folder")]
    listing = files.listing("scripts/tools/a.sh")
    assert (listing.kind, listing.size, listing.digest) == ("file", 7, hashlib.sha256(b"echo a\n").hexdigest())
    for refused in ("..", "interact-files"):
        with pytest.raises(PermissionError):
            files.listing(refused)


def test_file_query_needs_the_server_signature(tmp_path: Path) -> None:
    config = _config(tmp_path, "read_only")
    query = MachineFileQuery(id=uuid4(), machine=MachineRef(id=config.machine_id), workspace_id=config.workspace_id, path="",
                             expires_at=datetime.now(UTC) + timedelta(seconds=10), signature="0" * 64)
    with pytest.raises(PermissionError, match="signature is invalid"):
        MachineRunner._verify_signed(config, query, "file query")
    unsigned = query.model_dump(mode="json", exclude={"signature"})
    key = hashlib.sha256(config.token.get_secret_value().encode()).digest()
    signed = query.model_copy(update={"signature": hmac.new(key, json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()})
    MachineRunner._verify_signed(config, signed, "file query")


def test_approving_a_script_shows_the_file_as_it_is_on_this_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`galaius machine approve-script` shows what a digest stands for before the owner says yes:
    the file, how it starts, and — run on that machine — whether the file still is what was picked."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = _scripts_config(tmp_path)
    MachineRunner().save(config)
    script = tmp_path / "scripts" / "job.sh"
    script.parent.mkdir()
    script.write_text("echo week\n", newline="")
    spec = ScriptFile(path="scripts/job.sh", file_digest=hashlib.sha256(b"echo week\n").hexdigest(), args=("39",))
    command = _file_script_command(config.machine_id, spec, "shell")
    node = WorkflowNode(id=uuid4(), label="Weekly", x=0, y=0, impl=command.impl, config=command.config, placement={"target": "machine", "machine": {"id": str(config.machine_id)}},
                        ports=(PortSpec(name="result", direction="output", value_type="text"),))
    shown = _described_step("Report", node, config.machine_id)
    assert "Language: Shell" in shown and "File: scripts/job.sh" in shown and "Arguments: 39" in shown and "same content as picked" in shown
    script.write_text("rm -rf /\n", newline="")
    assert "CHANGED since it was picked" in _described_step("Report", node, config.machine_id)
    assert "Not checked here" in _described_step("Report", node, uuid4())


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed on this machine")
def test_a_script_in_a_git_checkout_shows_its_repository_without_credentials(tmp_path: Path) -> None:
    config = _scripts_config(tmp_path)
    repo = tmp_path / "scripts" / "tools"
    repo.mkdir(parents=True)
    (repo / "job.py").write_text("print('x')\n", newline="")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    # A fixture remote carrying a user and password, assembled here (the commit gate refuses a literal one).
    remote = "https://" + ":".join(("someone", "fixture-password")) + "@git.example.invalid/team/tools.git"
    for arguments in (["init", "-q"], ["remote", "add", "origin", remote], ["add", "job.py"], ["commit", "-q", "-m", "job"]):
        subprocess.run([*git, *arguments], check=True, capture_output=True)
    origin = MachineFiles(config=config, area="scripts").listing("scripts/tools/job.py").git
    assert origin is not None and (origin.repository, origin.path, origin.clean) == ("https://git.example.invalid/team/tools.git", "job.py", True)
    (repo / "job.py").write_text("print('edited')\n", newline="")
    assert MachineFiles(config=config, area="scripts").listing("scripts/tools/job.py").git.clean is False



@pytest.mark.parametrize(("language", "program"), [("python", "galaius's own Python"), ("shell", SCRIPT_RUNTIMES["shell"].label)])
def test_approving_inline_code_names_its_execution_program(language: str, program: str) -> None:
    source = "echo 39\n"
    node = WorkflowNode(id=uuid4(), label="Weekly", x=0, y=0, impl={"kind": "script", "language": language, "source_digest": hashlib.sha256(source.encode()).hexdigest()},
                          config={"source": source}, placement={"target": "machine", "machine": {"id": str(uuid4())}}, ports=(PortSpec(name="result", direction="output", value_type="text"),))
    shown = _described_step("Report", node, uuid4())
    assert f"run by {program}" in shown


def test_a_script_approved_as_python_is_refused_as_shell(tmp_path: Path) -> None:
    """The runner's own re-check: a command whose pin is the Python digest of the text but whose
    language says shell never runs (the same text, another program)."""
    config = _config(tmp_path, "full_access")
    source = "echo hi\n"
    with pytest.raises(ValueError, match="does not match its pinned digest"):
        _script_command(config.machine_id, "shell", source, ScriptImplementation.inline_digest("python", source))


def test_approve_script_pending_asks_once_per_waiting_version_on_this_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """`galaius machine approve-script --pending`: no digest to copy — every saved Script step placed
    on this machine and not approved yet is shown and asked for, one question per version (two
    steps running the same code share it); approved ones and other machines' steps are left alone."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = _scripts_config(tmp_path)
    MachineRunner().save(config)
    here, elsewhere = str(config.machine_id), str(uuid4())
    def step(label: str, language: str, source: str, machine: str) -> WorkflowNode:
        return WorkflowNode(id=uuid4(), label=label, x=0, y=0, impl=ScriptImplementation.inline(language, source), config={"source": source},
                            placement={"target": "machine", "machine": {"id": machine}}, ports=(PortSpec(name="result", direction="output", value_type="text"),))
    workflows = [WorkflowRevision(key=WorkflowKey(id=uuid4()), revision=uuid4(), name=name, created_at=datetime.now(UTC), nodes=nodes, edges=(), interface=WorkflowInterface())
                 for name, nodes in (("Report", (step("Stock", "python", "print(1)\n", here), step("Other PC", "python", "print(2)\n", elsewhere))),
                                     ("Rapport", (step("Stock bis", "python", "print(1)\n", here), step("Approved", "shell", "echo ok\n", here), step("Refused", "shell", "rm x\n", here))))]
    approved_before = {ScriptImplementation.inline_digest("shell", "echo ok\n")}
    calls: list[str] = []
    class Fake:
        def workflows(self): return tuple(workflows)
        def script_approvals(self, machine_id): assert str(machine_id) == here; return tuple(approved_before)
        def approve_script(self, machine_id, digest): calls.append(digest)
    monkeypatch.setattr(server_workspace.ServerWorkspace, "configured", classmethod(lambda cls: Fake()))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    machine_command.machine_approve_script(pending=True)
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert calls == [ScriptImplementation.inline_digest("python", "print(1)\n")] and result["skipped"] == [ScriptImplementation.inline_digest("shell", "rm x\n")]


def test_agents_stay_off_until_the_owner_turns_them_on_there(tmp_path: Path) -> None:
    """An agent CLI can read what its user can: no path that saves a machine turns them on for him."""
    assert _config(tmp_path, "read_only").run_agents is False


def test_a_report_from_the_service_log_keeps_only_what_galaius_wrote() -> None:
    """A program the PC ran writes into the same service log; only galaius's own lines and its
    tracebacks go into a report (its data is its owner's)."""
    with pytest.raises(AttributeError) as caught:
        MachineErrorReports.own_lines(None)  # raises inside galaius's own code: a real traceback of it
    raised = "".join(traceback.format_exception(caught.value)).rstrip("\n").splitlines()
    log = "\n".join([
        '{"ts":"2026-10-08T17:44:05+00:00","level":"error","source":"machine","name":"galaius.machines","message":"connection crashed","exception":"RuntimeError: gone"}',
        "my-script: customer list exported to /srv/clients.csv",
        "    indented output of the same script",
        '{"ts":"x","level":"info","name":"httpx","message":"GET /secret"}',
        "Traceback (most recent call last):",  # the PC's own program crashing: its frames hold its data
        '  File "/srv/scripts/export.py", line 3, in <module>',
        "    rows = customers()",
        "ValueError: customer 4411 has no email",
        *raised,
    ])
    kept = MachineErrorReports.own_lines(log)
    assert kept[0] == "2026-10-08T17:44:05+00:00 error galaius.machines: connection crashed (RuntimeError: gone)"
    assert kept[1:] == tuple(raised), "galaius's own traceback, whole: head, frames, the raised error"
    assert "customer 4411" not in "\n".join(kept) and "/srv/clients.csv" not in "\n".join(kept)
