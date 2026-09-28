"""The process a client or service manager starts for a long-lived command, running the real
program as a child WORKER from the active runtime, and swapping that worker when the pointer moves.

The supervisor owns what must never drop: the client's pipe (`StdioRelay`: MCP, the VS Code
console) or the service / terminal slot (`Passthrough`: machine connection, TUI). A worker is
replaced only at its quiet point: a relay decides itself (nothing written to the worker is
unanswered, the worker reports it holds nothing a swap would lose, then its input closes); any other
worker decides and exits `EXIT_UPGRADE`.

Only a runtime this supervisor SWITCHED to is on probation: until it answers the client's replayed
handshake (relay) or stays up `probation_seconds` (passthrough). Failing it rolls the pointer back;
the build is condemned (never installed again) only once the runtime rolled back to starts, so a
failure both share (not signed in, a broken environment) blames this computer, not the release.
A crash is restarted; a message the crashed worker had received is answered with an error, never
sent twice.

Kept small on purpose: it only upgrades itself when its client reconnects."""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from interact.upgrade.store import EXIT_UPGRADE, Runtime, RuntimeStore

if sys.platform == "win32":
    import win32api
    import win32con
    import win32job
elif sys.platform == "linux":
    import ctypes


class SupervisorTimings(BaseModel):
    """Every wait of a supervisor; `INTERACT_SUPERVISOR_<FIELD>` overrides one (tests)."""

    model_config = ConfigDict(frozen=True)
    #: A switched-to passthrough worker up this long has started.
    probation_seconds: float = 30.0
    #: A switched-to relayed worker answers the replayed handshake within this.
    handshake_seconds: float = 120.0
    #: A worker asked to stop is killed after this.
    stop_seconds: float = 15.0
    #: This many crashes within `crash_window_seconds` of a runtime switched to in that window roll it back.
    crash_limit: int = 3
    crash_window_seconds: float = 600.0
    tick_seconds: float = 0.5
    #: What a supervisor claims before starting a release check (the check sets the real next one).
    check_claim_seconds: float = 120.0

    @classmethod
    def from_environment(cls) -> "SupervisorTimings":
        """A bad override is said and ignored: it never stops a long-lived command from starting."""
        prefix = "INTERACT_SUPERVISOR_"
        try:
            return cls.model_validate({name.removeprefix(prefix).lower(): value for name, value in os.environ.items() if name.startswith(prefix)})
        except ValidationError as error:
            print(f"interact: ignoring {prefix}* settings ({error.error_count()} invalid); using the defaults", file=sys.stderr)
            return cls()


class Supervision(BaseModel):
    """What a worker learns from `INTERACT_SUPERVISED`: the runtime it was started from, where to
    report its state (relay) and how it leaves at its quiet point. This is the contract between a
    supervisor (which may run old code for days) and newer workers: versioned, and a worker only
    ever ADDS optional fields to it."""

    model_config = ConfigDict(frozen=True)
    version: Literal[1] = 1
    runtime: Path
    mode: Literal["report", "exit"]
    state: Path | None = None
    variable: ClassVar[str] = "INTERACT_SUPERVISED"

    @classmethod
    def current(cls) -> "Supervision | None":
        raw = os.environ.get(cls.variable)
        if not raw:
            return None
        try:
            return cls.model_validate_json(raw)
        except ValidationError:
            return None


class WorkerState(BaseModel):
    """A relayed worker's report: whether it holds something a swap would lose, and when it looked."""

    model_config = ConfigDict(frozen=True)
    holds: bool
    at: float


class Children:
    """Workers never outlive their supervisor. Windows: each sits in a job closed with this process.
    Linux: a passthrough worker gets SIGTERM when its parent dies (a relayed one ends on its closed
    input). macOS: a killed supervisor can leave a passthrough worker running."""

    def __init__(self) -> None:
        self.job = None
        if sys.platform == "win32":
            self.job = win32job.CreateJobObject(None, "")
            limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)

    @staticmethod
    def options(threaded: bool) -> dict:
        """Popen options binding the child to this process (never a fork hook in a threaded one)."""
        if sys.platform != "linux" or threaded:
            return {}
        return {"preexec_fn": lambda: ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)}  # PR_SET_PDEATHSIG

    def hold(self, process: subprocess.Popen) -> None:
        if self.job is not None:
            handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid)
            win32job.AssignProcessToJobObject(self.job, handle)


