"""A project folder copied from one of the owner's PCs to another through the server, for a PC that
cannot clone it (no git there, no sign-in to its host, no repository at all).

On the source PC (`WorkspaceFiles`): the git checkout's tracked files when git is there, else the
folder walked without hidden names and `DEPENDENCY_FOLDERS`; never a link, a credential store
(`NEVER_GRANTABLE.secret`), `.git`, or a name another system cannot hold (`PortableName`). The files
that fit within `WORKSPACE_COPY_MAX_BYTES` / `_FILES` are taken, the rest listed (`WorkspaceSkip`);
packed as a gzip'd tar of plain files; uploaded part by part over HTTP with the machine's own token
(`WorkspaceTransfers`).

On the target PC (`WorkspaceArchiveReader`): downloaded part by part, its sha256 checked over the
whole, then read as a stream bounded in total (`BoundedReader`) and per header record
(`BoundedHeader`: none grows in memory), every member checked before one byte is
written (a plain file, a portable relative name, no `.git`, within the counts it declared), written
through `PinnedDirectory` (never through a link or junction) into a hidden partial folder that
`MachineWorkspaces` renames into place."""

import gzip
import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import tarfile
import time
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import ClassVar, get_args
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict

from galaius_core import (
    RESERVED_DEVICE_NAMES, WORKSPACE_ARCHIVE_MAX_BYTES, WORKSPACE_COPY_EXAMINED, WORKSPACE_COPY_MAX_BYTES, WORKSPACE_COPY_MAX_FILES, WORKSPACE_SKIP_EXAMPLES, WORKSPACE_TRANSFER_PART,
    GitRemote, WorkspaceArchive, WorkspacePack, WorkspaceSkip, WorkspaceSkipReason, WorkspaceUpload,
)

from galaius.pinned_directory import PinnedDirectory
from galaius.places import DEPENDENCY_FOLDERS, NEVER_GRANTABLE
from galaius.workspace_git import Git, WorkspaceRefused


class PortableName:
    """A file or folder name every system the owner's PCs run can hold, and never one that acts as
    `.git`: printable UTF-8 (no invisible character HFS+ would drop), not empty, `.` or `..`, no
    character Windows refuses (`<>:"/\\|?*`), no trailing dot or space, no reserved device name
    (`RESERVED_DEVICE_NAMES`, whatever follows a dot), not `.git` nor its Windows short name `git~N`."""

    FORBIDDEN: ClassVar[frozenset[str]] = frozenset('<>:"/\\|?*')
    GIT: ClassVar[re.Pattern[str]] = re.compile(r"^(?:\.git|git~[0-9]+)$", re.IGNORECASE)
    LONGEST: ClassVar[int] = 255

    @classmethod
    def holds(cls, name: str) -> bool:
        return (bool(name) and name.isprintable() and name.encode("utf-8", errors="replace").decode("utf-8") == name and name not in {".", ".."}
                and len(name) <= cls.LONGEST and not cls.FORBIDDEN.intersection(name) and not name.endswith((".", " "))
                and name.split(".")[0].casefold() not in RESERVED_DEVICE_NAMES and cls.GIT.match(name) is None)

    @classmethod
    def path(cls, path: str) -> tuple[str, ...] | None:
        """`a/b/c` as its parts when every one holds, else None (absolute, `..`, a drive, `.git` ...)."""
        parts = tuple(path.split("/"))
        return parts if path and all(cls.holds(part) for part in parts) else None


class PackedFile(BaseModel):
    """One file a copy carries: its parts below the copied folder and its size when measured."""

    model_config = ConfigDict(frozen=True)
    parts: tuple[str, ...]
    size: int

    @property
    def path(self) -> str:
        return "/".join(self.parts)


