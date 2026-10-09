"""What says a file changed without reading it."""

import os
from pathlib import Path
from typing import NamedTuple, Self


class FileStamp(NamedTuple):
    """A file's inode (a replace makes a new one), size and modification time. Never the change time:
    a reader's chmod moves it without changing a byte."""

    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def of(cls, info: os.stat_result) -> Self:
        return cls(info.st_ino, info.st_size, info.st_mtime_ns)

    @classmethod
    def at(cls, path: Path | os.DirEntry | str | int) -> Self | None:
        """The stamp of a path, a directory entry or an open descriptor; None when it is gone."""
        try:
            info = os.fstat(path) if isinstance(path, int) else path.stat() if isinstance(path, (Path, os.DirEntry)) else os.stat(path)
        except OSError:
            return None
        return cls.of(info)
