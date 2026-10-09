"""A directory reached component by component, never through a link, so a link swapped into its
path cannot redirect what is then created, read, moved or removed inside it
(`PinnedDirectory.open(base, *parts)`; every operation takes one entry NAME inside it).

- POSIX (`DescriptorDirectory`): each component opened with O_NOFOLLOW from its parent's
  descriptor, every operation an `*at()` call on the last one: the kernel pins the directory.
- Windows (`PathDirectory`, chosen wherever `os.supports_dir_fd` lacks those calls): no `openat`,
  so each component is checked with `lstat` to be a real folder, never a link or junction (a
  name-surrogate reparse point; OneDrive / dedup placeholders are plain files and folders), and
  each operation uses the full path; a file is opened, then checked to still be the plain entry
  its name holds (O_TRUNC applied only after that check).

Residual (Windows): a process of the SAME user swapping a component between check and use can
still redirect one operation; the profile's ACL keeps every other user out.
"""

import contextlib
import errno
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict


class PinnedDirectory(BaseModel):
    """The platform-neutral contract; `open` builds the selected backend (`selected`)."""

    model_config = ConfigDict(frozen=True)
    path: Path
    #: The folders `open(..., create=True)` made on the way, outermost first.
    created: tuple[Path, ...] = ()
    #: Characters that would make an entry name a path.
    SEPARATORS: ClassVar[frozenset[str]] = frozenset("/")
    #: Opens a planted named pipe without waiting on it (refused next by its file type).
    NONBLOCK: ClassVar[int] = getattr(os, "O_NONBLOCK", 0)
    #: The backend `PinnedDirectory.open` builds: the platform's own, unless a caller pins
    #: another with `using` (tests run the Windows backend on Linux this way).
    selected: ClassVar[ContextVar[type]]

    @classmethod
    @contextmanager
    def open(cls, base: Path, *parts: str, create: bool = False) -> Iterator[Self]:
        """`base` joined with `parts`, every one of them a real folder reached without a link;
        `create` makes the missing ones (mode 0700) on the way."""
        backend = cls.selected.get() if cls is PinnedDirectory else cls
        with backend._pin(Path(base), tuple(backend._name(part) for part in parts), create) as directory:
            yield directory

    @classmethod
    def at(cls, path: Path, *, create: bool = False):
        """Absolute `path` pinned from its anchor (`/`, a drive, a share): no link anywhere on it."""
        path = Path(path).absolute()
        return cls.open(Path(path.anchor), *path.parts[1:], create=create)

    @classmethod
    @contextmanager
    def using(cls, backend: type) -> Iterator[None]:
        token = cls.selected.set(backend)
        try:
            yield
        finally:
            cls.selected.reset(token)

    @classmethod
    def permissions(cls, info: os.stat_result) -> int:
        """The permission bits a file's facts stand for on the selected backend."""
        return cls.selected.get().permissions(info)

    @classmethod
    def link_like(cls, info: os.stat_result) -> bool:
        """Whether an entry's own facts (`stat(name)`) show a link the selected backend refuses."""
        return cls.selected.get().link_like(info)

    @classmethod
    def backends(cls) -> tuple[type, ...]:
        """Every backend this computer can run (the portable one always)."""
        return tuple(backend for backend in (DescriptorDirectory, PathDirectory) if backend.supported())

    def move(self, name: str, target: Self, target_name: str) -> None:
        """Folder `name` renamed to `target_name` in `target`, never over anything there:
        FileExistsError when that name is taken, even by an empty folder. POSIX renames over an
        empty folder, so the name is claimed first with a folder of its own (`mkdir` fails when
        taken), then renamed over that claim; Windows renames never replace (`PathDirectory`)."""
        target.mkdir(target_name)
        try:
            self.replace(name, target, target_name)
        except OSError as error:
            with contextlib.suppress(OSError):
                target.rmdir(target_name)
            raise FileExistsError(errno.EEXIST, "taken meanwhile", target_name) if error.errno in {errno.ENOTEMPTY, errno.EEXIST} else error

    @classmethod
    def _name(cls, name: str) -> str:
        if not name or name in {".", ".."} or any(separator in name for separator in cls.SEPARATORS):
            raise ValueError(f"{name!r} is not one entry name")
        return name


