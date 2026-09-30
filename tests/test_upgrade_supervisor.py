"""The supervisor keeps a client's pipe across runtime swaps, rolls a failed start back, and never
runs a call twice. Runtimes here are fakes whose `python` is a scripted worker (real quiet-point
code); `tests/test_upgrade_integration.py` swaps real installed runtimes."""

import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from interact.upgrade.handoff import Handoff
from interact.upgrade.release import BuildIdentity
from interact.upgrade.store import Runtime, RuntimeReceipt, RuntimeStore, active_interpreter
from interact.upgrade.supervisor import RelayHandover

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake runtimes are shebang scripts")

WORKER = textwrap.dedent('''\
    #!{python}
    import json, os, sys, threading, time, pathlib
    here = pathlib.Path(__file__).resolve().parent.parent
    version = json.loads((here / "installation.json").read_text())["build"]["version"]
    mode = (here / "mode").read_text().strip() if (here / "mode").exists() else ""
    log = here.parent / "starts.log"
    with open(log, "a") as stream:
        stream.write(f"{{version}} {{os.getpid()}}\\n")
    if mode == "crash-on-start":
        sys.exit(3)
    from interact.upgrade.quiet import QuietPoint, UpgradeReady
    quiet = QuietPoint.current()
    if quiet.supervision.mode == "exit":
        while True:
            try:
                quiet.step(False)
            except UpgradeReady:
                sys.exit(75)
            time.sleep(0.05)
    holds = [False]
    def report():
        while True:
            quiet.step(holds[0])
            time.sleep(0.05)
    threading.Thread(target=report, daemon=True).start()
    def send(body):
        sys.stdout.write(json.dumps(body) + "\\n")
        sys.stdout.flush()
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        if "request_id" in message:  # the VS Code console's protocol: a started turn holds the worker
            holds[0] = method == "start" or (holds[0] and method != "cancel")
            send({{"version": 1, "type": "response", "method": method, "request_id": message["request_id"], "ok": True, "who": f"{{version}} {{os.getpid()}}"}})
            continue
        if method == "initialize":
            send({{"jsonrpc": "2.0", "id": message["id"], "result": {{"protocolVersion": message["params"]["protocolVersion"],
                  "capabilities": {{"tools": {{}}}}, "serverInfo": {{"name": "fake", "version": version}}}}}})
        elif method == "tools/call":
            name = message["params"]["name"]
            if name == "crash":
                with open(here.parent / "crash-count", "a") as stream:
                    stream.write("x")
                os._exit(9)
            if name == "slow":
                time.sleep(1.0)
            if name == "hold":
                holds[0] = True
            if name == "release":
                holds[0] = False
            send({{"jsonrpc": "2.0", "id": message["id"], "result": {{"content": [{{"type": "text", "text": f"{{version}} {{os.getpid()}}"}}]}}}})
    ''')


def fake_runtime(store: RuntimeStore, version: str, minutes: int, mode: str = "") -> Runtime:
    path = store.root / f"{version}-fake"
    (path / "bin").mkdir(parents=True)
    python = path / "bin" / "python"
    python.write_text(WORKER.format(python=sys.executable))
    python.chmod(0o755)
    build = BuildIdentity(version=version, released_at=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=minutes), commit=f"{minutes:07x}")
    receipt = RuntimeReceipt(build=build, identity=f"{minutes:064x}", source="local", installed_at=datetime.now(UTC), packages={})
    (path / "installation.json").write_text(receipt.model_dump_json())
    if mode:
        (path / "mode").write_text(mode)
    return Runtime(path=path)


@pytest.fixture
def store(tmp_path: Path) -> RuntimeStore:
    store = RuntimeStore(root=tmp_path / "runtimes")
    store.schedule(10**9)  # no release check during these tests
    return store


