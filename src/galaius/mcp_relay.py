"""`galaius mcp` as a client runs it: a relay between the client's stdio and the one galaius MCP
server of this computer (`galaius.server.shared`), standard library only, so a client costs a few
megabytes instead of a whole server. Run as a file (`python -I -S mcp_relay.py ENDPOINT`): no
galaius, pydantic or mcp import is on its path.

It opens a connection to the server named by its endpoint (started detached when nothing listens,
one starter at a time under the endpoint's lock) and says who calls in one hello line: this
process's environment, working folder and pid (`galaius.caller.Caller`). Then it forwards lines
both ways, keeping what a new connection needs: the client's handshake, replayed under a private id.

- While a server starts, the client's handshake and first lists are answered from what the
  launcher wrote ahead (`ahead`), so an agent CLI that waits only a moment finds every tool; the
  server's own tool list is asked once it serves, and a difference is said (`tools/list_changed`).
- The server says `SWITCH` when a newer runtime's server should take this client: it has run
  nothing of this connection that is unanswered, so the relay connects to the endpoint it names,
  replays the handshake and resends what was pending.
- A connection that drops otherwise is a crash: a request in flight is answered with an error
  (it may or may not have run), the relay connects again (a new session) and says so on stderr.

This module is also the one place the wire between relay and server is defined (`HELLO`, `SWITCH`,
`Endpoint`): the server imports it."""

import contextlib
import fcntl
import json
import os
import queue
import socket
import stat
import subprocess
import sys
import threading
import time

#: The relay's first line on a connection: who calls (environment, cwd, pid).
HELLO = "galaius/hello"
#: The server's word that this connection moves to the endpoint in its params.
SWITCH = "galaius/switch"
#: Prefix of the ids the relay gives its own requests (a replayed handshake, a confirmation).
PRIVATE = "galaius-relay-"
#: What of a relay's environment the server it starts keeps: who the user is, his desktop and the folders that
#: key the server (`galaius.paths.UserPaths`), never one caller's run, keys or network route: the server takes its
#: settings and keys from the user's config, and each call its caller's environment from the hello.
STARTER = frozenset({"HOME", "USER", "LOGNAME", "PATH", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR", "DISPLAY", "WAYLAND_DISPLAY",
                     "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "GALAIUS_AGENTS_DIR", "GALAIUS_SUBSCRIPTIONS_DIR", "GALAIUS_RUNTIMES",
                     "GALAIUS_AGENT_CEILING"})
STARTER_PREFIXES = ("LC_", "XDG_", "GALAIUS_SHARED_MCP_")


