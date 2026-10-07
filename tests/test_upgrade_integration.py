"""A real upgrade: runtime N (0.43.0) and N+1 (0.43.1) are built from this checkout and signed with a
test key both builds ship, served over loopback, and a running `galaius mcp` (real MCP server, real
client pipe) and a running `galaius machine connect` (to a websocket stand-in for the server) move
from N to N+1 by themselves: same supervisor pid, same client pipe, new worker on N+1."""

import asyncio
import contextlib
import http.server
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from galaius.machines import MachineConfig, MachineRunner
from galaius.config.settings import Config
from galaius.upgrade.check import UpgradeCheck
from galaius.upgrade.source import ReleaseSigner
from galaius.server_registry import _alive
from galaius.upgrade.store import LiveProcess, Runtime, RuntimeStore

CHECKOUT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get("GALAIUS_CORE_SOURCE", CHECKOUT.parent / "galaius-core"))

pytestmark = [
    pytest.mark.skipif(shutil.which("uv") is None or not (CORE / "pyproject.toml").is_file(), reason="needs uv and a galaius-core checkout (GALAIUS_CORE_SOURCE)"),
    pytest.mark.timeout(600),
]


def snapshot(into: Path, version: str, public_pem: bytes) -> Path:
    """This checkout as a source snapshot at `version`, trusting only the test key."""
    ignore = shutil.ignore_patterns("__pycache__", "build.json")
    into.mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
        shutil.copy2(CHECKOUT / name, into / name)
    shutil.copytree(CHECKOUT / "src", into / "src", ignore=ignore)
    keys = into / "src" / "galaius" / "data" / "release-keys"
    shutil.rmtree(keys)
    keys.mkdir()
    (keys / "test.pub").write_bytes(public_pem)
    pyproject = into / "pyproject.toml"
    pyproject.write_text(re.sub(r'(?m)^version = "[^"]+"', f'version = "{version}"', pyproject.read_text(), count=1))
    return into


def running(store: RuntimeStore) -> dict[int, Path]:
    """Which runtime each live supervisor and worker runs, as they registered it (any OS)."""
    found = {}
    for entry in store.live_path.glob("*.json"):
        with contextlib.suppress(OSError, ValueError):
            live = LiveProcess.model_validate_json(entry.read_bytes())
            if _alive(live.pid):
                found[live.pid] = live.runtime
    return found


def reported_version(runtime: Runtime, environment: dict[str, str]) -> str:
    """What `galaius --version` prints when started from `runtime`'s own interpreter."""
    return subprocess.run(runtime.command(("--version",)), env=environment, capture_output=True, text=True, timeout=120).stdout.strip()


def until(condition, timeout: float, what: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (value := condition()):
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


class MachineChannel:
    """Stands in for the server's `/v1/machine-channel`: records who says hello, as which build."""

    def __init__(self) -> None:
        self.hellos: list[str] = []
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), daemon=True).start()
        ready.wait(10)

    def _run(self, ready: threading.Event) -> None:
        async def handler(connection) -> None:
            async for raw in connection:
                if json.loads(raw).get("type") == "hello":
                    self.hellos.append(connection.request.headers["User-Agent"])

        async def main() -> None:
            async with serve(handler, "127.0.0.1", 0) as server:
                self.port = server.sockets[0].getsockname()[1]
                ready.set()
                await asyncio.Future()

        self.loop.run_until_complete(main())