class WorkspaceFiles(BaseModel):
    """The files a copy of `folder` carries (`pack`) and their archive (`write`)."""

    model_config = ConfigDict(frozen=True)
    folder: Path
    environment: dict[str, str]
    DEPTH: ClassVar[int] = 32
    GIT_OPTIONS: ClassVar[tuple[str, ...]] = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.quotePath=false")

    def tracked(self) -> list[str] | None:
        """The checkout's tracked paths (submodules' included) when `folder` is one and git runs here, else None."""
        if not Git.installed(self.environment) or not (self.folder / ".git").exists():
            return None
        git = Git(options=self.GIT_OPTIONS, environment={**self.environment, "GIT_TERMINAL_PROMPT": "0"}, host="", deadline=time.monotonic() + 120)
        try:
            return [path for path in git.run("-C", str(self.folder), "ls-files", "-z", "--cached", "--recurse-submodules").split("\0") if path]
        except (WorkspaceRefused, OSError, TimeoutError, subprocess.TimeoutExpired):
            return None

    def _plain(self, parts: tuple[str, ...]) -> int | None:
        """The size of `parts` when it is a plain file reached without a link, else None."""
        try:
            with PinnedDirectory.open(self.folder, *parts[:-1]) as folder:
                facts = folder.stat(parts[-1])
        except (OSError, ValueError):
            return None
        return facts.st_size if stat.S_ISREG(facts.st_mode) and not PinnedDirectory.link_like(facts) else None

    def _walked(self, parts: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
        """Every entry below `parts` that is not a folder walked into (links too: `_plain` sorts
        them out), never through a link, never into a hidden or dependency folder."""
        if len(parts) > self.DEPTH:
            return
        try:
            with PinnedDirectory.open(self.folder, *parts) as folder:
                found = []
                for name in sorted(folder.names()):
                    if name.startswith(".") or name in DEPENDENCY_FOLDERS:
                        continue
                    try:
                        facts = folder.stat(name)
                    except OSError:
                        continue
                    found.append((name, stat.S_ISDIR(facts.st_mode) and not PinnedDirectory.link_like(facts)))
        except OSError:
            return
        for name, is_folder in found:
            yield from self._walked((*parts, name)) if is_folder else ((*parts, name),)

    def pack(self, origin: GitRemote | None) -> tuple[WorkspacePack, list[PackedFile]]:
        """The measure and the files behind it: every plain file under a portable, non-credential name,
        taken in order while it fits within the copy limits; the rest left out, by reason."""
        tracked = self.tracked()
        files: list[PackedFile] = []
        size, counts, examples = 0, dict.fromkeys(get_args(WorkspaceSkipReason), 0), {reason: [] for reason in get_args(WorkspaceSkipReason)}
        candidates = (tuple(path.split("/")) for path in tracked) if tracked is not None else self._walked()
        more = False
        for seen, parts in enumerate(candidates):
            if seen >= WORKSPACE_COPY_EXAMINED:
                more = True
                break
            path = "/".join(parts)
            if PortableName.path(path) is None:
                reason = "name"
            elif any(NEVER_GRANTABLE.secret(part) for part in parts):
                reason = "credential_store"
            elif (found := self._plain(parts)) is None:
                reason = "link"
            elif len(files) >= WORKSPACE_COPY_MAX_FILES or size + found > WORKSPACE_COPY_MAX_BYTES:
                reason = "over_limit"
            else:
                files.append(PackedFile(parts=parts, size=found))
                size += found
                continue
            counts[reason] += 1
            if len(examples[reason]) < WORKSPACE_SKIP_EXAMPLES:
                examples[reason].append(path)
        if more:
            counts["over_limit"] += 1
        left_out = tuple(WorkspaceSkip(reason=reason, count=count, examples=tuple(examples[reason]), more=more and reason == "over_limit") for reason, count in counts.items() if count)
        return WorkspacePack(files=len(files), size=size, tracked=tracked is not None, origin=origin, skipped=left_out), files

    def write(self, files: list[PackedFile], archive: Path) -> None:
        """`files` as a gzip'd tar at `archive`: plain files only, each read through `PinnedDirectory`
        and exactly as large as when measured (a file that changed meanwhile fails the copy)."""
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as packed:
            for file in files:
                with PinnedDirectory.open(self.folder, *file.parts[:-1]) as folder:
                    descriptor = folder.file(file.parts[-1], os.O_RDONLY | getattr(os, "O_BINARY", 0) | PinnedDirectory.NONBLOCK)
                with os.fdopen(descriptor, "rb") as stream:
                    facts = os.fstat(stream.fileno())
                    if not stat.S_ISREG(facts.st_mode) or facts.st_size != file.size:
                        raise WorkspaceRefused("failed", f"{file.path} changed while it was being copied; try again")
                    member = tarfile.TarInfo(file.path)
                    member.size, member.mtime, member.mode = file.size, int(facts.st_mtime), 0o755 if facts.st_mode & stat.S_IXUSR else 0o644
                    packed.addfile(member, stream)


class WorkspaceTransfers(BaseModel):
    """A copy's archive crossing the server: parts PUT by the source PC, GET by the target PC
    (`WorkspaceArchive.part_route`), under this machine's own token; each part retried a few times
    with a growing wait on a network error or a server error, never on a refusal."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    endpoint: Callable[[str], str]
    headers: dict[str, str]
    timeout: float = 120
    WAITS: ClassVar[tuple[float, ...]] = (2, 8, 30)

    def _call(self, method: str, path: str, headers: dict[str, str] | None = None, **options) -> httpx.Response:
        waits, url, headers = iter(self.WAITS), self.endpoint(path), {**self.headers, **(headers or {})}
        while True:
            try:
                response = httpx.request(method, url, headers=headers, timeout=self.timeout, **options)
            except httpx.HTTPError as error:
                failure = f"the server is unreachable ({type(error).__name__})"
            else:
                if response.status_code < 500 and response.status_code not in {408, 429}:
                    if response.is_error:
                        raise WorkspaceRefused("transfer_failed", f"the server refused the copy (HTTP {response.status_code})")
                    return response
                failure = f"the server answered HTTP {response.status_code}"
            wait = next(waits, None)
            if wait is None:
                raise WorkspaceRefused("transfer_failed", failure)
            time.sleep(wait)

    def upload(self, transfer: UUID, archive: Path, files: int) -> WorkspaceArchive:
        """Every part of `archive`, then the `WorkspaceUpload` that ends the transfer."""
        whole, size, index = hashlib.sha256(), 0, 0
        with archive.open("rb") as stream:
            while part := stream.read(WORKSPACE_TRANSFER_PART):
                whole.update(part)
                size += len(part)
                self._call("PUT", WorkspaceArchive.part_route(transfer, index), content=part, headers={"x-galaius-digest": hashlib.sha256(part).hexdigest()})
                index += 1
        sent = WorkspaceArchive(transfer=transfer, size=size, digest=whole.hexdigest(), files=files)
        self.ended(transfer, WorkspaceUpload(outcome=sent))
        return sent

    def ended(self, transfer: UUID, outcome: WorkspaceUpload) -> None:
        self._call("POST", WorkspaceArchive.route(transfer), json=outcome.model_dump(mode="json"))

    def download(self, archive: WorkspaceArchive, target: Path, progress: Callable[[int], None]) -> None:
        """Every part of `archive` into `target`, its size and sha256 checked over the whole."""
        whole, size = hashlib.sha256(), 0
        with target.open("wb") as stream:
            for index in range(archive.parts):
                part = self._call("GET", WorkspaceArchive.part_route(archive.transfer, index)).content
                if len(part) != min(WORKSPACE_TRANSFER_PART, archive.size - size):
                    raise WorkspaceRefused("transfer_failed", f"part {index} of the copy arrived with {len(part)} bytes")
                whole.update(part)
                stream.write(part)
                size += len(part)
                progress(size)
        if whole.hexdigest() != archive.digest:
            raise WorkspaceRefused("transfer_failed", "the copy arrived damaged (sha256 mismatch)")


class BoundedReader(io.RawIOBase):
    """A decompressed archive as tarfile reads it: never more bytes in all than an archive within
    the copy limits holds."""

    def __init__(self, stream: gzip.GzipFile) -> None:
        self.stream, self.total = stream, 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self.stream.read(len(buffer))
        self.total += len(data)
        if self.total > WORKSPACE_ARCHIVE_MAX_BYTES:
            raise WorkspaceRefused("unsafe_archive", "the copy unpacks to more than it may hold")
        buffer[:len(data)] = data
        return len(data)


class BoundedHeader(tarfile.TarInfo):
    """A member whose header records (long names, PAX attributes) tarfile reads whole into memory:
    refused past `MOST` bytes, before they are read; a sparse member (its map read ahead) never.
    Hooks tarfile's `_proc_member` (stable across the Python versions galaius ships, pinned by the
    copy tests)."""

    MOST: ClassVar[int] = 64 * 1024
    RECORDS: ClassVar[frozenset[bytes]] = frozenset({tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK, tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE})

    def _proc_member(self, tarfile_: tarfile.TarFile):
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise tarfile.HeaderError("a sparse member: no copy holds one")
        if self.type in self.RECORDS and self.size > self.MOST:
            raise tarfile.HeaderError(f"a header record of {self.size} bytes")
        return super()._proc_member(tarfile_)


class WorkspaceArchiveReader(BaseModel):
    """Writes a received archive's files into `parts` below `base` (a fresh partial folder)."""

    model_config = ConfigDict(frozen=True)
    archive: WorkspaceArchive
    base: Path
    parts: tuple[str, ...]

    def extract(self, path: Path) -> int:
        """Every member written, or `unsafe_archive` at the first one that is not a plain file with
        a portable relative name, a second one under the same name (any case), or one past the
        counts the archive declared; the files written."""
        try:
            with gzip.open(path, "rb") as unzipped, tarfile.open(fileobj=BoundedReader(unzipped), mode="r|", tarinfo=BoundedHeader) as packed:
                return self._written(packed)
        except (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile) as error:
            raise WorkspaceRefused("unsafe_archive", f"the copy is damaged ({type(error).__name__})") from None

    def _written(self, packed: tarfile.TarFile) -> int:
        written, seen, total = 0, set(), 0
        for member in packed:
            parts = PortableName.path(member.name)
            if not member.isreg() or parts is None:
                raise WorkspaceRefused("unsafe_archive", f"the copy holds {member.name[:200]!r}, which is never written (only plain files under plain names)")
            folded = "/".join(parts).casefold()
            total += member.size
            if folded in seen or written >= self.archive.files or total > WORKSPACE_COPY_MAX_BYTES:
                raise WorkspaceRefused("unsafe_archive", "the copy holds more than it declared, or one name twice")
            seen.add(folded)
            source = packed.extractfile(member)
            with PinnedDirectory.open(self.base, *self.parts, *parts[:-1], create=True) as folder:
                descriptor = folder.file(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o755 if member.mode & stat.S_IXUSR else 0o644)
            with os.fdopen(descriptor, "wb") as stream:
                shutil.copyfileobj(source, stream, 1 << 20)
            written += 1
        return written