class DescriptorDirectory(PinnedDirectory):
    """POSIX: the directory held open; operations are relative to its descriptor."""

    descriptor: int
    FLAGS: ClassVar[int] = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

    @classmethod
    def supported(cls) -> bool:
        return (getattr(os, "O_NOFOLLOW", 0) > 0 and getattr(os, "O_DIRECTORY", 0) > 0
                and {os.open, os.mkdir, os.unlink, os.rmdir, os.rename, os.link, os.stat} <= os.supports_dir_fd
                and shutil.rmtree.avoids_symlink_attacks)

    @classmethod
    @contextmanager
    def _pin(cls, base: Path, parts: tuple[str, ...], create: bool) -> Iterator[Self]:
        descriptor, path, created = os.open(base, cls.FLAGS), base, []
        try:
            for part in parts:
                path = path / part
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                        created.append(path)
                    except FileExistsError:
                        pass
                child = os.open(part, cls.FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield cls(path=path, created=tuple(created), descriptor=descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def link_like(cls, info: os.stat_result) -> bool:
        return stat.S_ISLNK(info.st_mode)

    @staticmethod
    def chmod(descriptor: int, mode: int) -> None:
        os.fchmod(descriptor, mode)

    @staticmethod
    def permissions(info: os.stat_result) -> int:
        return stat.S_IMODE(info.st_mode)

    def file(self, name: str, flags: int = os.O_RDONLY, mode: int = 0o600) -> int:
        """A descriptor of entry `name`, refused (ELOOP) when that entry is a link."""
        return os.open(self._name(name), flags | os.O_NOFOLLOW, mode, dir_fd=self.descriptor)

    def stat(self, name: str | None = None) -> os.stat_result:
        """The entry's own facts (a link is reported as a link); the folder's when `name` is None."""
        if name is None:
            return os.fstat(self.descriptor)
        return os.stat(self._name(name), dir_fd=self.descriptor, follow_symlinks=False)

    def names(self) -> list[str]:
        return os.listdir(self.descriptor)

    def mkdir(self, name: str, mode: int = 0o700) -> None:
        os.mkdir(self._name(name), mode, dir_fd=self.descriptor)

    def unlink(self, name: str) -> None:
        os.unlink(self._name(name), dir_fd=self.descriptor)

    def rmdir(self, name: str) -> None:
        os.rmdir(self._name(name), dir_fd=self.descriptor)

    def rmtree(self, name: str) -> None:
        shutil.rmtree(self._name(name), dir_fd=self.descriptor)

    def replace(self, name: str, target: Self, target_name: str) -> None:
        os.replace(self._name(name), target._name(target_name), src_dir_fd=self.descriptor, dst_dir_fd=target.descriptor)

    def link(self, name: str, target: Self, target_name: str) -> os.stat_result | None:
        """A second name for the entry (a link entry is linked as itself); fails if taken."""
        os.link(self._name(name), target._name(target_name), src_dir_fd=self.descriptor,
                dst_dir_fd=target.descriptor, follow_symlinks=False)
        return None


class PathDirectory(PinnedDirectory):
    """Windows (any system without `*at()` calls): full paths, each entry checked before use."""

    #: ':' too: "D:x" is relative to another drive's folder, "name:stream" an NTFS alternate stream.
    SEPARATORS: ClassVar[frozenset[str]] = frozenset("/\\:")
    FLAGS: ClassVar[int] = getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    #: IsReparseTagNameSurrogate: the reparse point names another entry (symlink, junction, mount).
    SURROGATE: ClassVar[int] = 0x20000000
    #: Why a hard link can fail where a copy works: winerror (invalid function, not the same
    #: device, not supported) and errno (FAT / exFAT / network shares refuse links).
    UNLINKABLE_WINERRORS: ClassVar[frozenset[int]] = frozenset({1, 17, 50})
    UNLINKABLE_ERRNOS: ClassVar[frozenset[int]] = frozenset({errno.EXDEV, errno.EOPNOTSUPP})

    @classmethod
    def supported(cls) -> bool:
        return True

    @classmethod
    def link_like(cls, info: os.stat_result) -> bool:
        """A symlink, or a Windows junction / mount point (`st_file_attributes` is Windows-only)."""
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            and getattr(info, "st_reparse_tag", 0) & cls.SURROGATE)

    @classmethod
    def _checked(cls, path: Path, *, directory: bool) -> os.stat_result:
        info = os.lstat(path)
        if cls.link_like(info):
            raise OSError(errno.ELOOP, "links and junctions are never followed", str(path))
        if directory and not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "not a folder", str(path))
        return info

    @classmethod
    @contextmanager
    def _pin(cls, base: Path, parts: tuple[str, ...], create: bool) -> Iterator[Self]:
        path, created = base, []
        cls._checked(path, directory=True)
        for part in parts:
            path = path / part
            if create:
                try:
                    os.mkdir(path, 0o700)
                    created.append(path)
                except FileExistsError:
                    pass
            cls._checked(path, directory=True)
        yield cls(path=path, created=tuple(created))

    @staticmethod
    def chmod(descriptor: int, mode: int) -> None:
        """Windows has no mode bits beyond read-only: the profile ACL is the privacy."""

    @staticmethod
    def permissions(info: os.stat_result) -> int:
        """The POSIX mode a file here stands for: owner-private, as the profile ACL makes it."""
        return 0o600

    def file(self, name: str, flags: int = os.O_RDONLY, mode: int = 0o600) -> int:
        """A descriptor of entry `name`: refused (ELOOP) when it is a link before the open, or when
        the name no longer holds the opened file after it; O_TRUNC only once that is proven."""
        path = self.path / self._name(name)
        try:
            self._checked(path, directory=False)
        except FileNotFoundError:
            pass
        descriptor = os.open(path, (flags & ~os.O_TRUNC) | self.FLAGS, mode)
        try:
            opened, named = os.fstat(descriptor), os.lstat(path)
            if self.link_like(named) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise OSError(errno.ELOOP, "the name changed to a link while it was opened", str(path))
            if flags & os.O_TRUNC:
                os.ftruncate(descriptor, 0)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def stat(self, name: str | None = None) -> os.stat_result:
        return os.lstat(self.path if name is None else self.path / self._name(name))

    def names(self) -> list[str]:
        return os.listdir(self.path)

    def mkdir(self, name: str, mode: int = 0o700) -> None:
        os.mkdir(self.path / self._name(name), mode)

    def unlink(self, name: str) -> None:
        os.unlink(self.path / self._name(name))

    def rmdir(self, name: str) -> None:
        path = self.path / self._name(name)
        self._checked(path, directory=True)
        os.rmdir(path)

    def rmtree(self, name: str) -> None:
        path = self.path / self._name(name)
        self._checked(path, directory=True)
        shutil.rmtree(path)

    def replace(self, name: str, target: Self, target_name: str) -> None:
        os.replace(self.path / self._name(name), target.path / target._name(target_name))

    def move(self, name: str, target: Self, target_name: str) -> None:
        if os.name != "nt":
            return super().move(name, target, target_name)
        try:
            os.rename(self.path / self._name(name), target.path / target._name(target_name))
        except OSError as error:
            if getattr(error, "winerror", None) in {80, 183}:  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
                raise FileExistsError(errno.EEXIST, "taken", target_name) from None
            raise

    def link(self, name: str, target: Self, target_name: str) -> os.stat_result | None:
        """A hard link where the file system has them (None: the same file, second name); else an
        exclusive byte copy, whose own facts are returned (still fails when `target_name` is
        taken, never follows a link)."""
        try:
            # A link names itself, never what it points to: plain link() follows it on macOS.
            os.link(self.path / self._name(name), target.path / target._name(target_name),
                    **({"follow_symlinks": False} if os.link in os.supports_follow_symlinks else {}))
            return None
        except OSError as error:
            if getattr(error, "winerror", None) not in self.UNLINKABLE_WINERRORS and error.errno not in self.UNLINKABLE_ERRNOS:
                raise
        with os.fdopen(self.file(name), "rb") as source:
            content, mode = source.read(), stat.S_IMODE(os.fstat(source.fileno()).st_mode)
        with os.fdopen(target.file(target_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as copy:
            copy.write(content)
            copy.flush()
            return os.fstat(copy.fileno())


PinnedDirectory.selected = ContextVar(
    "pinned_directory_backend", default=DescriptorDirectory if DescriptorDirectory.supported() else PathDirectory)
