"""The one cross-process writer lock: an exclusive lock held on a lock file its caller opened (how
it is opened safely — private folder, no link followed — stays the caller's). POSIX `flock`;
Windows `LockFileEx` on the file's first byte, waiting as long as another holder keeps it."""

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import BaseModel, ConfigDict

if sys.platform == "win32":
    import msvcrt

    import pywintypes
    import win32file

    LOCKFILE_EXCLUSIVE_LOCK = 0x2  # <minwinbase.h>
else:
    import fcntl


class FileLock(BaseModel):
    """One exclusive lock on an open file, as this system takes it."""

    model_config = ConfigDict(frozen=True)

    def acquire(self, descriptor: int) -> None:
        raise NotImplementedError

    def release(self, descriptor: int) -> None:
        raise NotImplementedError


class PosixFileLock(FileLock):
    """POSIX: `flock` on the whole file."""

    def acquire(self, descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)

    def release(self, descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


class WindowsFileLock(FileLock):
    """Windows: the first byte, from offset 0. `LockFileEx` blocks until the holder lets go and
    wakes at once (`msvcrt.locking` retried once a second, so a waiter could lose whole seconds to
    every other taker in turn)."""

    def acquire(self, descriptor: int) -> None:
        win32file.LockFileEx(msvcrt.get_osfhandle(descriptor), LOCKFILE_EXCLUSIVE_LOCK, 0, 1, pywintypes.OVERLAPPED())

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