class Worker:
    """One started worker: its process, runtime, generation, and whether it is on probation."""

    def __init__(self, process: subprocess.Popen, runtime: Runtime, generation: int, state: Path | None, probation: bool) -> None:
        self.process = process
        self.runtime = runtime
        self.generation = generation
        self.state = state
        self.probation = probation
        self.started = time.time()

    def reported(self) -> WorkerState | None:
        try:
            return WorkerState.model_validate_json(self.state.read_bytes()) if self.state is not None else None
        except (OSError, ValidationError):
            return None

    def stop(self, seconds: float) -> int:
        try:
            code = self.process.wait(seconds)
        except subprocess.TimeoutExpired:
            self.process.kill()
            code = self.process.wait()
        return 128 - code if code < 0 else code  # a signal as a shell reports it (SIGTERM: 143)


class Lifecycle:
    """One supervisor's judgement of its workers: the build suspected after a rollback (condemned
    once the runtime rolled back to starts) and recent crashes of a runtime switched to lately."""

    def __init__(self, owner: "Supervisor") -> None:
        self.owner = owner
        self.store = owner.store
        self.suspect: Runtime | None = None
        self.reason = ""
        self.switched_at: float | None = None
        self.crashes: list[float] = []

    def switched(self) -> None:
        self.switched_at = time.time()
        self.crashes = []

    def failed_start(self, worker: Worker, reason: str) -> Runtime | None:
        """A worker on probation ended before it started: roll back to the runtime before it. None:
        nothing left to try (the runtime rolled back to failed too: this computer, not a release)."""
        if self.suspect is not None:
            self.store.record("failed", f"{self.suspect.label()} and {worker.runtime.label()} both {reason}: the cause is on this computer, no build is blamed")
            self.suspect = None
            return None
        back = self.store.roll_back(worker.runtime, reason)
        if back.path == worker.runtime.path:
            return None
        self.suspect, self.reason = worker.runtime, reason
        self.switched()
        return back

    def started(self, worker: Worker) -> str | None:
        """`worker` passed its probation: a build it replaced after a failed start is condemned now.
        Returns what to tell the person, if anything."""
        worker.probation = False
        if self.suspect is None:
            return None
        self.store.condemn(self.suspect)
        text = f"{self.suspect.label()} {self.reason}; back on {worker.runtime.label()}"
        self.suspect = None
        return text

    def crashed(self, worker: Worker, code: int) -> Runtime | None:
        """Where a crashed worker restarts, or None to give up: a runtime switched to lately that
        keeps crashing is rolled back; one this supervisor started with is not blamed."""
        now = time.time()
        self.crashes = [at for at in self.crashes if at > now - self.owner.timings.crash_window_seconds] + [now]
        if len(self.crashes) < self.owner.timings.crash_limit:
            self.store.record("restarted", f"{worker.runtime.label()} exited {code}; restarting it")
            return worker.runtime
        if self.switched_at is not None and now - self.switched_at < self.owner.timings.crash_window_seconds:
            return self.failed_start(worker, f"crashed {len(self.crashes)} times within {self.owner.timings.crash_window_seconds / 60:.0f} min of starting")
        self.store.record("failed", f"{worker.runtime.label()} exited {code} {len(self.crashes)} times in a row; stopping")
        return None


