"""A project folder's files as the web reads them, read-only (`ProjectFilesBase`): one folder's entries with their git
marks, one file's text or image, a search by name, a part of a file's raw bytes (never one shown as text: those leave only
redacted).

Only beneath one agent root; every name on a path matched byte for byte against its folder's own listing (a case,
short-name or trailing-dot alias never opens the entry it stands for), every folder reached by `PinnedDirectory` and
every file opened through it (never a link); never a `project_hidden` name (a credential store's included), nor what
the owner's levels close (`PlaceMap.closed`). A listing counts what it left out,
never names it. Text leaves redacted: credential shapes, the PC's secret values, and every value of the project
folder's own `.env*` files (where its vault's secrets are written), redacted before it is cut.

git runs only in a repository its owner put here himself (never one cloned or copied from the web) whose own config
names no filter, file-system monitor or include; read-only (`--no-optional-locks`: the index is never rewritten),
literal pathspecs, no hooks, no system config, never above the agent root, under a deadline after which its whole
process group is killed, its output capped.
"""

import base64
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, ClassVar

from pydantic import BaseModel, ConfigDict

from galaius_core import (MACHINE_AGENT_MEDIA, PROJECT_FILES_MAX, PROJECT_FOUND_MAX, PROJECT_TEXT_MAX, GitMark, MachineAgentAnswer, ProjectFileBytes,
                          ProjectFileBytesRequest, ProjectFileContent, ProjectFileEntry, ProjectFileRequest, ProjectFilesBase, ProjectFilesRequest,
                          ProjectFindRequest, image_type, project_hidden)
from galaius.pinned_directory import PinnedDirectory
from galaius.places import DEPENDENCY_FOLDERS, PlaceMap
from galaius.redaction import redact


