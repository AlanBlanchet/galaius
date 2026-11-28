"""Which files of some folders changed since last asked, as the kernel says (Linux inotify), so a poller of a folder of
tens of thousands of files reads only what moved instead of listing it all again."""

import ctypes
import os
import struct
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Self

_MODIFY, _ATTRIB, _CLOSE_WRITE, _MOVED_FROM, _MOVED_TO, _CREATE, _DELETE, _DELETE_SELF, _MOVE_SELF = (
    0x2, 0x4, 0x8, 0x40, 0x80, 0x100, 0x200, 0x400, 0x800)
_OVERFLOW, _IGNORED, _ISDIR = 0x4000, 0x8000, 0x40000000
_WATCHED = _MODIFY | _ATTRIB | _CLOSE_WRITE | _MOVED_FROM | _MOVED_TO | _CREATE | _DELETE | _DELETE_SELF | _MOVE_SELF
#: struct inotify_event before its name: wd, mask, cookie, len.
_EVENT = struct.Struct("iIII")


class FolderChanges:
    """The files written, created, renamed or removed in `folders` (their own entries, never deeper) and in every direct
    subfolder of `parents` (one created later too). Not shared between threads; a forked child opens its own."""

    def __init__(self, descriptor: int, libc: ctypes.CDLL) -> None:
        self._descriptor, self._libc, self._process = descriptor, libc, os.getpid()
        self._folders: dict[int, Path] = {}
        self._parents: set[Path] = set()
        #: A change was missed (queue overflow, a watched folder gone): the next `take` says so once.
        self._lost = False

    @classmethod
    def watch(cls, folders: Iterable[Path] = (), *, parents: Iterable[Path] = ()) -> Self | None:
        """A watch of them; None where the system has no inotify or refuses one (another system, its limit reached, a
        folder missing): the caller lists the folders itself then."""
        if not sys.platform.startswith("linux"):
            return None
        libc = ctypes.CDLL(None, use_errno=True)
        descriptor = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if descriptor < 0:
            return None
        changes = cls(descriptor, libc)
        parents = tuple(parents)
        changes._parents.update(parents)
        try:
            watched = all([changes._add(folder) for folder in (*folders, *parents)])
            for parent in parents:
                with os.scandir(parent) as entries:
                    watched = all([changes._add(Path(entry.path)) for entry in entries if entry.is_dir()]) and watched
        except OSError:
            watched = False
        if not watched:
            changes.close()
            return None
        return changes

    def _add(self, folder: Path) -> bool:
        number = self._libc.inotify_add_watch(self._descriptor, os.fsencode(folder), _WATCHED)
        if number >= 0:
            self._folders[number] = folder
        return number >= 0

    @property
    def usable(self) -> bool:
        """False once closed, a watched folder is gone, or this is a forked child: watch again."""
        return self._descriptor >= 0 and self._process == os.getpid() and bool(self._folders)

    def take(self) -> set[Path] | None:
        """The paths that changed since the last call (the first: since the watch began); None when changes were missed,
        so anything may have changed."""
        if not self.usable:
            return None
        changed: set[Path] = set()
        while True:
            try:
                events = os.read(self._descriptor, 65536)
            except BlockingIOError:
                break
            except OSError:
                self._lost = True
                break
            offset = 0
            while offset < len(events):
                number, mask, _cookie, length = _EVENT.unpack_from(events, offset)
                name = events[offset + _EVENT.size:offset + _EVENT.size + length].rstrip(b"\0")
                offset += _EVENT.size + length
                if mask & _OVERFLOW:
                    self._lost = True
                    continue
                folder = self._folders.get(number)
                if folder is None:
                    continue
                if mask & (_IGNORED | _DELETE_SELF | _MOVE_SELF):
                    # A subfolder of a parent going away is an ordinary change; a watched folder itself ends the watch.
                    if folder.parent in self._parents:
                        self._folders.pop(number, None)
                        changed.add(folder)
                    else:
                        self._lost = True
                        self.close()
                        return None
                    continue
                if not name:
                    continue
                path = folder / os.fsdecode(name)
                if mask & _ISDIR and mask & (_CREATE | _MOVED_TO) and folder in self._parents:
                    # What it already holds was written before this watch of it: anything may have changed there.
                    self._add(path)
                    self._lost = True
                changed.add(path)
        if self._lost:
            self._lost = False
            return None
        return changed

    def close(self) -> None:
        if self._descriptor >= 0:
            if self._process == os.getpid():
                os.close(self._descriptor)
            self._descriptor = -1
        self._folders.clear()

    def __del__(self) -> None:
        self.close()