class Supervisor(BaseModel):
    """Common to every supervised command: start a runtime's worker, schedule release checks.
    `arguments` is the command the worker runs (`mcp`, `machine connect`)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    store: RuntimeStore
    arguments: tuple[str, ...]
    timings: SupervisorTimings
    #: How its workers leave at their quiet point: they report (a relay decides) or exit.
    mode: ClassVar[Literal["report", "exit"]]
    #: Whether this supervisor runs threads while starting workers (no fork hook then).
    threaded: ClassVar[bool] = False

    @staticmethod
    def command(arguments: tuple[str, ...], interactive: bool) -> tuple[str, ...]:
        """The command a command line means: bare `interact` in a terminal is the dashboard."""
        return ("_tui",) if not arguments and interactive else arguments

    @classmethod
    def for_arguments(cls, arguments: tuple[str, ...], interactive: bool) -> "Supervisor | None":
        """The supervisor for this command line, or None: not a long-lived command, already a
        worker (whether or not it can read its supervisor's contract: never a supervisor inside
        a supervisor), a development checkout (unless `INTERACT_SUPERVISE=1`) or `INTERACT_SUPERVISE=0`."""
        command = cls.command(arguments, interactive)
        make = next((make for member, make in LONG_LIVED.items() if command[: len(member)] == member), None)
        wanted = os.environ.get("INTERACT_SUPERVISE", "")
        if make is None or Supervision.variable in os.environ or wanted == "0" or (wanted != "1" and cls.development()):
            return None
        return make(store=RuntimeStore.default(), arguments=command, timings=SupervisorTimings.from_environment())

    @staticmethod
    def development() -> bool:
        """Running from an editable checkout: it tracks its source, nothing to upgrade."""
        try:
            raw = distribution("interact").read_text("direct_url.json")
        except PackageNotFoundError:
            return True  # a source tree nobody installed
        try:
            return bool(raw) and json.loads(raw).get("dir_info", {}).get("editable", False)
        except ValueError:
            return False

    def run(self) -> int:
        raise NotImplementedError

    def environment(self, runtime: Runtime, state: Path | None) -> dict[str, str]:
        supervision = Supervision(runtime=runtime.path, mode=self.mode, state=state)
        return {**os.environ, Supervision.variable: supervision.model_dump_json(), "INTERACT_RUNTIMES": str(self.store.root)}

    def spawn(self, runtime: Runtime, generation: int, children: Children, probation: bool, **stdio) -> Worker:
        state = self.store.live_path / f"{os.getpid()}-{generation}.state.json" if self.mode == "report" else None
        if state is not None:
            state.unlink(missing_ok=True)
        process = subprocess.Popen(runtime.command(self.arguments), env=self.environment(runtime, state), **Children.options(self.threaded), **stdio)
        children.hold(process)
        self.store.register(Runtime.own(), os.getpid())
        self.store.register(runtime, process.pid)
        return Worker(process, runtime, generation, state, probation)

    def check_releases(self, checker: subprocess.Popen | None) -> subprocess.Popen | None:
        """Start `interact upgrade check` from the active runtime when one is due (the newest code
        fetches, verifies and installs; this process only watches the pointer). Its output goes to
        the store's `check.log`, never to a client's terminal."""
        if checker is not None and checker.poll() is None:
            return checker
        if time.time() < self.store.next_check():
            return None
        self.store.schedule(self.timings.check_claim_seconds)
        with open(self.store.root / "check.log", "ab") as log:
            return subprocess.Popen(self.store.active().command(("upgrade", "check", "--background")), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    env={**os.environ, "INTERACT_RUNTIMES": str(self.store.root)})

    def say(self, text: str) -> None:
        print(f"interact: {text}", file=sys.stderr, flush=True)


class Passthrough(Supervisor):
    """A worker that owns the terminal / service slot directly (it inherits stdio) and leaves by
    itself at its quiet point with `EXIT_UPGRADE`."""

    mode: ClassVar[Literal["report", "exit"]] = "exit"
    #: Exit codes that mean "stopped on purpose" (a person's Ctrl-C, a service stop), never a crash.
    stopped: ClassVar[frozenset[int]] = frozenset({0, 130, 143})

    def run(self) -> int:
        children, lifecycle, stop = Children(), Lifecycle(self), threading.Event()
        for name in ("SIGTERM", "SIGHUP"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), lambda *_: stop.set())
        previous_interrupt = signal.signal(signal.SIGINT, signal.SIG_IGN)  # the worker takes Ctrl-C
        generation, checker = 0, None
        worker = self.spawn(self.store.active(), generation, children, probation=False)
        try:
            while True:
                if stop.is_set():
                    worker.process.terminate()
                    return worker.stop(self.timings.stop_seconds)
                checker = self.check_releases(checker)
                if worker.probation and time.time() - worker.started > self.timings.probation_seconds and (text := lifecycle.started(worker)):
                    self.say(text)
                try:
                    worker.process.wait(self.timings.tick_seconds)
                except subprocess.TimeoutExpired:
                    continue
                code, generation = worker.stop(0), generation + 1
                if code == EXIT_UPGRADE:
                    target = self.store.active()
                    lifecycle.switched()
                    self.say(f"switching to {target.label()}")
                elif worker.probation and code not in self.stopped:
                    target = lifecycle.failed_start(worker, f"failed to start (exited {code} after {time.time() - worker.started:.0f} s)")
                elif code in self.stopped:
                    return code
                else:
                    target = lifecycle.crashed(worker, code)
                    time.sleep(1)
                if target is None:
                    return code
                worker = self.spawn(target, generation, children, probation=code == EXIT_UPGRADE or lifecycle.suspect is not None)
        finally:
            signal.signal(signal.SIGINT, previous_interrupt)


class Message(BaseModel):
    """One protocol line as the relay tracks it; `raw` is forwarded byte for byte."""

    model_config = ConfigDict(frozen=True)
    raw: bytes
    body: dict | None

    @classmethod
    def parsed(cls, raw: bytes) -> "Message":
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        return cls(raw=raw.rstrip(b"\r\n"), body=body if isinstance(body, dict) else None)

    @classmethod
    def of(cls, body: dict) -> "Message":
        return cls(raw=json.dumps(body, separators=(",", ":")).encode(), body=body)


class RelayProtocol(BaseModel):
    """What a relay must know of the line protocol it keeps open across workers: which lines are
    requests and answers (and their id), the handshake it replays, what it tells the client."""

    model_config = ConfigDict(frozen=True)
    #: Where a line carries its id.
    identifier: ClassVar[str]
    #: The client's handshake request, replayed into every new worker.
    handshake: ClassVar[str] = "initialize"
    #: The client's note that the handshake finished (sent again to every new worker), if any.
    handshake_done: ClassVar[str | None] = None

    def key(self, message: Message) -> str | None:
        """The id, typed (1 and "1" are different requests)."""
        return json.dumps(message.body[self.identifier]) if message.body is not None and self.identifier in message.body else None

    def method(self, message: Message) -> str | None:
        return message.body.get("method") if message.body is not None else None

    def is_request(self, message: Message) -> bool:
        raise NotImplementedError

    def is_answer(self, message: Message) -> bool:
        raise NotImplementedError

    def renamed(self, message: Message, identifier: str) -> Message:
        return Message.of({**message.body, self.identifier: identifier})

    def refused(self, answer: Message) -> bool:
        raise NotImplementedError

    def first_answer(self, answer: Message) -> Message:
        """The handshake answer as the client sees it the first time."""
        return answer

    def interrupted(self, request: Message, text: str) -> Message:
        raise NotImplementedError

    def after_swap(self) -> list[Message]:
        return []

    def notice(self, text: str, warning: bool) -> list[Message]:
        return []


class McpProtocol(RelayProtocol):
    """MCP over stdio (JSON-RPC 2.0). The client learns of a swap through `tools/list_changed`
    (the relay declares that capability) and a log message; worker-to-client requests exist."""

    identifier: ClassVar[str] = "id"
    handshake_done: ClassVar[str | None] = "notifications/initialized"

    def is_request(self, message: Message) -> bool:
        return self.method(message) is not None and self.key(message) is not None

    def is_answer(self, message: Message) -> bool:
        return self.method(message) is None and self.key(message) is not None

    def refused(self, answer: Message) -> bool:
        return "error" in answer.body

    def first_answer(self, answer: Message) -> Message:
        result = dict(answer.body.get("result") or {})
        capabilities = dict(result.get("capabilities") or {})
        capabilities["tools"] = {**(capabilities.get("tools") or {}), "listChanged": True}
        capabilities.setdefault("logging", {})
        return Message.of({**answer.body, "result": {**result, "capabilities": capabilities}})

    def interrupted(self, request: Message, text: str) -> Message:
        return Message.of({"jsonrpc": "2.0", "id": request.body["id"], "error": {"code": -32603, "message": text}})

    def after_swap(self) -> list[Message]:
        return [Message.of({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})]

    def notice(self, text: str, warning: bool) -> list[Message]:
        return [Message.of({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "warning" if warning else "info", "logger": "interact", "data": text}})]


class ConsoleProtocol(RelayProtocol):
    """The VS Code extension's conversation console (`interact agents console`): newline JSON with
    `request_id`; a conversation survives a new host (it reopens the provider session on the next
    send), so a swap between turns loses nothing."""

    identifier: ClassVar[str] = "request_id"

    def is_request(self, message: Message) -> bool:
        return message.body is not None and "type" not in message.body and self.key(message) is not None

    def is_answer(self, message: Message) -> bool:
        return message.body is not None and message.body.get("type") == "response"

    def refused(self, answer: Message) -> bool:
        return answer.body.get("ok") is False

    def interrupted(self, request: Message, text: str) -> Message:
        return Message.of({"version": 1, "type": "response", "method": self.method(request), "request_id": request.body["request_id"], "ok": False,
                           "error_code": "internal_error", "error": text})



class StdioRelay(Supervisor):
    """A client's pipe kept across workers. The relay decides the quiet point itself (it sees every
    request and answer) and replays the client's handshake into each new worker."""

    mode: ClassVar[Literal["report", "exit"]] = "report"
    threaded: ClassVar[bool] = True
    protocol: RelayProtocol
    handshake_prefix: ClassVar[str] = "interact-supervisor-"

    def run(self) -> int:
        """Never returns: the client-reader thread may sit in `stdin.readline` holding the buffer
        lock, which aborts a normal interpreter shutdown; the exit code leaves directly instead."""
        code = _Relay(self).serve()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)