class GitRun(BaseModel):
    """git as a project files view runs it in one folder: fixed options, a bare environment, a deadline, capped output."""

    model_config = ConfigDict(frozen=True)
    #: The agent root: git never looks for a repository above it.
    root: Path
    environment: dict[str, str]
    OPTIONS: ClassVar[tuple[str, ...]] = ("--no-optional-locks", "--literal-pathspecs", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                                          "-c", f"core.hooksPath={os.devnull}", "-c", "core.quotePath=false")
    #: Environment names git keeps from the PC's.
    KEPT: ClassVar[frozenset[str]] = frozenset({"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "LANG"})
    #: A repository's own config naming one of these runs code of its own on a read: no git there.
    RISKY: ClassVar[str] = r"^(filter\.|core\.fsmonitor|include)"
    #: Most bytes one run's output is read to.
    OUTPUT_MAX: ClassVar[int] = 64 * 1024 * 1024
    #: How long the config check may take.
    CHECK_SECONDS: ClassVar[float] = 1.5

    def __call__(self, folder: Path, *arguments: str, seconds: float) -> tuple[int, bytes] | None:
        """git's exit code and output run in `folder`; None without git, past `seconds` or OUTPUT_MAX (its process group
        killed)."""
        git = shutil.which("git", path=self.environment.get("PATH"))
        if git is None:
            return None
        environment = {key: value for key, value in self.environment.items() if key in self.KEPT}
        environment |= {"LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CEILING_DIRECTORIES": str(self.root.parent)}
        try:
            process = subprocess.Popen([git, *self.OPTIONS, *arguments], cwd=folder, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, start_new_session=os.name == "posix",
                                       creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        except OSError:
            return None
        chunks: list[bytes] = []

        def read() -> None:
            size = 0
            while size <= self.OUTPUT_MAX and (chunk := process.stdout.read1(65536)):
                chunks.append(chunk)
                size += len(chunk)

        deadline = time.monotonic() + seconds
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(seconds)
        with process.stdout:
            if reader.is_alive() or sum(map(len, chunks)) > self.OUTPUT_MAX:
                self._kill(process)
                return None
            try:
                code = process.wait(max(deadline - time.monotonic(), 0))
            except subprocess.TimeoutExpired:
                self._kill(process)
                return None
        return code, b"".join(chunks)

    def usable(self, folder: Path) -> bool:
        """Whether `folder`'s repository config runs nothing of its own on a read (`RISKY`: no hit, exit 1)."""
        said = self(folder, "config", "--local", "--get-regexp", self.RISKY, seconds=self.CHECK_SECONDS)
        return said is not None and said[0] == 1

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        """Kills git with every process it started: its process group on POSIX, its process tree on Windows."""
        with suppress(OSError, subprocess.SubprocessError):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe")
                subprocess.run([str(taskkill), "/T", "/F", "/PID", str(process.pid)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5, check=False)
        with suppress(OSError):
            process.kill()
        process.wait()


class ProjectFolder(BaseModel):
    """Project folder `base` beneath agent root `root` (a folder this PC's owner set: trusted as it is)."""

    model_config = ConfigDict(frozen=True)
    root: Path
    base: tuple[str, ...]
    secrets: tuple[str, ...] = ()
    #: What git runs with (its PATH, the home folder its own settings live in).
    environment: dict[str, str]
    #: The owner's levels.
    places: PlaceMap
    #: Folders put on this PC from the web (clones, copies): no git runs there.
    untrusted: tuple[Path, ...] = ()
    #: How long `git status` may take for one folder's marks; how long a search may take.
    STATUS_SECONDS: ClassVar[float] = 1.5
    FIND_SECONDS: ClassVar[float] = 2.0
    #: Entries a search outside a repository looks at, at most.
    WALK_MAX: ClassVar[int] = 50_000
    #: The first bytes read to tell text (no NUL), an image and anything else apart.
    HEAD: ClassVar[int] = 8192
    #: Heads of documents a viewer opens, never read as source text, though they may hold no NUL: 15 of 400 PDFs
    #: sampled on one PC (2026-10-28) carry none in their first 8 KiB.
    DOCUMENT_HEADS: ClassVar[tuple[bytes, ...]] = (b"%PDF-",)
    #: The tail a cut line loses: its last run of key characters (no space, quote, comma, semicolon), at most CUT_TAIL of
    #: them: a secret the cut split, which no mask recognises any more.
    CUT_TAIL: ClassVar[int] = 1024
    KEY_BREAKS: ClassVar[frozenset[str]] = frozenset(" \t\r\n\"',;")
    #: Most bytes of one `.env*` file read for the values to redact; most such files.
    ENV_MAX: ClassVar[int] = 256 * 1024
    ENV_FILES: ClassVar[int] = 32
    #: A `.env` line's value: `NAME=value`, `export NAME="va\\"lue"`, `NAME='value' # a comment`, `NAME=va#lue # a comment` (`#`
    #: starts a comment only after a space, as dotenv reads it).
    ENV_LINE: ClassVar[re.Pattern[str]] = re.compile(
        r"""^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_.-]*\s*=\s*(?:"(?P<double>(?:[^"\\]|\\.)*)"|'(?P<single>[^']*)'|(?P<bare>.*?)(?:\s+#.*)?\s*$)""")
    #: `git status --porcelain` two-letter codes of an unresolved merge.
    CONFLICTS: ClassVar[frozenset[str]] = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})
    #: File operations one PC runs at once; how long one waits for its turn.
    AT_ONCE: ClassVar[threading.BoundedSemaphore] = threading.BoundedSemaphore(2)
    WAIT_SECONDS: ClassVar[float] = 10.0

    @staticmethod
    def parts(path: str) -> tuple[str, ...]:
        """`path`'s names ("" none), each checked where the request parsed (`ProjectPath`, `FolderPath`)."""
        return tuple(path.split("/")) if path else ()

    @classmethod
    def of(cls, root: Path, request: ProjectFilesBase, *, secrets: tuple[str, ...], environment: dict[str, str], places: PlaceMap,
           untrusted: tuple[Path, ...]) -> "ProjectFolder":
        return cls(root=root.resolve(), base=cls.parts(request.base), secrets=secrets, environment=environment, places=places,
                   untrusted=tuple(folder.resolve() for folder in untrusted))

    @property
    def folder(self) -> Path:
        return self.root.joinpath(*self.base)

    @property
    def git(self) -> GitRun:
        return GitRun(root=self.root, environment=self.environment)

    def answer(self, request: ProjectFilesBase) -> MachineAgentAnswer:
        """The answer to one request, at most AT_ONCE at a time; a missing, linked, aliased or closed entry refused in one
        sentence (PermissionError)."""
        if not self.AT_ONCE.acquire(timeout=self.WAIT_SECONDS):
            raise PermissionError("this PC is busy reading other project files; ask again in a moment")
        try:
            return self._answer(request)
        except PermissionError:
            raise
        except OSError as error:
            raise PermissionError(f"{getattr(request, 'path', '') or 'the project folder'}: no plain file or folder there (links are never followed)") from error
        finally:
            self.AT_ONCE.release()

    def _answer(self, request: ProjectFilesBase) -> MachineAgentAnswer:
        match request:
            case ProjectFilesRequest():
                entries, hidden, truncated = self.entries(request.path)
                return MachineAgentAnswer(request_id=request.id, files=entries, hidden=hidden, truncated=truncated)
            case ProjectFileRequest():
                return MachineAgentAnswer(request_id=request.id, content=self.content(request.path, request.offset))
            case ProjectFindRequest():
                found, truncated = self.find(request.query)
                return MachineAgentAnswer(request_id=request.id, found=found, truncated=truncated)
            case ProjectFileBytesRequest():
                return MachineAgentAnswer(request_id=request.id, part=self.part(request.path, request.offset, request.length))
        raise TypeError(f"not a project files request: {request.op}")

    def entries(self, path: str) -> tuple[tuple[ProjectFileEntry, ...], int, bool]:
        """Folder `path`'s entries, folders first then files, A to Z; how many were left out; whether it holds more."""
        parts = self.parts(path)
        with self._opened(parts) as directory:
            kept: list[tuple[str, os.stat_result]] = []
            hidden = 0
            for name in directory.names():
                facts = None if project_hidden(name) or self._closed(directory.path / name) else directory.stat(name)
                if facts is None or PinnedDirectory.link_like(facts) or not (stat.S_ISDIR(facts.st_mode) or stat.S_ISREG(facts.st_mode)):
                    hidden += 1
                    continue
                kept.append((name, facts))
            marks = self.marks(directory.path)
        kept.sort(key=lambda item: (not stat.S_ISDIR(item[1].st_mode), item[0].casefold(), item[0]))
        entries = tuple(self._entry(name, facts, marks.get(name)) for name, facts in kept[:PROJECT_FILES_MAX])
        return entries, hidden, len(kept) > PROJECT_FILES_MAX

    def content(self, path: str, offset: int = 0) -> ProjectFileContent:
        """File `path`: its text from `offset` (redacted, at most PROJECT_TEXT_MAX bytes cut where `page` says), its
        image, or its facts alone."""
        with self._file(path) as (handle, facts, directory):
            head = handle.read(self.HEAD)
            name = self.parts(path)[-1]
            facts_of = {"path": path, "size": facts.st_size, "modified_at": self._time(facts), "git": self.marks(directory.path, name).get(name)}
            if (content_type := image_type(head)) is not None:
                if facts.st_size > MACHINE_AGENT_MEDIA:
                    return ProjectFileContent(kind="too_large", **facts_of)
                data = head + handle.read(MACHINE_AGENT_MEDIA + 1 - len(head))
                return ProjectFileContent(kind="image", content_type=content_type, data=base64.b64encode(data[:MACHINE_AGENT_MEDIA]).decode("ascii"), **facts_of)
            if not self.text(head):
                return ProjectFileContent(kind="binary", **facts_of)
            offset = min(offset, facts.st_size)
            handle.seek(max(offset - 1, 0))
            before = handle.read(1) if offset else b"\n"
            data = handle.read(PROJECT_TEXT_MAX + 1)
        if (values := self.env_values()) is None:
            # An environment file it cannot read for its values: what they would mask cannot be told apart, so no text leaves.
            return ProjectFileContent(kind="binary", **facts_of)
        secrets = (*self.secrets, *values)
        start, end = self.page(data, before, tuple(secret.encode() for secret in secrets))
        return ProjectFileContent(kind="text", text=redact(data[start:end].decode("utf-8", errors="replace"), secrets), truncated=offset + end < facts.st_size,
                                  offset=offset + start, end=offset + end, **facts_of)

    @classmethod
    def page(cls, data: bytes, before: bytes, secrets: tuple[bytes, ...]) -> tuple[int, int]:
        """The bytes of `data` (read from a file, PROJECT_TEXT_MAX + 1 at most, `before`: the byte ahead of them) a page
        shows, so that neither of its cuts splits a secret: it starts after a line end, else after a word break (the
        cut word left out, at most CUT_TAIL bytes of it), and ends at its last line end, else its last word break, when
        `data` runs past PROJECT_TEXT_MAX. A secret either cut still split is left out too. A page asked from the end of
        the one before starts where it ended: nothing between them lost."""
        breaks = cls.KEY_BREAKS
        start = 0
        if before != b"\n":
            if chr(before[0]) not in breaks:
                start = next((size for size in range(min(cls.CUT_TAIL, len(data))) if chr(data[size]) in breaks), min(cls.CUT_TAIL, len(data)))
            start += max((size for secret in secrets for size in range(min(len(secret) - 1, len(data) - start), 3, -1)
                          if data[start:].startswith(secret[-size:])), default=0)
        if len(data) <= PROJECT_TEXT_MAX:
            return start, len(data)
        window = data[start:PROJECT_TEXT_MAX]
        if (line := window.rfind(b"\n")) >= 0:
            return start, start + line + 1
        run = next((size for size in range(min(cls.CUT_TAIL, len(window))) if chr(window[-1 - size]) in breaks), min(cls.CUT_TAIL, len(window)))
        window = window[:len(window) - run]
        run += max((size for secret in secrets for size in range(min(len(secret) - 1, len(window)), 3, -1) if window.endswith(secret[:size])), default=0)
        return start, max(start, PROJECT_TEXT_MAX - run)

    def part(self, path: str, offset: int, length: int) -> ProjectFileBytes:
        """`length` raw bytes of file `path` from `offset` (none past its end), never redacted: refused for a file `content`
        shows as text (`text`), whose bytes leave only redacted."""
        with self._file(path) as (handle, facts, _):
            if self.text(handle.read(self.HEAD)):
                raise PermissionError(f"{path} is shown as text, redacted: its raw bytes never leave this PC")
            handle.seek(offset)
            data = handle.read(min(length, max(facts.st_size - offset, 0)))
        return ProjectFileBytes(path=path, size=facts.st_size, modified_at=self._time(facts), offset=offset, data=base64.b64encode(data).decode("ascii"))

    @classmethod
    def text(cls, head: bytes) -> bool:
        """Whether a file whose first bytes are `head` is shown as text: no image or document they prove, no NUL among them."""
        return image_type(head) is None and not head.startswith(cls.DOCUMENT_HEADS) and b"\x00" not in head

    def env_values(self) -> tuple[str, ...] | None:
        """Every value of 8 characters or more in the project folder's own `.env*` files (its vault's secrets are
        written to its `.env`), longest first: masked in any text shown, as the PC's own secrets are. A `.env*` that is a
        link is followed for its values only (never shown); one that cannot be read as a plain file → None (fail closed)."""
        values: set[str] = set()
        flags = os.O_RDONLY | PinnedDirectory.NONBLOCK | getattr(os, "O_BINARY", 0)
        with self._opened(()) as directory:
            for name in sorted(name for name in directory.names() if name.casefold().startswith(".env"))[:self.ENV_FILES]:
                try:
                    descriptor = os.open(directory.path / name, flags)
                except FileNotFoundError:
                    continue   # a link to nothing holds no value
                except OSError:
                    return None
                with os.fdopen(descriptor, "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        if stat.S_ISDIR(os.fstat(handle.fileno()).st_mode):
                            continue
                        return None
                    for line in handle.read(self.ENV_MAX).decode("utf-8", errors="replace").splitlines():
                        if (match := self.ENV_LINE.match(line)) is not None:
                            value = match["double"].replace('\\"', '"') if match["double"] is not None else next(group for group in (match["single"], match["bare"]) if group is not None)
                            if len(value) >= 8:
                                values.add(value)
        return tuple(sorted(values, key=len, reverse=True))

    def find(self, query: str) -> tuple[tuple[str, ...], bool]:
        """Files whose name holds `query` (casefolded), those starting with it first, then the shorter paths: the
        repository's tracked and untracked-not-ignored files (submodules' included) when git may run here, else a walk
        of at most WALK_MAX entries; dependency folders left out, FIND_SECONDS at most. Every part of a found path
        checked as a listing would."""
        wanted, deadline = query.casefold(), time.monotonic() + self.FIND_SECONDS
        listed = self._listed(deadline)
        paths, cut = listed if listed is not None else self._walk(deadline)
        matches = [path for path in paths if wanted in path.rsplit("/", 1)[-1].casefold()]
        matches.sort(key=lambda path: (not path.rsplit("/", 1)[-1].casefold().startswith(wanted), len(path), path))
        found, plain = [], PlainPaths(folder=self.folder)
        for path in matches:
            if len(found) == PROJECT_FOUND_MAX:
                return tuple(found), True
            parts = path.split("/")
            if not any(project_hidden(part) or part in DEPENDENCY_FOLDERS for part in parts) and not self._closed(self.folder / path) and plain.holds(path):
                found.append(path)
        return tuple(found), cut

    def marks(self, folder: Path, name: str = ".") -> dict[str, GitMark]:
        """The git mark of each entry of `folder` (or of entry `name` alone) as its own repository says it: a folder
        holding marked paths is `modified` (never for a path the view hides or the owner's levels close); an untracked
        folder `untracked`. Empty where git may not run, outside a repository or past STATUS_SECONDS."""
        top = self._repository(folder)
        said = self.git(folder, "status", "--porcelain=v1", "-z", "--untracked-files=normal", "--", name, seconds=self.STATUS_SECONDS) if top is not None else None
        if said is None or said[0] != 0 or top is None:
            return {}
        prefix = "" if top == folder else folder.relative_to(top).as_posix() + "/"
        marks: dict[str, GitMark] = {}
        records = iter(said[1].split(b"\x00"))
        for record in records:
            if len(record) < 4:
                continue
            code, path = record[:2].decode("ascii", errors="replace"), os.fsdecode(record[3:])
            if "R" in code or "C" in code:
                next(records, None)   # the path it came from
            if not path.startswith(prefix):
                continue
            entry, _, beneath = path[len(prefix):].rstrip("/").partition("/")
            if beneath:
                if not any(project_hidden(part) for part in beneath.split("/")) and not self._closed(folder / entry / beneath):
                    marks.setdefault(entry, "modified")
            else:
                marks[entry] = self._mark(code)
        return marks

    def _repository(self, folder: Path) -> Path | None:
        """The top of `folder`'s repository (at or below the agent root) when git may run there, else None."""
        if any(folder == place or folder.is_relative_to(place) for place in self.untrusted):
            return None
        top = next((above for above in (folder, *folder.parents) if above.is_relative_to(self.root) and os.path.lexists(above / ".git")), None)
        return top if top is not None and self.git.usable(folder) else None

    def _listed(self, deadline: float) -> tuple[list[str], bool] | None:
        """The repository's tracked and untracked-not-ignored files beneath the project folder, None where git may not
        run or did not answer in time."""
        if self._repository(self.folder) is None:
            return None
        tracked = self.git(self.folder, "ls-files", "-z", "--cached", "--recurse-submodules", seconds=max(deadline - time.monotonic(), 0.1))
        others = self.git(self.folder, "ls-files", "-z", "--others", "--exclude-standard", seconds=max(deadline - time.monotonic(), 0.1)) if tracked else None
        if tracked is None or others is None or tracked[0] != 0 or others[0] != 0:
            return None
        return sorted({os.fsdecode(item) for item in (tracked[1] + others[1]).split(b"\x00") if item}), False

    @classmethod
    def _mark(cls, code: str) -> GitMark:
        if code in cls.CONFLICTS:
            return "conflict"
        if code == "??":
            return "untracked"
        if "R" in code:
            return "renamed"
        if "D" in code:
            return "deleted"
        return "added" if code[0] in "AC" else "modified"

    def _walk(self, deadline: float) -> tuple[list[str], bool]:
        """Every plain file beneath the project folder, breadth first, past hidden names, credential stores, dependency
        folders and links; True when it stopped at WALK_MAX entries or the deadline."""
        paths: list[str] = []
        queue, seen = deque([""]), 0
        while queue:
            current = queue.popleft()
            try:
                with os.scandir(self.folder / current if current else self.folder) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > self.WALK_MAX or time.monotonic() > deadline:
                            return paths, True
                        if project_hidden(entry.name) or entry.is_symlink():
                            continue
                        relative = f"{current}/{entry.name}" if current else entry.name
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in DEPENDENCY_FOLDERS:
                                queue.append(relative)
                        elif entry.is_file(follow_symlinks=False):
                            paths.append(relative)
            except OSError:
                continue
        return paths, False

    @contextmanager
    def _file(self, path: str) -> Iterator[tuple[BinaryIO, os.stat_result, PinnedDirectory]]:
        """Plain file `path` opened through its folder's pinned handle (`_opened`; never a link, never closed by the owner's
        levels), its facts, and that folder."""
        *folder, name = self.parts(path)
        with self._opened(tuple(folder)) as directory:
            if name not in directory.names() or self._closed(directory.path / name):
                raise PermissionError(f"{path}: no such file here")
            descriptor = directory.file(name, os.O_RDONLY | PinnedDirectory.NONBLOCK | getattr(os, "O_BINARY", 0))
            with os.fdopen(descriptor, "rb") as handle:
                facts = os.fstat(handle.fileno())
                if not stat.S_ISREG(facts.st_mode):
                    raise PermissionError(f"{path} is not a plain file")
                yield handle, facts, directory

    @contextmanager
    def _opened(self, parts: tuple[str, ...]) -> Iterator[PinnedDirectory]:
        """Folder `parts` beneath the project folder, each folder on the way pinned from its parent's own handle (never a
        link), its name byte-equal to an entry of that handle's listing, never hidden nor closed by the owner's levels."""
        with ExitStack() as stack:
            directory = stack.enter_context(PinnedDirectory.open(self.root))
            for part in (*self.base, *parts):
                if project_hidden(part) or part not in directory.names() or self._closed(directory.path / part):
                    raise PermissionError(f"{'/'.join(parts) or 'the project folder'}: no such folder here")
                directory = stack.enter_context(directory.child(part))
            yield directory

    def _closed(self, target: Path) -> bool:
        return self.places.closed(target)

    def _entry(self, name: str, facts: os.stat_result, mark: GitMark | None) -> ProjectFileEntry:
        folder = stat.S_ISDIR(facts.st_mode)
        return ProjectFileEntry(name=name, kind="folder" if folder else "file", size=None if folder else facts.st_size, modified_at=self._time(facts), git=mark)

    @staticmethod
    def _time(facts: os.stat_result) -> datetime:
        return datetime.fromtimestamp(facts.st_mtime, UTC)


class PlainPaths(BaseModel):
    """Which paths beneath `folder` are reached through plain folders to a plain file, never a link (each folder
    looked at once)."""

    folder: Path
    folders: dict[str, bool] = {}

    def holds(self, path: str) -> bool:
        parts = path.split("/")
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index])
            if prefix not in self.folders:
                self.folders[prefix] = self._is(prefix, stat.S_ISDIR)
            if not self.folders[prefix]:
                return False
        return self._is(path, stat.S_ISREG)

    def _is(self, path: str, kind) -> bool:
        try:
            facts = os.lstat(self.folder / path)
        except OSError:
            return False
        return kind(facts.st_mode) and not PinnedDirectory.link_like(facts)
