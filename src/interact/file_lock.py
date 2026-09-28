"""The one cross-process writer lock: an exclusive lock held on a lock file its caller opened (how
it is opened safely — private folder, no link followed — stays the caller's). POSIX `flock`;
Windows locks the file's first byte (`msvcrt.locking`), waiting as long as another process holds it."""

import errno
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import BaseModel, ConfigDict

if sys.platform == "win32":
    import msvcrt
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
    """Windows: the first byte, locked from offset 0 (the lock is by position)."""

    def acquire(self, descriptor: int) -> None:
        while True:
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                return
            except OSError as error:
                if error.errno != errno.EDEADLOCK:  # LK_LOCK gives up after ~10 s with EDEADLOCK: wait again; anything else is real
                    raise

    def release(self, descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


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