class _Relay:
    """One relay's state, driven by one thread: every input (client line, worker line, worker
    exit, tick) is an event on one queue, so no two decisions race."""

    def __init__(self, owner: StdioRelay) -> None:
        self.owner = owner
        self.protocol = owner.protocol
        self.timings = owner.timings
        self.lifecycle = Lifecycle(owner)
        self.events: queue.Queue[tuple] = queue.Queue()
        self.children = Children()
        self.out = sys.stdout.buffer
        self.generation = 0
        self.worker: Worker | None = None
        self.phase: Literal["handshake", "serving", "draining"] = "handshake"
        self.initialize: Message | None = None
        self.initialized: Message | None = None
        self.client_initialized = False
        self.handshake_key: str | None = None
        self.pending: dict[str, Message] = {}
        self.worker_requests: set[str] = set()
        self.held: list[Message] = []
        self.notices: list[tuple[str, bool]] = []
        self.swapping_from: Runtime | None = None
        self.last_answer = 0.0
        self.checker: subprocess.Popen | None = None
        self.closing = False
        #: When the current handshake was sent, or the current drain began.
        self.since = 0.0

    # ---- plumbing ----------------------------------------------------------------------------

    def serve(self) -> int:
        threading.Thread(target=self._read, args=(sys.stdin.buffer, ("client",)), daemon=True, name="relay-client").start()
        self.start(self.owner.store.active(), probation=False)
        while True:
            try:
                event = self.events.get(timeout=self.timings.tick_seconds)
            except queue.Empty:
                event = ("tick",)
            if (code := self.handle(event)) is not None:
                return code

    def _read(self, stream, tag: tuple) -> None:
        for line in iter(stream.readline, b""):
            if line.strip():
                self.events.put((*tag, Message.parsed(line)))
        self.events.put((*tag, None))

    def to_client(self, message: Message) -> None:
        self.out.write(message.raw + b"\n")
        self.out.flush()

    def to_worker(self, message: Message) -> None:
        try:
            self.worker.process.stdin.write(message.raw + b"\n")
            self.worker.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass  # its exit arrives as an event and is handled there

    def start(self, runtime: Runtime, probation: bool) -> None:
        self.generation += 1
        self.worker = self.owner.spawn(runtime, self.generation, self.children, probation, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        threading.Thread(target=self._read, args=(self.worker.process.stdout, ("worker", self.generation)), daemon=True, name=f"relay-worker-{self.generation}").start()
        self.phase = "handshake"
        self.worker_requests = set()
        if self.initialize is not None:
            self.send_initialize()

    def send_initialize(self) -> None:
        """The client's own handshake the first time; afterwards the same request under a private
        id, whose answer the client never sees."""
        message = self.protocol.renamed(self.initialize, f"{StdioRelay.handshake_prefix}{self.generation}") if self.client_initialized else self.initialize
        self.handshake_key = self.protocol.key(message)
        self.since = time.time()
        self.to_worker(message)

    # ---- events ------------------------------------------------------------------------------

    def handle(self, event: tuple) -> int | None:
        kind = event[0]
        if kind == "client":
            return self.from_client(event[1])
        if kind == "worker":
            if event[1] == self.generation:
                if event[2] is None:
                    return self.worker_ended()
                self.from_worker(event[2])
            return None
        return self.tick()

    def from_client(self, message: Message | None) -> int | None:
        if message is None:  # the client hung up: so does the worker, then this process
            self.closing = True
            if self.worker is not None:
                self.worker.process.stdin.close()
                self.worker.stop(self.timings.stop_seconds)
            return 0
        method = self.protocol.method(message)
        if method == self.protocol.handshake and self.initialize is None:
            self.initialize = message
            if self.phase == "handshake":
                self.send_initialize()
            return None
        if method is not None and method == self.protocol.handshake_done:
            self.initialized = message
            if self.phase == "serving":
                self.to_worker(message)
            return None
        if self.phase != "serving":
            self.held.append(message)
            return None
        self.deliver(message)
        return None

    def deliver(self, message: Message) -> None:
        key = self.protocol.key(message)
        if self.protocol.is_request(message):
            self.pending[key] = message
        elif self.protocol.is_answer(message):
            if key not in self.worker_requests:
                return  # an answer for a worker that is gone
            self.worker_requests.discard(key)
        self.to_worker(message)

    def from_worker(self, message: Message) -> None:
        key = self.protocol.key(message)
        if self.protocol.is_answer(message) and key == self.handshake_key and self.phase == "handshake":
            self.handshake_done(message)
            return
        if self.protocol.is_answer(message):
            self.pending.pop(key, None)
            self.last_answer = time.time()
        elif self.protocol.is_request(message):
            self.worker_requests.add(key)
        self.to_client(message)

    def handshake_done(self, answer: Message) -> None:
        if self.protocol.refused(answer) and self.worker.probation:
            self.worker.process.kill()  # a handshake the previous runtime accepted: a failed start
            return
        if not self.client_initialized:
            self.to_client(self.protocol.first_answer(answer) if not self.protocol.refused(answer) else answer)
            self.client_initialized = not self.protocol.refused(answer)
        if self.initialized is not None:
            self.to_worker(self.initialized)
        self.phase = "serving"
        self.handshake_key = None
        if (text := self.lifecycle.started(self.worker)) is not None:
            self.notices.append((text, True))
        elif self.swapping_from is not None:
            for message in self.protocol.after_swap():
                self.to_client(message)
            self.notices.append((f"upgraded from {self.swapping_from.label()} to {self.worker.runtime.label()}", False))
        self.swapping_from = None
        for text, warning in self.notices:
            for message in self.protocol.notice(text, warning):
                self.to_client(message)
            self.owner.say(text)
        self.notices = []
        held, self.held = self.held, []
        for message in held:
            self.deliver(message)

    def worker_ended(self) -> int | None:
        worker = self.worker
        code = worker.stop(self.timings.stop_seconds)
        if self.closing:
            return 0
        if self.phase == "draining":
            self.swapping_from = worker.runtime
            self.lifecycle.switched()
            self.start(self.owner.store.active(), probation=True)
            return None
        for request in self.pending.values():  # received, maybe run: answered, never replayed
            self.to_client(self.protocol.interrupted(request, f"interact's worker stopped (exit {code}) before answering; the call may or may not have run"))
        self.pending = {}
        if worker.probation:
            target = self.lifecycle.failed_start(worker, f"failed to start (exited {code} before answering the handshake)")
        elif self.phase == "handshake" or code == 0:
            return code  # it never served, or it ended on its own: as an unsupervised server would
        else:
            target = self.lifecycle.crashed(worker, code)
        if target is None:
            return code or 1
        self.start(target, probation=self.lifecycle.suspect is not None)
        return None

    def tick(self) -> int | None:
        self.checker = self.owner.check_releases(self.checker)
        worker = self.worker
        waited = time.time() - self.since
        if (self.phase == "handshake" and self.handshake_key is not None and worker.probation and waited > self.timings.handshake_seconds) \
                or (self.phase == "draining" and waited > self.timings.stop_seconds):
            worker.process.kill()
        elif self.phase == "serving" and self.quiet() and self.owner.store.active().path != worker.runtime.path:
            self.phase = "draining"  # from here client messages wait in `held`
            self.since = time.time()
            worker.process.stdin.close()
        return None

    def quiet(self) -> bool:
        """Nothing written to the worker is unanswered (either way), and its report, taken after
        its last answer, says it holds nothing a swap would lose."""
        if self.pending or self.worker_requests:
            return False
        state = self.worker.reported()
        return state is not None and not state.holds and state.at > max(self.last_answer, self.worker.started)


#: Every long-lived command, and the supervisor that keeps it.
LONG_LIVED: dict[tuple[str, ...], Callable[..., Supervisor]] = {
    ("mcp",): lambda **held: StdioRelay(**held, protocol=McpProtocol()),
    ("agents", "console"): lambda **held: StdioRelay(**held, protocol=ConsoleProtocol()),
    ("machine", "connect"): Passthrough,
    ("machine", "service", "run"): Passthrough,
    ("_tui",): Passthrough,
}