def test_running_mcp_server_and_machine_connection_upgrade_themselves_from_n_to_n_plus_1(tmp_path: Path, monkeypatch) -> None:
    signer = ReleaseSigner.generated()
    key = tmp_path / "signing.pem"
    key.write_bytes(signer.private_pem())
    core = tmp_path / "core"
    shutil.copytree(CORE, core, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "tests"))
    releases = {}
    for version, minutes in (("0.43.0", 0), ("0.43.1", 5)):
        out = tmp_path / f"release-{version}"
        subprocess.run([sys.executable, str(CHECKOUT / "scripts" / "release.py"), "build", "--source", str(snapshot(tmp_path / f"source-{version}", version, signer.public_pem())),
                        "--core", str(core), "--commit", f"{minutes + 10:07x}", "--released-at", (datetime.now(UTC) - timedelta(minutes=30 - minutes)).isoformat(),
                        "--key", str(key), "--out", str(out)], check=True)
        releases[version] = out

    served = tmp_path / "served"
    published = served / "install" / "release"  # where a Galaius server serves its signed release
    published.mkdir(parents=True)
    handler = type("Handler", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *_: None})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: handler(*args, directory=str(served)))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def publish(version: str) -> None:
        for path in releases[version].iterdir():
            shutil.copy2(path, published / path.name)

    config = tmp_path / "config"
    environment = {**os.environ, "XDG_CONFIG_HOME": str(config), "XDG_DATA_HOME": str(tmp_path / "data"), "GALAIUS_RUNTIMES": str(tmp_path / "runtimes"),
                   "GALAIUS_AUTO_UPGRADE": "true", "GALAIUS_UPGRADE_CHECK_SECONDS": "30", "GALAIUS_REFRESH_LIVE_DATA": "false"}
    for name in ("GALAIUS_SUPERVISE", "GALAIUS_SUPERVISED", "VIRTUAL_ENV", "PYTHONPATH"):
        environment.pop(name, None)
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "GALAIUS_RUNTIMES"):
        monkeypatch.setenv(name, environment[name])
    (config / "galaius").mkdir(parents=True)
    (config / "galaius" / "login-server").write_text(f"http://127.0.0.1:{server.server_port}\n")
    channel = MachineChannel()
    MachineRunner().save(MachineConfig(server_url=f"http://127.0.0.1:{channel.port}", workspace_id="00000000-0000-0000-0000-000000000001",
                                       machine_id="00000000-0000-0000-0000-000000000002", token="t" * 40, permission_ceiling="read_only", working_directory=tmp_path))

    # This computer's first install: runtime N, from the signed server release (as a bootstrap would).
    publish("0.43.0")
    store = RuntimeStore.default()
    check = UpgradeCheck(store=store, keys=signer.keys(), config=Config(auto_upgrade=True, upgrade_pin="", upgrade_check_seconds=30, upgrade_github=False))
    assert "0.43.0" in check.run()
    first = store.active()
    assert first.receipt().packages["galaius"] == "0.43.0"

    mcp = subprocess.Popen(first.command(("mcp",)), stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=environment, cwd=tmp_path)
    machine = subprocess.Popen(first.command(("machine", "connect")), stdin=subprocess.DEVNULL, env=environment, cwd=tmp_path)
    try:
        lines: queue.Queue[dict] = queue.Queue()
        threading.Thread(target=lambda: [lines.put(json.loads(line)) for line in mcp.stdout], daemon=True).start()
        notes: list[dict] = []

        def request(identifier: int, method: str, params: dict) -> dict:
            mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}).encode() + b"\n")
            mcp.stdin.flush()
            deadline = time.time() + 120
            while time.time() < deadline:
                message = lines.get(timeout=deadline - time.time())
                if message.get("id") == identifier:
                    return message
                notes.append(message)
            raise AssertionError(f"no answer to {method}")

        request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "upgrade-test", "version": "0"}})
        mcp.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        mcp.stdin.flush()
        tools_before = request(2, "tools/list", {})["result"]["tools"]
        until(lambda: channel.hellos == ["galaius/0.43.0"], 120, "the machine's hello from N")
        until(lambda: list(running(store).values()).count(first.path) >= 4, 30, "two supervisors and two workers on N")
        before = running(store)

        # N+1 is published; the next check (due now) installs and activates it with nobody restarting anything.
        publish("0.43.1")
        store.schedule(0)
        until(lambda: store.active().path != first.path, 180, "N+1 installed and active")
        second = store.active()
        assert second.receipt().packages["galaius"] == "0.43.1"
        # A command started from N's own interpreter (an installer's `galaius`) runs N+1.
        assert reported_version(first, environment) == "0.43.1"

        until(lambda: any(note.get("method") == "notifications/tools/list_changed" for note in notes) or (notes.append(lines.get(timeout=1)) if not lines.empty() else None), 120, "list_changed")
        tools_after = request(3, "tools/list", {})["result"]["tools"]
        assert {tool["name"] for tool in tools_after} == {tool["name"] for tool in tools_before}
        assert any("upgraded from 0.43.0" in note.get("params", {}).get("data", "") and "0.43.1" in note["params"]["data"] for note in notes)

        until(lambda: channel.hellos[-1:] == ["galaius/0.43.1"], 120, "the machine's hello from N+1")
        # EVERY galaius process ends on N+1: both workers, and both supervisors — the MCP relay
        # replaces itself in place at its quiet point, carrying the client's session over, so nothing
        # is left running N.
        def moved() -> dict[int, Path] | None:
            found = running(store)
            return found if list(found.values()).count(second.path) >= 4 else None

        after = until(moved, 120, "both workers and both supervisors on N+1")
        assert mcp.poll() is None and machine.poll() is None
        assert list(after.values()).count(first.path) == 0  # nothing still runs N
        if sys.platform != "win32":  # replaced in place: the pids a client or service manager holds never change
            assert (after[mcp.pid], after[machine.pid]) == (second.path, second.path)
            assert (before[mcp.pid], before[machine.pid]) == (first.path, first.path)
        # The client's session survived that replacement: this call needs the handshake the relay
        # carried over (a worker that never saw `initialize` answers nothing).
        assert {tool["name"] for tool in request(4, "tools/list", {})["result"]["tools"]} == {tool["name"] for tool in tools_before}
    finally:
        mcp.stdin.close()
        machine.terminate()
        for process in (mcp, machine):
            try:
                process.wait(20)
            except subprocess.TimeoutExpired:
                process.kill()
        server.shutdown()
