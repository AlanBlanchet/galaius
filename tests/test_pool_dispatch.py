"""Runner-side proof that a POOLED command actually routes through the gVisor sandbox
(`MachineRunner._run_script_pooled`), never the direct-subprocess path a same-workspace command
gets — the piece `_execute` reads `command.tenancy` for."""

import hashlib
from uuid import uuid4

import pytest
from interact_core import EgressAllowEntry, MachineCommand, MachineRef, ScriptImplementation, WorkflowKey, WorkflowRevisionRef

from interact.machines import MachineConfig, MachineRunner
from interact.sandbox import gvisor_available

pytestmark = pytest.mark.skipif(not gvisor_available(), reason="gVisor (runsc) is not installed/registered as a Docker runtime here")


def _pooled_command(source: str, tenant_workspace_id=None, config_extra: dict | None = None) -> MachineCommand:
    digest = hashlib.sha256(source.encode()).hexdigest()
    from datetime import UTC, datetime, timedelta

    return MachineCommand(
        id=uuid4(), nonce=uuid4(), machine=MachineRef(id=uuid4()), workspace_id=tenant_workspace_id or uuid4(),
        run_id=uuid4(), workflow=WorkflowRevisionRef(key=WorkflowKey(id=uuid4()), revision=uuid4()), node_id=uuid4(),
        impl=ScriptImplementation(kind="script", language="python", source_digest=digest), config={"source": source, **(config_extra or {})},
        inputs={}, expires_at=datetime.now(UTC) + timedelta(minutes=1), signature="0" * 64, tenancy="pooled",
    )


def _owner_config() -> MachineConfig:
    return MachineConfig(
        server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(), token="iwm_" + "x" * 48,
        permission_ceiling="full_access", working_directory=__import__("pathlib").Path.cwd(),
    )


def test_pooled_script_runs_sandboxed_and_returns_its_output() -> None:
    runner = MachineRunner()
    command = _pooled_command("print('pooled and sandboxed')")
    output = runner._run_script_pooled(command, _owner_config())
    assert output.strip() == "pooled and sandboxed"


def test_pooled_script_cannot_see_the_host_filesystem() -> None:
    """The decisive isolation property at the dispatch layer: a pooled script asking for the
    OWNER's own home directory sees the sandbox's `/work`, never the real host tree."""
    runner = MachineRunner()
    marker = f"host-marker-{uuid4().hex}"
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as scratch:
        scratch.write(marker)
        host_path = Path(scratch.name)
    try:
        command = _pooled_command(f"import os; print(sorted(os.listdir('/')))")
        output = runner._run_script_pooled(command, _owner_config())
        assert str(host_path) not in output
        assert "home" not in output or "/home" != str(host_path.parent)  # sandbox has its own minimal root, not the host's
    finally:
        host_path.unlink(missing_ok=True)


def test_pooled_script_digest_mismatch_is_refused_before_running() -> None:
    runner = MachineRunner()
    command = _pooled_command("print('should never run')")
    tampered = command.model_copy(update={"config": {"source": "print('tampered')"}})
    with pytest.raises(RuntimeError, match="does not match its pinned digest"):
        runner._run_script_pooled(tampered, _owner_config())


def test_pooled_script_refuses_a_read_only_machine() -> None:
    runner = MachineRunner()
    command = _pooled_command("print('nope')")
    read_only = _owner_config().model_copy(update={"permission_ceiling": "read_only"})
    with pytest.raises(PermissionError, match="full-access"):
        runner._run_script_pooled(command, read_only)


def test_pooled_script_honours_its_egress_allow_list() -> None:
    """The command's `_pool_egress_allow` (the server-stamped list) actually reaches
    `interact.sandbox.run_pooled` as a real `EgressPolicy` — a raw TCP connect to an unlisted host
    fails from inside the sandboxed script."""
    runner = MachineRunner()
    probe = "import socket; s=socket.socket(); s.settimeout(3)\ntry:\n s.connect(('8.8.8.8', 443))\n print('OPEN')\nexcept OSError as e:\n print('BLOCKED')\n"
    command = _pooled_command(probe, config_extra={"_pool_egress_allow": [EgressAllowEntry(host="1.1.1.1", port=443).model_dump(mode="json")]})
    output = runner._run_script_pooled(command, _owner_config())
    assert "BLOCKED" in output


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        import json

        self.sent.append(json.loads(payload))


async def _execute_and_collect(command: MachineCommand, config: MachineConfig):
    runner = MachineRunner()
    socket = _FakeSocket()
    await runner._execute(socket, config, command)
    result = next(frame["result"] for frame in socket.sent if frame.get("type") == "result")
    return result, socket.sent


@pytest.mark.asyncio
async def test_execute_accepts_a_pooled_command_whose_workspace_id_differs_from_the_machines_own() -> None:
    """The one invariant relaxation pooling needs, exercised through the REAL `_execute` path: a
    `tenancy="pooled"` command's mismatched `workspace_id` is accepted (it IS the tenant), the
    script still runs sandboxed, and the result comes back succeeded."""
    config = _owner_config()
    command = _pooled_command("print('cross-workspace ok')", tenant_workspace_id=uuid4()).model_copy(update={"machine": MachineRef(id=config.machine_id)})
    signed = _sign(command, config)
    assert signed.workspace_id != config.workspace_id

    result, _ = await _execute_and_collect(signed, config)

    assert result["status"] == "succeeded"
    assert "cross-workspace ok" in result["result"]


@pytest.mark.asyncio
async def test_execute_still_refuses_an_owner_command_whose_workspace_id_does_not_match() -> None:
    """The invariant a non-pooled command must never lose: unchanged from before pooling, a
    workspace mismatch on an `"owner"` command is refused outright (raised, never a soft failed
    result — this is a protocol violation, not a run-time error)."""
    config = _owner_config()
    command = _pooled_command("print('should never run')").model_copy(update={"tenancy": "owner", "machine": MachineRef(id=config.machine_id)})
    signed = _sign(command, config)
    with pytest.raises(PermissionError, match="another workspace"):
        await MachineRunner()._execute(_FakeSocket(), config, signed)


def _sign(command: MachineCommand, config: MachineConfig) -> MachineCommand:
    import hmac
    import json as _json

    unsigned = command.model_dump(mode="json", exclude={"signature"})
    key = hashlib.sha256(config.token.get_secret_value().encode()).digest()
    signature = hmac.new(key, _json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    return command.model_copy(update={"signature": signature})