class Endpoint:
    """Where a runtime's shared server listens (`socket`), how to start it (`start`, run detached),
    where its output goes (`log`) and what to answer ahead while it starts (`ahead`: a file, or None)."""

    def __init__(self, socket: str, start: list[str], log: str, ahead: str | None = None) -> None:
        self.socket, self.start, self.log, self.ahead = socket, start, log, ahead

    @classmethod
    def parsed(cls, raw: str | dict) -> "Endpoint":
        data = json.loads(raw) if isinstance(raw, str) else raw
        return cls(socket=data["socket"], start=list(data["start"]), log=data["log"], ahead=data.get("ahead"))

    def dumped(self) -> dict:
        return {"socket": self.socket, "start": self.start, "log": self.log, "ahead": self.ahead}

    @staticmethod
    def private_folder(socket_path: str) -> None:
        """The socket's folder, made when missing: refused unless it is this user's own, mode 0700, not a link
        (another user's folder could listen in its place and read every caller's environment)."""
        folder = os.path.dirname(socket_path)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        info = os.lstat(folder)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError(f"{folder} is not a folder private to this user (yours, mode 0700, not a link): not using it")

    def dial(self) -> socket.socket:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(self.socket)
        except OSError:
            conn.close()
            raise
        return conn

    def connect(self, wait: float) -> socket.socket:
        """A connection to the server, starting it first when nothing listens (one starter at a
        time: the others wait on the lock, then find it listening). OSError when none answers in `wait`."""
        self.private_folder(self.socket)
        try:
            return self.dial()
        except OSError:
            pass
        with open(self.socket + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return self.dial()
            except OSError:
                pass
            with open(self.log, "ab") as log:
                server = subprocess.Popen(self.start, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd=os.path.expanduser("~"),
                                          env={name: value for name, value in os.environ.items() if name in STARTER or name.startswith(STARTER_PREFIXES)})
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                try:
                    return self.dial()
                except OSError:
                    if server.poll() is not None:
                        break
                    time.sleep(0.05)
        raise OSError(f"no galaius MCP server answered at {self.socket} (see {self.log})")


class Line:
    """One JSON-RPC line: `raw` forwarded byte for byte, `body` its parsed object (None: not one)."""

    def __init__(self, raw: bytes, body: dict | None) -> None:
        self.raw, self.body = raw, body

    @classmethod
    def parsed(cls, raw: bytes) -> "Line":
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        return cls(raw.rstrip(b"\r\n"), body if isinstance(body, dict) else None)

    @classmethod
    def of(cls, body: dict) -> "Line":
        return cls(json.dumps(body, separators=(",", ":")).encode(), body)

    @property
    def method(self) -> str | None:
        return self.body.get("method") if self.body is not None else None

    @property
    def key(self) -> str | None:
        """The id, typed (1 and "1" are different requests)."""
        return json.dumps(self.body["id"]) if self.body is not None and "id" in self.body else None

    @property
    def request(self) -> bool:
        return self.method is not None and self.key is not None

    @property
    def answer(self) -> bool:
        return self.method is None and self.key is not None

    def renamed(self, identifier: str) -> "Line":
        return Line.of({**self.body, "id": identifier})

    def error(self, text: str) -> "Line":
        return Line.of({"jsonrpc": "2.0", "id": self.body["id"], "error": {"code": -32603, "message": text}})


class Ahead:
    """The handshake answer and lists of a server still starting, as its runtime announces them
    (`galaius.mcp_announcement.McpAnnouncement`, written by the launcher with this run's gateway tools)."""

    def __init__(self, announced: dict) -> None:
        self.announced = announced

    @classmethod
    def read(cls, path: str | None) -> "Ahead | None":
        if path is None:
            return None
        try:
            with open(path, "rb") as stream:
                return cls(json.loads(stream.read()))
        except (OSError, ValueError):
            return None
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)  # written for this relay alone

    def answer(self, line: Line) -> Line | None:
        params = line.body.get("params") if isinstance(line.body.get("params"), dict) else {}
        announced = self.announced
        if line.method == "initialize" and isinstance(params.get("protocolVersion"), str):
            versions = announced["protocol_versions"]
            result = {"protocolVersion": params["protocolVersion"] if params["protocolVersion"] in versions else versions[-1],
                      "capabilities": announced["capabilities"], "serverInfo": {"name": announced["server_name"], "version": announced["server_version"]},
                      "instructions": announced["instructions"]}
        elif line.method in announced["lists"] and not params.get("cursor"):
            result = announced["lists"][line.method]
        else:
            return None
        return Line.of({"jsonrpc": "2.0", "id": line.body["id"], "result": result})

    def changed(self, answer: Line) -> bool:
        return answer.body.get("result") != self.announced["lists"].get("tools/list")


