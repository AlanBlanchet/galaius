"""A test signals only processes it started (POSIX). 2026-11-08: a test stopped a run record holding pid 1; the code under
test sent `killpg(1)`, which is `kill(-1)`: every process of the user, and his desktop session ended three times.

`SignalGuard` stands in for `os.kill` / `os.killpg` while a test runs (`tests/conftest.py`): a probe (signal 0) and a
signal to this test process, a process it started (at any depth) or a group one of them leads go through; one to pid 1
or below, to this process's own group, or (Linux) to a live process it did not start is never sent: the call raises
`PermissionError` (an `OSError`, as the kernel's own refusal) and the test fails at teardown naming it. A pid no process
holds raises `ProcessLookupError`, as it would, nothing sent: rows registering made-up pids stay valid. `alive_pid` is
the running process such rows use instead: a sleeping child of this test process, started again once a stop ended it."""

import atexit
import errno
import os
import subprocess
import sys
from collections.abc import Callable

#: How many parents up a started process may sit (a vendor CLI's tool's subprocess is a few).
DEPTH = 32
#: Whose child a process is, read from /proc (Linux, this PC and the CI runner). Elsewhere a live process it did not
#: start is not told apart (asking `ps` runs a program, which tests emptying PATH or standing in for Popen refuse);
#: pid 1 and below and this process's own group stay refused.
LINEAGE = sys.platform.startswith("linux")

_sleeper: subprocess.Popen | None = None


def alive_pid() -> int:
    """A running process this test process started, in a group of its own: a stop ending it ends nothing else."""
    global _sleeper
    if _sleeper is None or _sleeper.poll() is not None:
        _sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(86400)"], start_new_session=True,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        atexit.register(_sleeper.kill)   # never outlives the test run
    return _sleeper.pid


def _parent(pid: int) -> int | None:
    """`pid`'s parent, read from /proc; None once it is gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat:
            return int(stat.read().rsplit(b")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


class SignalGuard:
    def __init__(self, kill: Callable[[int, int], None], killpg: Callable[[int, int], None]) -> None:
        self._kill, self._killpg = kill, killpg
        self.me, self.group = os.getpid(), os.getpgrp()
        #: Each signal refused, in words, for the failing test's report.
        self.refused: list[str] = []

    def _started(self, pid: int) -> bool:
        if not LINEAGE:
            return pid > 1
        for _ in range(DEPTH):
            if pid == self.me:
                return True
            if pid <= 1 or (pid := _parent(pid)) is None:
                return False
        return False

    def _exists(self, pid: int) -> bool:
        try:
            self._kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _refuse(self, what: str, target: int, sig: int) -> None:
        if target > 1 and not self._exists(target):
            raise ProcessLookupError(errno.ESRCH, f"no process {target}")
        self.refused.append(f"signal {sig} to {what} {target}: this test did not start it")
        raise PermissionError(errno.EPERM, self.refused[-1])

    def kill(self, pid: int, sig: int) -> None:
        if pid <= 0:
            return self.killpg(-pid or self.group, sig) if pid != -1 else self._refuse("every process (pid -1)", pid, sig)
        if sig == 0 or self._started(pid):
            return self._kill(pid, sig)
        self._refuse("process", pid, sig)

    def killpg(self, group: int, sig: int) -> None:
        if sig == 0 or (group > 1 and group != self.group and self._started(group)):
            return self._killpg(group, sig)
        self._refuse("process group", group, sig)
