"""One galaius MCP server per computer: relays (`galaius.mcp_relay`) of different callers share it,
each served as its own caller; a relay outlives its server; a server of an older runtime leaves."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import galaius.mcp_relay as relay_module

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="shared MCP server: Unix sockets")


class Client:
    """An MCP client talking to a relay over its stdio."""

    def __init__(self, endpoint: dict, environment: dict, cwd: Path) -> None:
        self.process = subprocess.Popen([sys.executable, "-I", "-S", relay_module.__file__, json.dumps(endpoint)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, **environment}, cwd=cwd)
        self.next_id = 0

    def send(self, method: str, params: dict | None = None, notify: bool = False) -> int | None:
        body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            self.next_id += 1
            body["id"] = self.next_id
        self.process.stdin.write((json.dumps(body) + "\n").encode())
        self.process.stdin.flush()
        return body.get("id")

    def answer(self, identifier: int, seconds: float = 90) -> dict:
        deadline = time.time() + seconds
        while time.time() < deadline:
            line = self.process.stdout.readline()
            assert line, self.process.stderr.read().decode()[-2000:]
            body = json.loads(line)
            if body.get("id") == identifier:
                return body
        raise TimeoutError(identifier)

    def call(self, method: str, params: dict | None = None) -> dict:
        return self.answer(self.send(method, params))

    def start(self) -> dict:
        answer = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
        self.send("notifications/initialized", notify=True)
        return answer

    def tool(self, name: str, **arguments) -> str:
        return self.call("tools/call", {"name": name, "arguments": arguments})["result"]["content"][0]["text"]

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(15)


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("GALAIUS_RUNTIMES", str(tmp_path / "runtimes"))
    for name in ("GALAIUS_PARENT_RUN_ID", "GALAIUS_RUN_ID", "GALAIUS_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    socket = tmp_path / "s" / "mcp.sock"
    yield {"socket": str(socket), "start": [sys.executable, "-I", "-m", "galaius.server.shared", str(socket)], "log": str(tmp_path / "server.log"), "ahead": None}
    subprocess.run(["pkill", "-f", f"galaius.server.shared {socket}"], check=False)


def serve(endpoint: dict, **environment: str) -> subprocess.Popen:
    """The endpoint's server started here (a relay starts one with the user's environment only), listening."""
    server = subprocess.Popen(endpoint["start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, **environment})
    deadline = time.time() + 60
    while not Path(endpoint["socket"]).exists() and time.time() < deadline:
        time.sleep(0.1)
    return server


def servers(endpoint: dict) -> list[int]:
    found = subprocess.run(["pgrep", "-f", f"galaius.server.shared {endpoint['socket']}"], capture_output=True, text=True).stdout.split()
    return [int(pid) for pid in found]


def test_two_callers_on_one_server_each_see_their_own_identity(endpoint, tmp_path):
    first = Client(endpoint, {"GALAIUS_PARENT_RUN_ID": "run-a", "GALAIUS_SESSION_ID": "conversation-a"}, tmp_path)
    second = Client(endpoint, {"GALAIUS_PARENT_RUN_ID": "run-b", "GALAIUS_SESSION_ID": "conversation-b"}, Path.home())
    try:
        for client in (first, second):
            assert client.start()["result"]["serverInfo"]["name"] == "galaius"
        assert first.tool("agent_list").startswith("Session conversation-a:")
        assert second.tool("agent_list").startswith("Session conversation-b:")
        assert len(servers(endpoint)) == 1
    finally:
        first.close()
        second.close()


def test_a_relay_whose_server_died_connects_again_and_answers(endpoint, tmp_path):
    client = Client(endpoint, {"GALAIUS_SESSION_ID": "conversation-a"}, tmp_path)
    try:
        client.start()
        (server,) = servers(endpoint)
        os.kill(server, signal.SIGKILL)
        time.sleep(1)  # the relay sees its connection end before the next call
        assert client.tool("agent_list").startswith("Session conversation-a:")  # a new session on a new server
        assert servers(endpoint) and servers(endpoint) != [server]
        assert "connected again" in client.process.stderr.read1().decode()
    finally:
        client.close()


@pytest.mark.parametrize("outdated", [True, False])
def test_a_server_with_no_client_leaves_soon_once_another_runtime_is_active(endpoint, tmp_path, monkeypatch, outdated):
    active = tmp_path / "runtimes" / "newer" if outdated else Path(sys.prefix)
    if outdated:
        (active / "bin").mkdir(parents=True)
        (active / "bin" / "python").symlink_to(sys.executable)
    (tmp_path / "runtimes").mkdir(mode=0o700, exist_ok=True)
    (tmp_path / "runtimes").chmod(0o700)
    pointer = tmp_path / "runtimes" / "current.json"
    pointer.write_text(json.dumps({"active": str(active)}))
    pointer.chmod(0o600)
    monkeypatch.setenv("GALAIUS_SHARED_MCP_OUTDATED_SECONDS", "1")
    monkeypatch.setenv("GALAIUS_SHARED_MCP_IDLE_SECONDS", "60")
    server = subprocess.Popen(endpoint["start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        server.wait(25)
    except subprocess.TimeoutExpired:
        pass
    assert server.poll() == (0 if outdated else None)  # left on its own, not crashed
    server.kill()


class FakeServer:
    """A server on a Unix socket that answers one connection by the script it is given."""

    def __init__(self, path: Path, script) -> None:
        import socket as sockets
        import threading
        path.parent.mkdir(parents=True, exist_ok=True)
        self.listener = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.seen: list[dict] = []
        self.thread = threading.Thread(target=self._serve, args=(script,), daemon=True)
        self.thread.start()

    def _serve(self, script) -> None:
        conn, _ = self.listener.accept()
        with conn, conn.makefile("rwb") as stream:
            def read() -> dict:
                body = json.loads(stream.readline())
                self.seen.append(body)
                return body

            def write(body: dict) -> None:
                stream.write(json.dumps(body).encode() + b"\n")
                stream.flush()
            script(read, write)


def test_a_relay_told_to_switch_replays_the_handshake_and_resends_what_never_ran(tmp_path):
    newer = {"socket": str(tmp_path / "new.sock"), "start": ["false"], "log": str(tmp_path / "log"), "ahead": None}

    def older(read, write):
        assert read()["method"] == relay_module.HELLO
        initialize = read()
        write({"jsonrpc": "2.0", "id": initialize["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "galaius", "version": "old"}}})
        assert read()["method"] == "notifications/initialized"
        read()  # the call: never run here
        write({"jsonrpc": "2.0", "method": relay_module.SWITCH, "params": newer})

    def newest(read, write):
        assert read()["method"] == relay_module.HELLO
        initialize = read()
        assert initialize["id"].startswith(relay_module.PRIVATE)  # the client's handshake, under the relay's own id
        write({"jsonrpc": "2.0", "id": initialize["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "galaius", "version": "new"}}})
        assert read()["method"] == "notifications/initialized"
        call = read()
        write({"jsonrpc": "2.0", "id": call["id"], "result": {"content": [{"type": "text", "text": "ran on the newer server"}]}})

    FakeServer(tmp_path / "old.sock", older)
    FakeServer(tmp_path / "new.sock", newest)
    client = Client({"socket": str(tmp_path / "old.sock"), "start": ["false"], "log": str(tmp_path / "log"), "ahead": None}, {}, tmp_path)
    try:
        assert client.start()["result"]["serverInfo"]["version"] == "old"
        assert client.tool("anything") == "ran on the newer server"
    finally:
        client.close()


def test_a_server_keeps_its_clients_and_the_pointer_when_the_newer_runtimes_server_never_answers(endpoint, tmp_path, monkeypatch):
    broken = tmp_path / "runtimes" / "broken"
    (broken / "bin").mkdir(parents=True)
    (broken / "bin" / "python").write_text("#!/bin/sh\nexit 3\n")
    (broken / "bin" / "python").chmod(0o755)
    shared = broken / "lib" / "python3" / "site-packages" / "galaius" / "server"
    shared.mkdir(parents=True)
    (shared / "shared.py").write_text("")  # a build whose clients share a server
    (tmp_path / "runtimes").chmod(0o700)
    pointer = tmp_path / "runtimes" / "current.json"
    pointer.write_text(json.dumps({"active": str(broken)}))
    pointer.chmod(0o600)
    monkeypatch.setenv("GALAIUS_AGENT_CEILING", "0")
    monkeypatch.setenv("GALAIUS_SHARED_MCP_START_SECONDS", "2")
    server = serve(endpoint)
    client = Client(endpoint, {"GALAIUS_SESSION_ID": "conversation-a"}, tmp_path)
    try:
        client.start()
        time.sleep(5)  # it asked the newer runtime's server, which never answered
        assert json.loads(pointer.read_text())["active"] == str(broken)  # never moved by a server
        assert client.tool("agent_list").startswith("Session conversation-a:")  # its client stays served
    finally:
        client.close()
        server.kill()


def test_a_client_an_older_supervisor_hands_over_keeps_its_session(tmp_path):
    initialize = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    held = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "anything", "arguments": {}}}

    def server(read, write):
        assert read()["method"] == relay_module.HELLO
        replayed = read()
        assert replayed["id"].startswith(relay_module.PRIVATE) and replayed["params"] == initialize["params"]
        write({"jsonrpc": "2.0", "id": replayed["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "galaius", "version": "x"}}})
        assert read()["method"] == "notifications/initialized"
        call = read()
        write({"jsonrpc": "2.0", "id": call["id"], "result": {"content": [{"type": "text", "text": "held call ran"}]}})

    FakeServer(tmp_path / "s.sock", server)
    carried = {"initialize": json.dumps(initialize), "initialized": '{"jsonrpc":"2.0","method":"notifications/initialized"}', "client_initialized": True,
               "held": [json.dumps(held)], "from_runtime": str(tmp_path)}
    endpoint = {"socket": str(tmp_path / "s.sock"), "start": ["false"], "log": str(tmp_path / "log"), "ahead": None}
    relay = subprocess.Popen([sys.executable, "-I", "-S", relay_module.__file__, json.dumps(endpoint), json.dumps(carried)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        answers = []
        while not any(answer.get("id") == 7 for answer in answers):
            answers.append(json.loads(relay.stdout.readline()))
        assert next(answer for answer in answers if answer.get("id") == 7)["result"]["content"][0]["text"] == "held call ran"
        assert not any(answer.get("id") == 0 for answer in answers)  # its client saw its handshake answered long ago
    finally:
        relay.stdin.close()
        relay.wait(10)

def test_a_client_leaving_closes_its_own_browser_session_only(endpoint, tmp_path):
    serve(endpoint, GALAIUS_HEADLESS="true")
    first, second = Client(endpoint, {}, tmp_path), Client(endpoint, {}, tmp_path)
    try:
        for client in (first, second):
            client.start()
            assert "[session: caller-" in client.tool("navigate", url="data:text/html,<p>here</p>")
        both = second.tool("session", action="list")
        assert both.count("caller-") == 2
        first.close()
        deadline = time.time() + 15
        while second.tool("session", action="list").count("caller-") != 1 and time.time() < deadline:
            time.sleep(0.5)
        left = second.tool("session", action="list")
        assert left.count("caller-") == 1 and "(yours)" in left
    finally:
        second.close()


def test_a_relay_refuses_a_socket_folder_another_user_could_open(endpoint, tmp_path):
    folder = Path(endpoint["socket"]).parent
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    client = Client(endpoint, {}, tmp_path)
    assert client.process.wait(30) == 1
    assert "not a folder private to this user" in client.process.stderr.read().decode()
    assert not servers(endpoint)