class Client:
    """An MCP client holding one pipe to `interact mcp` for the whole test."""

    def __init__(self, store: RuntimeStore, *arguments: str) -> None:
        # The supervisor under test runs this checkout's code, never the (fake) active runtime's.
        environment = {**os.environ, "INTERACT_SUPERVISE": "1", "INTERACT_RUNTIMES": str(store.root), Handoff.child: "1",
                       "INTERACT_SUPERVISOR_TICK_SECONDS": "0.05", "INTERACT_SUPERVISOR_PROBATION_SECONDS": "2", "INTERACT_SUPERVISOR_STOP_SECONDS": "5", "INTERACT_SUPERVISOR_REPLACE_ITSELF": "false"}
        environment.pop("INTERACT_SUPERVISED", None)
        self.process = subprocess.Popen([sys.executable, "-m", "interact", *(arguments or ("mcp",))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=environment)
        self.lines: queue.Queue[dict] = queue.Queue()
        self.notifications: list[dict] = []
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout:
            self.lines.put(json.loads(line))

    def send(self, method: str, params: dict | None = None) -> int:
        self.next_id += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}).encode() + b"\n")
        self.process.stdin.flush()
        return self.next_id

    def answer(self, request: int, timeout: float = 20) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                message = self.lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if message.get("id") == request:
                return message
            self.notifications.append(message)
        raise AssertionError(f"no answer to {request}")

    def call(self, method: str, params: dict | None = None) -> dict:
        return self.answer(self.send(method, params))

    def initialize(self) -> dict:
        answer = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
        self.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.process.stdin.flush()
        return answer

    def tool(self, name: str) -> str:
        return self.call("tools/call", {"name": name, "arguments": {}})["result"]["content"][0]["text"]

    def wait_for(self, method: str, timeout: float = 20) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = next((note for note in self.notifications if note.get("method") == method), None)
            if found is not None:
                return found
            try:
                self.notifications.append(self.lines.get(timeout=0.1))
            except queue.Empty:
                pass
        raise AssertionError(f"no {method}")

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(10)


def test_relay_swaps_the_worker_behind_the_same_client_pipe(store: RuntimeStore) -> None:
    old, new = fake_runtime(store, "0.1.0", 1), fake_runtime(store, "0.2.0", 2)
    store.activate(old)
    client = Client(store)
    answer = client.initialize()
    assert answer["result"]["serverInfo"]["version"] == "0.1.0"
    assert answer["result"]["capabilities"]["tools"]["listChanged"] is True
    first_version, first_pid = client.tool("whoami").split()
    assert first_version == "0.1.0"

    store.activate(new)
    client.wait_for("notifications/tools/list_changed")
    version, pid = client.tool("whoami").split()
    assert (version, pid != first_pid) == ("0.2.0", True)
    assert client.process.poll() is None  # same supervisor, same pipe
    assert any("upgraded from 0.1.0" in note.get("params", {}).get("data", "") for note in client.notifications)
    client.close()


def test_relay_swaps_only_once_every_call_is_answered_and_nothing_is_held(store: RuntimeStore) -> None:
    old, new = fake_runtime(store, "0.1.0", 1), fake_runtime(store, "0.2.0", 2)
    store.activate(old)
    client = Client(store)
    client.initialize()
    assert client.tool("hold").startswith("0.1.0")  # the worker now holds a "browser session"
    slow = client.send("tools/call", {"name": "slow", "arguments": {}})
    store.activate(new)
    assert client.answer(slow)["result"]["content"][0]["text"].startswith("0.1.0")  # answered by the worker that took it
    time.sleep(0.5)
    assert client.tool("whoami").startswith("0.1.0")  # still held: no swap
    assert client.tool("release").startswith("0.1.0")
    client.wait_for("notifications/tools/list_changed")
    assert client.tool("whoami").startswith("0.2.0")
    client.close()


