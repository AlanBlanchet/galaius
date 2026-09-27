"""The one cross-process writer lock: an exclusive `flock` held on a lock file its caller opened
(how it is opened safely — private folder, no link followed — stays the caller's)."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def exclusive(descriptor: int) -> Iterator[None]:
    """Hold an exclusive lock on `descriptor` for the block, then release it and close it."""
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
