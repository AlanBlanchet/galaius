"""The one cross-process writer lock: an exclusive lock held on a lock file its caller opened (how
it is opened safely — private folder, no link followed — stays the caller's). POSIX `flock`;
Windows `LockFileEx` on the file's first byte, waiting as long as another holder keeps it."""

import os
import sys
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


@contextmanager
def exclusive(descriptor: int) -> Iterator[None]:
    """Hold an exclusive lock on `descriptor` for the block, then release it and close it."""
    try:
        FILE_LOCK.acquire(descriptor)
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