class McpRelay:
    """One client's relay. Every input (client line, server line, connection end) is an event on
    one queue, handled by one thread, so no two decisions race."""

    #: Seconds a starting server has to listen.
    start_seconds = 60.0
    #: Connections lost (not switched) within `crash_window` that end the relay.
    crash_limit, crash_window = 3, 600.0

    def __init__(self, endpoint: Endpoint) -> None:
        self.endpoint = endpoint
        self.events: queue.Queue[tuple] = queue.Queue()
        self.out = sys.stdout.buffer
        self.conn: socket.socket | None = None
        self.generation = 0
        self.phase = "handshake"  # handshake | serving | switching
        self.initialize: Line | None = None
        self.initialized: Line | None = None
        self.client_initialized = False
        self.handshake_key: str | None = None
        self.pending: dict[str, Line] = {}
        self.server_requests: set[str] = set()
        self.held: list[Line] = []
        self.ahead = Ahead.read(endpoint.ahead)
        self.confirming: str | None = None
        self.served = False
        self.notice: str | None = None
        self.switch_to: Endpoint | None = None
        self.crashes: list[float] = []

    # ---- plumbing ----------------------------------------------------------------------------

    def serve(self) -> int:
        threading.Thread(target=self._read_client, daemon=True, name="relay-client").start()
        if not self.open(self.endpoint):
            return 1
        while True:
            event = self.events.get()
            if event[0] == "client":
                code = self.from_client(event[1])
            elif event[1] != self.generation:
                code = None  # a connection already left behind
            elif event[2] is None:
                code = self.server_ended()
            else:
                code = self.from_server(event[2])
            if code is not None:
                return code

    def _read_client(self) -> None:
        for raw in iter(sys.stdin.buffer.readline, b""):
            if raw.strip():
                self.events.put(("client", Line.parsed(raw)))
        self.events.put(("client", None))

    def _read_server(self, conn: socket.socket, generation: int) -> None:
        with conn.makefile("rb") as stream:
            try:
                for raw in iter(stream.readline, b""):
                    if raw.strip():
                        self.events.put(("server", generation, Line.parsed(raw)))
            except OSError:
                pass
        self.events.put(("server", generation, None))

    def to_client(self, line: Line) -> None:
        self.out.write(line.raw + b"\n")
        self.out.flush()

    def to_server(self, line: Line) -> None:
        try:
            self.conn.sendall(line.raw + b"\n")
        except OSError:
            pass  # its end arrives as an event and is handled there

    def say(self, text: str) -> None:
        print(f"galaius: {text}", file=sys.stderr, flush=True)

    def open(self, endpoint: Endpoint) -> bool:
        """Connect to `endpoint` (starting its server when needed), say hello, replay the handshake."""
        try:
            conn = endpoint.connect(self.start_seconds)
        except OSError as error:
            self.say(str(error))
            return False
        self.endpoint, self.conn, self.generation = endpoint, conn, self.generation + 1
        self.phase, self.server_requests = "handshake", set()
        threading.Thread(target=self._read_server, args=(conn, self.generation), daemon=True, name=f"relay-server-{self.generation}").start()
        self.to_server(Line.of({"jsonrpc": "2.0", "method": HELLO, "params": {"environment": dict(os.environ), "cwd": os.getcwd(), "pid": os.getpid()}}))
        if self.initialize is not None:
            self.send_initialize()
        return True

    def send_initialize(self) -> None:
        """The client's own handshake the first time; afterwards the same under a private id, whose answer the client never sees."""
        line = self.initialize.renamed(f"{PRIVATE}{self.generation}") if self.client_initialized else self.initialize
        self.handshake_key = line.key
        self.to_server(line)

    # ---- events ------------------------------------------------------------------------------

    def from_client(self, line: Line | None) -> int | None:
        if line is None:  # the client hung up
            self.conn.close()
            return 0
        if line.method == "initialize" and self.initialize is None:
            self.initialize = line
            if self.ahead is not None and (answer := self.ahead.answer(line)) is not None:
                self.to_client(self.first_answer(answer))
                self.client_initialized = True
            if self.phase == "handshake":
                self.send_initialize()
            return None
        if self.initialize is None and line.request:
            self.to_client(Line.of({"jsonrpc": "2.0", "id": line.body["id"], "result": {}}) if line.method == "ping"
                           else Line.of({"jsonrpc": "2.0", "id": line.body["id"], "error": {"code": -32601, "message": f"{line.method} comes after initialize, if at all"}}))
            return None
        if line.method == "notifications/initialized":
            self.initialized = line
            if self.phase == "serving":
                self.to_server(line)
            return None
        if not self.served and self.ahead is not None and line.request and (answer := self.ahead.answer(line)) is not None:
            self.to_client(answer)
            return None
        if self.phase != "serving":
            self.held.append(line)
            return None
        self.deliver(line)
        return None

    def deliver(self, line: Line) -> None:
        if line.request:
            self.pending[line.key] = line
        elif line.answer:
            if line.key not in self.server_requests:
                return  # an answer for a connection that is gone
            self.server_requests.discard(line.key)
        self.to_server(line)

    def from_server(self, line: Line) -> None:
        if line.method == SWITCH and not line.request:
            self.switch_to = Endpoint.parsed(line.body["params"])
            self.phase = "switching"
            self.conn.shutdown(socket.SHUT_RDWR)
            return
        if line.answer and line.key == self.handshake_key and self.phase == "handshake":
            self.handshake_done(line)
            return
        if line.answer and line.key == self.confirming:
            if self.ahead.changed(line):
                self.to_client(Line.of({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}))
            self.ahead, self.confirming = None, None
            return
        if line.answer:
            self.pending.pop(line.key, None)
        elif line.request:
            self.server_requests.add(line.key)
        self.to_client(line)

    def handshake_done(self, answer: Line) -> None:
        refused = "error" in answer.body
        if not self.client_initialized:
            self.to_client(answer if refused else self.first_answer(answer))
            self.client_initialized = not refused
        if self.initialized is not None:
            self.to_server(self.initialized)
        self.phase, self.handshake_key, self.served = "serving", None, True
        if self.ahead is not None:
            confirmation = Line.of({"jsonrpc": "2.0", "id": f"{PRIVATE}ahead-{self.generation}", "method": "tools/list"})
            self.confirming = confirmation.key
            self.to_server(confirmation)
        if self.notice is not None:
            self.to_client(Line.of({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}))
            self.to_client(Line.of({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "logger": "galaius", "data": self.notice}}))
            self.say(self.notice)
            self.notice = None
        held, self.held = self.held, []
        for line in held:
            self.deliver(line)

    def server_ended(self) -> int | None:
        self.conn.close()
        if self.phase == "switching":
            # The server ran nothing of this connection still unanswered: what is pending goes to the next one.
            self.held = [*self.pending.values(), *self.held]
            self.pending, self.notice = {}, f"moved to the galaius MCP server at {self.switch_to.socket}"
            if self.open(self.switch_to):
                return None
            return self.ended(1)
        for request in self.pending.values():
            self.to_client(request.error("galaius's MCP server connection dropped before answering; the call may or may not have run"))
        self.pending = {}
        now = time.monotonic()
        self.crashes = [at for at in self.crashes if now - at < self.crash_window] + [now]
        if len(self.crashes) > self.crash_limit:
            self.say(f"the galaius MCP server dropped {len(self.crashes)} connections in {int(self.crash_window)} s; giving up")
            return self.ended(1)
        self.notice = "the galaius MCP server connection dropped; connected again (a new session: browser sessions it held are gone)"
        return None if self.open(self.endpoint) else self.ended(1)

    def ended(self, code: int) -> int:
        """This relay ends: a request it held for a server is told it never ran."""
        for line in (held for held in self.held if held.request):
            self.to_client(line.error("galaius's MCP server is unreachable; the call did not run"))
        return code

    @staticmethod
    def first_answer(answer: Line) -> Line:
        """The handshake answer as the client sees it: tool-list changes and log messages declared."""
        result = dict(answer.body.get("result") or {})
        capabilities = dict(result.get("capabilities") or {})
        capabilities["tools"] = {**(capabilities.get("tools") or {}), "listChanged": True}
        capabilities.setdefault("logging", {})
        return Line.of({**answer.body, "result": {**result, "capabilities": capabilities}})


if __name__ == "__main__":
    code = McpRelay(Endpoint.parsed(sys.argv[1])).serve()
    sys.stdout.flush()
    os._exit(code)  # the client reader may sit in readline holding the buffer lock
