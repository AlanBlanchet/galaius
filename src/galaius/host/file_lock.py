"""The one cross-process writer lock: an exclusive lock held on a lock file its caller opened (how
it is opened safely — private folder, no link followed — stays the caller's). POSIX `flock`;
Windows `LockFileEx` on the file's first byte, waiting as long as another holder keeps it."""

import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from pydantic import BaseModel, ConfigDict

if sys.platform == "win32":
    import msvcrt

    import pywintypes
    import win32file

    LOCKFILE_FAIL_IMMEDIATELY = 0x1  # <minwinbase.h>
    LOCKFILE_EXCLUSIVE_LOCK = 0x2
    ERROR_LOCK_VIOLATION = 33  # <winerror.h>: another handle holds the range
else:
    import fcntl


class FileLock(BaseModel):
    """One exclusive lock on an open file, as this system takes it."""

    model_config = ConfigDict(frozen=True)

    def acquire(self, descriptor: int) -> None:
        raise NotImplementedError

    def try_acquire(self, descriptor: int) -> bool:
        """Take the lock only if nobody holds it; whether it was taken."""
        raise NotImplementedError

    def release(self, descriptor: int) -> None:
        raise NotImplementedError


class PosixFileLock(FileLock):
    """POSIX: `flock` on the whole file."""

    def acquire(self, descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)

    def try_acquire(self, descriptor: int) -> bool:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def release(self, descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


class WindowsFileLock(FileLock):
    """Windows: the first byte, from offset 0. `LockFileEx` blocks until the holder lets go and
    wakes at once (`msvcrt.locking` retried once a second, so a waiter could lose whole seconds to
    every other taker in turn)."""

    def acquire(self, descriptor: int) -> None:
        win32file.LockFileEx(msvcrt.get_osfhandle(descriptor), LOCKFILE_EXCLUSIVE_LOCK, 0, 1, pywintypes.OVERLAPPED())

    def try_acquire(self, descriptor: int) -> bool:
        try:
            win32file.LockFileEx(msvcrt.get_osfhandle(descriptor), LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY, 0, 1, pywintypes.OVERLAPPED())
        except pywintypes.error as error:
            if error.winerror != ERROR_LOCK_VIOLATION:
                raise
            return False
        return True

    def release(self, descriptor: int) -> None:
        win32file.UnlockFileEx(msvcrt.get_osfhandle(descriptor), 0, 1, pywintypes.OVERLAPPED())


FILE_LOCK: FileLock = WindowsFileLock() if sys.platform == "win32" else PosixFileLock()


class LockTimeout(TimeoutError):
    """The lock was not taken within the waiter's `patience` (`exclusive`)."""


#: The longest a bounded waiter (`exclusive` with `patience` or `long_wait`) sleeps between two tries: a holder letting go
#: is seen within it.
RETRY_CAP = 0.025


def _wait_for(descriptor: int, *, patience: float | None, long_wait: float | None,
              on_long_wait: Callable[[float], None] | None) -> None:
    """Take the lock trying again and again, backing off to `RETRY_CAP`: past `long_wait` seconds call `on_long_wait` once
    with the time waited; past `patience` raise `LockTimeout`. A blocking `acquire` cannot be cut short from the thread
    waiting in it, which is what left one wedged run's every caller waiting for good."""
    started, pause, told = time.monotonic(), 0.001, False
    while not FILE_LOCK.try_acquire(descriptor):
        waited = time.monotonic() - started
        if long_wait is not None and not told and waited >= long_wait:
            told = True
            if on_long_wait is not None:
                on_long_wait(waited)
        if patience is not None and waited >= patience:
            raise LockTimeout(f"lock not taken within {patience:g} s")
        time.sleep(pause)
        pause = min(pause * 2, RETRY_CAP)


@contextmanager
def exclusive(descriptor: int, *, patience: float | None = None, long_wait: float | None = None,
              on_long_wait: Callable[[float], None] | None = None) -> Iterator[None]:
    """Hold an exclusive lock on `descriptor` for the block, then release it and close it. With neither `patience` nor
    `long_wait` it waits in the system's own blocking call; else as `_wait_for` says. A lock never taken still closes
    `descriptor` (`LockTimeout`)."""
    try:
        if patience is None and long_wait is None:
            FILE_LOCK.acquire(descriptor)
        else:
            _wait_for(descriptor, patience=patience, long_wait=long_wait, on_long_wait=on_long_wait)
    except BaseException:
        os.close(descriptor)
        raise
    try:
        yield
    finally:
        try:
            FILE_LOCK.release(descriptor)
        finally:
            os.close(descriptor)


class SoleHolder:
    """The one process doing a job every process COULD do: whoever first takes the lock on the file
    `opener` opens keeps it until it closes or exits (the system lets go of a dead holder's lock); the
    others ask again with `holds()`. Use as a context manager: leaving it lets the job go."""

    def __init__(self, opener: Callable[[], int]) -> None:
        self.opener = opener
        self.descriptor: int | None = None
        self.held = False

    def holds(self) -> bool:
        """Whether this process holds the job now, taking it when nobody does."""
        if not self.held:
            if self.descriptor is None:
                self.descriptor = self.opener()
            self.held = FILE_LOCK.try_acquire(self.descriptor)
        return self.held

    def __enter__(self) -> "SoleHolder":
        return self

    def __exit__(self, *_) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)  # closing the last descriptor releases the lock
        self.descriptor, self.held = None, False
