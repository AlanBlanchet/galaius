"""Every galaius command runs the ACTIVE runtime's code, whatever program started it.

An installer puts one `galaius` on the computer (uv's tool install) and never touches it again;
upgrades land in the runtime store. So the command line is handed to the active runtime before
anything else runs: POSIX replaces this process (`execv`, same pid: a service manager or terminal
sees nothing), Windows runs it as a child and waits, passing its exit code on, and starts it again
when a supervisor in it asks to become a newer runtime's (`EXIT_HANDOVER`). `galaius upgrade` never
hands off: it is how a person repairs a broken active runtime."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import ClassVar, NoReturn

from pydantic import BaseModel, ConfigDict, TypeAdapter

from galaius.private_files import PRIVATE_FILES
from galaius.upgrade.store import EXIT_HANDOVER, Runtime, RuntimeStore

if sys.platform == "win32":
    import win32api
    import win32con
    import win32job
elif sys.platform == "linux":
    import ctypes


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


class Handoff(BaseModel):
    model_config = ConfigDict(frozen=True)
    store: RuntimeStore
    #: Set in the child a Windows parent runs, to that parent's pid: it already is the active
    #: runtime's, and knows which process waits for it (a venv's python.exe is a launcher that runs
    #: the interpreter as ITS child, so the interpreter's own parent pid is the launcher's).
    child: ClassVar[str] = "GALAIUS_HANDED_OFF"
    #: Commands that always run the code they were started with.
    own_code: ClassVar[frozenset[str]] = frozenset({"upgrade"})
    #: The variables a Windows child hands its successor (`replace`), as written.
    carried: ClassVar[TypeAdapter[dict[str, str]]] = TypeAdapter(dict[str, str])

    def target(self, command: tuple[str, ...], long_lived: bool) -> Runtime | None:
        """Where this command line continues, or None to run it here. A supervisor handover that
        never started is rolled back first (said on stderr)."""
        if (command[:1] and command[0] in self.own_code) or os.environ.get(self.child):
            return None
        if (back := self.store.failed_handover()) is not None:
            print(f"galaius: the newest runtime never started as a supervisor; back on {back.label()}", file=sys.stderr)
        active = self.store.active()
        if active.path != Runtime.own().path:
            return active
        return active if long_lived and sys.platform == "win32" else None

    @staticmethod
    def flushed() -> None:
        """Pending output written before this process is replaced (none under pythonw: no streams)."""
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.flush()

    def carried_path(self, parent: int) -> Path:
        """Where a Windows child leaves the variables its successor starts with, for `parent`."""
        return self.store.root / f"handover-environment-{parent}.json"

    def continue_in(self, runtime: Runtime, command: tuple[str, ...]) -> NoReturn:
        self.flushed()
        if sys.platform != "win32":
            os.execv(runtime.python, runtime.command(command))
        base, children = {**os.environ, self.child: str(os.getpid())}, Children()
        environment, carried = base, self.carried_path(os.getpid())
        while True:
            process = subprocess.Popen(runtime.command(command), env=environment)
            children.hold(process)  # stopping this process (a logon task stop) stops the child too
            if (code := process.wait()) != EXIT_HANDOVER:
                raise SystemExit(code)
            try:
                environment = {**base, **self.carried.validate_json(PRIVATE_FILES.read_text(carried))}
            except (OSError, ValueError) as error:
                print(f"galaius: the newer runtime starts without what the older one handed over ({error})", file=sys.stderr)
                environment = base
            carried.unlink(missing_ok=True)
            runtime = self.store.active()

    def replace(self, runtime: Runtime, command: tuple[str, ...], carried: dict[str, str] | None = None) -> NoReturn:
        """A running supervisor becomes `runtime`'s, started with `carried` added to its environment:
        in place on POSIX, through its waiting parent on Windows. Remembered until the new one starts
        (`RuntimeStore.failed_handover`). Like `execv`, it leaves without an interpreter shutdown: a
        thread reading stdin holds its buffer lock and would abort one with another exit code."""
        self.store.hand_over(runtime)
        self.flushed()
        if sys.platform != "win32":
            os.environ.update(carried or {})
            os.execv(runtime.python, runtime.command(command))
        PRIVATE_FILES.write_text(self.carried_path(int(os.environ[self.child])), json.dumps(carried or {}))
        os._exit(EXIT_HANDOVER)