def test_relay_rolls_back_a_runtime_that_fails_to_start_and_says_so(store: RuntimeStore) -> None:
    old, broken = fake_runtime(store, "0.1.0", 1), fake_runtime(store, "0.2.0", 2, mode="crash-on-start")
    store.activate(old)
    client = Client(store)
    client.initialize()
    store.activate(broken)
    warning = client.wait_for("notifications/message")
    assert warning["params"]["level"] == "warning" and "failed to start" in warning["params"]["data"]
    assert client.tool("whoami").startswith("0.1.0")
    pointer = store.pointer()
    assert pointer.active == old.path
    assert broken.receipt().identity in pointer.failed
    assert [event.kind for event in store.events()][-2:] == ["rolled_back", "failed"]
    assert not any(note.get("method") == "notifications/tools/list_changed" or "upgraded" in str(note) for note in client.notifications)
    client.close()


@pytest.mark.parametrize("arguments", [("mcp",), ("machine", "connect")])
def test_a_runtime_this_supervisor_started_with_is_never_blamed_for_exiting_early(store: RuntimeStore, arguments) -> None:
    previous, active = fake_runtime(store, "0.1.0", 1), fake_runtime(store, "0.2.0", 2, mode="crash-on-start")
    store.activate(previous)
    store.activate(active)  # e.g. not signed in yet: exits at once, whatever the build
    client = Client(store, *arguments)
    if arguments == ("mcp",):
        client.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    assert client.process.wait(20) == 3
    assert store.pointer().active == active.path and store.pointer().failed == ()


def test_a_failure_the_previous_runtime_shares_condemns_no_build(store: RuntimeStore) -> None:
    old, new = fake_runtime(store, "0.1.0", 1), fake_runtime(store, "0.2.0", 2, mode="crash-on-start")
    store.activate(old)
    client = Client(store, "machine", "connect")
    deadline = time.time() + 10
    while not (store.root / "starts.log").exists() and time.time() < deadline:
        time.sleep(0.05)
    (old.path / "mode").write_text("crash-on-start")  # from now on this computer breaks both
    store.activate(new)
    assert client.process.wait(20) == 3
    assert store.pointer().failed == ()
    assert "both failed to start" in store.events()[-1].text and "no build is blamed" in store.events()[-1].text


@pytest.mark.skipif(sys.platform != "linux", reason="parent-death signal is Linux")
def test_a_passthrough_worker_ends_with_a_killed_supervisor(store: RuntimeStore) -> None:
    store.activate(fake_runtime(store, "0.1.0", 1))
    client = Client(store, "machine", "connect")
    log = store.root / "starts.log"
    deadline = time.time() + 10
    while not log.exists() and time.time() < deadline:
        time.sleep(0.05)
    worker = int(log.read_text().split()[1])
    client.process.kill()
    while Path(f"/proc/{worker}").exists() and time.time() < deadline + 5:
        time.sleep(0.05)
    assert not Path(f"/proc/{worker}").exists() or "Z" in Path(f"/proc/{worker}/stat").read_text().split()[2]


def test_relay_answers_a_call_whose_worker_crashed_and_never_runs_it_twice(store: RuntimeStore) -> None:
    store.activate(fake_runtime(store, "0.1.0", 1))
    client = Client(store)
    client.initialize()
    answer = client.call("tools/call", {"name": "crash", "arguments": {}})
    assert "error" in answer and "may or may not have run" in answer["error"]["message"]
    assert client.tool("whoami").startswith("0.1.0")  # restarted, same pipe
    assert (store.root / "crash-count").read_text() == "x"
    client.close()


def test_passthrough_worker_leaves_at_its_quiet_point_and_a_failed_start_rolls_back(store: RuntimeStore) -> None:
    old, broken, fixed = fake_runtime(store, "0.1.0", 1), fake_runtime(store, "0.2.0", 2, mode="crash-on-start"), fake_runtime(store, "0.3.0", 3)
    store.activate(old)
    client = Client(store, "machine", "connect")
    log = store.root / "starts.log"

    def starts() -> list[str]:
        return [line.split()[0] for line in log.read_text().splitlines()] if log.exists() else []

    deadline = time.time() + 10
    while starts() != ["0.1.0"] and time.time() < deadline:
        time.sleep(0.05)
    store.activate(broken)
    while len(starts()) < 3 and time.time() < deadline + 10:
        time.sleep(0.05)
    assert store.pointer().active == old.path
    assert starts()[:3] == ["0.1.0", "0.2.0", "0.1.0"]
    store.activate(fixed)
    while "0.3.0" not in starts() and time.time() < deadline + 20:
        time.sleep(0.05)
    assert starts()[-1] == "0.3.0" and client.process.poll() is None
    client.process.terminate()
    client.process.wait(10)


def test_console_relay_swaps_between_turns_on_the_same_extension_pipe(store: RuntimeStore, tmp_path: Path) -> None:
    old, new = fake_runtime(store, "0.1.0", 1), fake_runtime(store, "0.2.0", 2)
    store.activate(old)
    console = Client(store, "agents", "console", "--workspace-root", str(tmp_path))

    def ask(method: str, request: str) -> dict:
        console.process.stdin.write(json.dumps({"version": 1, "request_id": request, "method": method}).encode() + b"\n")
        console.process.stdin.flush()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                message = console.lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if message.get("request_id") == request:
                return message
        raise AssertionError(f"no answer to {request}")

    assert ask("initialize", "r1")["who"].startswith("0.1.0")
    assert ask("start", "r2")["who"].startswith("0.1.0")  # a turn runs
    store.activate(new)
    time.sleep(0.5)
    assert ask("catalog", "r3")["who"].startswith("0.1.0")  # no swap mid-turn
    assert ask("cancel", "r4")["who"].startswith("0.1.0")
    deadline = time.time() + 20
    while not ask("catalog", f"r5-{time.time()}")["who"].startswith("0.2.0"):
        assert time.time() < deadline
        time.sleep(0.1)
    assert console.process.poll() is None
    console.close()


def test_a_relay_carries_its_clients_session_across_becoming_a_newer_runtimes(store: RuntimeStore, monkeypatch) -> None:
    """The relay supervisor replaces itself in place, so its memory is gone: the client's handshake and
    whatever arrived during the drain travel in a file, or the client would be left in a session no
    worker ever saw."""
    store.live_path.mkdir(parents=True, exist_ok=True)
    handover = RelayHandover(initialize='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}', initialized='{"jsonrpc":"2.0","method":"notifications/initialized"}',
                             client_initialized=True, held=('{"jsonrpc":"2.0","id":7,"method":"tools/call"}',), from_runtime=str(store.root / "0.1.0-fake"))
    path = handover.left(store)
    assert path.read_text()
    monkeypatch.setenv(RelayHandover.variable, str(path))
    taken = RelayHandover.taken()
    assert taken == handover
    assert not path.exists()                                  # read once, never left behind
    assert RelayHandover.variable not in os.environ           # and never inherited by a worker
    assert RelayHandover.taken() is None                      # a supervisor nobody handed over to


def test_a_new_long_lived_child_starts_on_the_active_runtime_not_on_its_callers(store: RuntimeStore, monkeypatch) -> None:
    """A dispatcher keeps its runtime until its run ends, so the one it is GIVEN must be the active one:
    started from a supervisor that still ran an older build, it kept that build alive for hours."""
    old, new = fake_runtime(store, "0.1.0", 1), fake_runtime(store, "0.2.0", 2)
    monkeypatch.setenv("INTERACT_RUNTIMES", str(store.root))
    store.activate(old)
    assert active_interpreter() == str(old.python)
    store.activate(new)
    assert active_interpreter() == str(new.python)
    # A store with no runtime (a checkout, a first install): a real interpreter, and never a fake one.
    monkeypatch.setenv("INTERACT_RUNTIMES", str(store.root / "nothing-here"))
    fallback = active_interpreter()
    assert Path(fallback).is_file() and fallback not in {str(old.python), str(new.python)}
