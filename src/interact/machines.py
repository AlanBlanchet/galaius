"""Outbound machine connection for owner-scoped workflow agent nodes."""

import asyncio
import base64
import getpass
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import urllib.error
import urllib.request
import platform
import time
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal, Self
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

import websockets
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

from interact_core import MACHINE_AGENT_ACTIONS, MACHINE_MODELS, AgentRevisionRef, ArtifactRef, EgressAllowEntry, EgressPolicy, MachineAccelerator, MachineAgentAnswer, MachineAgentRequest, MachineCommand, MachineCommandResult, MachineDataAnswer, MachineDataRequest, MachineEvent, MachineFileEntry, MachineFileListing, MachineFileQuery, MachineFileQueryResult, MachineGitOrigin, MachineRuntime, ScriptFile, UserModelOrigin
from interact import USER_AGENT
from interact.agents.catalog import AgentCatalog
from interact.agents import providers as agent_providers
from interact.file_lock import exclusive
from interact.machine_agents import MachineAgents, WebRuns
from interact.agents.events import AgentEvent
from interact.agents.run import run_agent
from interact.agents import registry as reg
from interact.agents.profiles import ALLOWED_ENV
from interact.functions import FunctionRegistry, invoke as invoke_function
from interact.vision_env import Report, VisionWorker, ensure_vision_env
from interact import gpu_scrub, user_models
from interact.sandbox import run_pooled


class MachineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    server_url: str
    workspace_id: UUID
    machine_id: UUID
    token: SecretStr = Field(min_length=32, max_length=256)
    permission_ceiling: Literal["read_only", "full_access"]
    working_directory: Path
    #: The only folders a workflow's file ops may read or write, relative to `working_directory`
    #: - set HERE on the machine by its owner (`interact machine file-roots`), never by the
    #: server, so no workflow edit can widen them. Deny-by-default: one dedicated folder.
    file_roots: tuple[str, ...] = Field(default=("interact-files",), max_length=32)
    #: The only folders a Script step may run a file from (`ScriptFile`) — set here by the owner
    #: (`interact machine script-roots`), none by default. Never inside or around a file root: no
    #: workflow file step can write beside a script it would then run.
    script_roots: tuple[str, ...] = Field(default=(), max_length=32)
    #: Whether agent steps run here. An agent CLI on this computer can read any file its user can,
    #: whatever the file roots, so `interact login` adds a computer with them off; its owner turns
    #: them on here (`interact machine agents on`), never from the server.
    run_agents: bool = True
    #: The folders the owner lets agents be STARTED in from the web (any plain folder beneath one),
    #: relative to `working_directory` - set here (`interact machine agent-roots`), none by default.
    #: A file root's rules, and never overlapping a file or script root: no workflow file step can
    #: write an instruction file (CLAUDE.md, AGENTS.md) where an agent then starts. A starting
    #: point, not a fence: an agent CLI reads whatever its user can.
    agent_roots: tuple[str, ...] = Field(default=(), max_length=32)
    #: What an agent started from the web may do: never unrestricted unless its owner sets it here
    #: (`interact machine agent-permission full_access`); the server cannot raise it.
    agent_permission: Literal["read_only", "workspace_write", "full_access"] = "workspace_write"
    #: How long a vision model stays loaded after a step used it (0: loaded per step, GPU memory
    #: freed at once). Trades held GPU memory for ~6 s saved on each next step on that model.
    model_keep_warm_seconds: int = Field(default=300, ge=0, le=86400)
    seen_nonces: tuple[UUID, ...] = Field(default=(), max_length=10000)

    def usable_file_roots(self) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        """(the file roots safe to use, resolved; the names refused)."""
        return self._usable_roots(self.file_roots)

    def usable_script_roots(self) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        """(the script roots safe to use, resolved; the names refused): a file root's rules, and
        never overlapping a usable file root (inside it, around it, or the same folder)."""
        files = self.usable_file_roots()[0]
        usable, refused = self._usable_roots(self.script_roots)
        base = self.working_directory.resolve()
        overlapping = [root for root in usable if any(root == other or other in root.parents or root in other.parents for other in files)]
        return tuple(root for root in usable if root not in overlapping), (*refused, *(root.relative_to(base).as_posix() for root in overlapping))

    def usable_agent_roots(self) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        """(the agent roots safe to use, resolved; the names refused): a file root's rules, never
        overlapping a usable file or script root (inside it, around it, or the same folder)."""
        taken = (*self.usable_file_roots()[0], *self.usable_script_roots()[0])
        usable, refused = self._usable_roots(self.agent_roots)
        base = self.working_directory.resolve()
        overlapping = [root for root in usable if any(root == other or other in root.parents or root in other.parents for other in taken)]
        return tuple(root for root in usable if root not in overlapping), (*refused, *(root.relative_to(base).as_posix() for root in overlapping))

    def _usable_roots(self, names: tuple[str, ...]) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        """A usable root is strictly below the working directory, never the home folder or above
        it, never a symlink, never holding the runner's own folders, never under a hidden name."""
        base, home = self.working_directory.resolve(), Path.home().resolve()
        internal = (base / ".interact", MachineRunner.default_config_path().parent.resolve())
        usable, refused = [], []
        for name in names:
            declared = base / name
            root = declared.resolve()
            if declared.is_symlink() or base not in root.parents or root == home or root in home.parents or any(root == folder or root in folder.parents for folder in internal) \
                    or any(part.startswith(".") for part in root.relative_to(base).parts):  # hidden names are never reachable (`CommandFiles.inside`)
                refused.append(name)
            else:
                usable.append(root)
        return tuple(usable), tuple(refused)

    def reported_file_roots(self) -> list[str]:
        """The usable roots as the server shows them: paths relative to the working directory."""
        base = self.working_directory.resolve()
        return [root.relative_to(base).as_posix() for root in self.usable_file_roots()[0]]

    @field_validator("server_url")
    @classmethod
    def secure_server(cls, value: str) -> str:
        parsed = urlsplit(value)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme not in ({"https", "http"} if local else {"https"}) or parsed.username or parsed.password or not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("machine server URL must be HTTPS (HTTP is allowed only on loopback)")
        return value.rstrip("/")

    @model_validator(mode="after")
    def fixed_working_directory(self):
        if not self.working_directory.is_absolute() or not self.working_directory.is_dir():
            raise ValueError("machine working directory must be an existing absolute directory")
        return self


MachineAgents.model_rebuild(_types_namespace={"MachineConfig": MachineConfig})


logger = logging.getLogger(__name__)


class MachineFiles(BaseModel):
    """One of the machine owner's folder sets as a workflow reaches it — `files`, the file roots
    file steps read and write (`MachineConfig.file_roots`), or `scripts`, the script roots Script
    steps run files from (`MachineConfig.script_roots`): a path is resolved inside one of them or
    refused; a person picking a script sees a folder's entries or a file's sha256 (`listing`,
    answering a `MachineFileQuery`), never anything else."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    config: MachineConfig
    area: Literal["files", "scripts"] = "files"
    #: Characters no workflow path may hold: Windows streams / separators, drive prefixes.
    FORBIDDEN: ClassVar[re.Pattern] = re.compile(r"[:\\]|[. ]$")
    #: Most entries one folder listing carries.
    MAX_ENTRIES: ClassVar[int] = 500
    #: Largest file whose digest a listing computes (a script, not a dataset).
    MAX_DIGEST_BYTES: ClassVar[int] = 64 * 1024 * 1024

    def roots(self) -> tuple[Path, ...]:
        """The owner's roots of this area that are safe to use (`MachineConfig.usable_file_roots` / `usable_script_roots`)."""
        usable, refused = self.config.usable_script_roots() if self.area == "scripts" else self.config.usable_file_roots()
        for name in refused:
            logger.warning("%s root %s is refused: it must be a real folder inside the working directory, below the home folder%s", self.area[:-1], name,
                           ", apart from every file root" if self.area == "scripts" else "")
        return usable

    def inside(self, relative: object) -> Path:
        """`relative` inside one of the owner's file roots, else refused: an absolute or drive path,
        `..`, a symlink pointing out, a hidden (dot) name, a stream or separator character."""
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise PermissionError("a file path is relative to the machine's working directory")
        parts = Path(relative).parts
        if any(part.startswith(".") or self.FORBIDDEN.search(part) for part in parts):
            raise PermissionError(f"{relative}: hidden names, ':' '\\' and trailing dots or spaces are never reachable from a workflow")
        base = self.config.working_directory.resolve()
        target = (base / relative).resolve()
        if not any(target == root or root in target.parents for root in self.roots()):
            names, command = (self.config.script_roots, "script-roots") if self.area == "scripts" else (self.config.file_roots, "file-roots")
            raise PermissionError(f"{relative} is outside this machine's {'script' if self.area == 'scripts' else 'workflow'} folders ({', '.join(names) or 'none'}); its owner sets them with `interact machine {command}`")
        if any(part.startswith(".") for part in target.relative_to(base).parts):
            raise PermissionError(f"{relative}: hidden files and folders are never reachable from a workflow")
        return target

    def relative(self, path: Path) -> str:
        return path.relative_to(self.config.working_directory.resolve()).as_posix()

    def listing(self, relative: str) -> MachineFileListing:
        """"" lists the roots; a folder its visible entries (folders first, no links, no hidden
        names); a plain file its size, sha256 and the git checkout it sits in."""
        if not relative:
            return MachineFileListing(path="", kind="folder", entries=tuple(MachineFileEntry(name=self.relative(root), kind="folder") for root in self.roots()))
        target = self.inside(relative)
        facts = os.stat(target)
        if stat.S_ISDIR(facts.st_mode):
            entries = []
            for child in sorted(target.iterdir(), key=lambda item: item.name.lower()):
                if child.name.startswith(".") or self.FORBIDDEN.search(child.name) or child.is_symlink():
                    continue
                info = child.stat()
                if stat.S_ISDIR(info.st_mode):
                    entries.append(MachineFileEntry(name=child.name, kind="folder"))
                elif stat.S_ISREG(info.st_mode):
                    entries.append(MachineFileEntry(name=child.name, kind="file", size=info.st_size))
            entries.sort(key=lambda entry: entry.kind != "folder")
            return MachineFileListing(path=self.relative(target), kind="folder", entries=tuple(entries[:self.MAX_ENTRIES]), truncated=len(entries) > self.MAX_ENTRIES)
        content = self.script_bytes(target)
        return MachineFileListing(path=self.relative(target), kind="file", size=len(content), digest=hashlib.sha256(content).hexdigest(), git=self.git_origin(target))

    def script_bytes(self, target: Path) -> bytes:
        """The bytes of a plain file of the roots (no link, no pipe, no hard-linked twin)."""
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            facts = os.fstat(stream.fileno())
            if not stat.S_ISREG(facts.st_mode) or facts.st_nlink > 1:
                raise PermissionError(f"{target.name} is not a plain file of the machine's workflow folders")
            if facts.st_size > self.MAX_DIGEST_BYTES:
                raise PermissionError(f"{target.name} is larger than a script may be ({self.MAX_DIGEST_BYTES // (1024 * 1024)} MiB)")
            return stream.read()

    def git_origin(self, target: Path) -> MachineGitOrigin | None:
        """The git checkout `target` sits in (its remote without credentials, commit, whether the
        file differs from it), or None: not in one, or git is not installed."""
        base, git = self.config.working_directory.resolve(), shutil.which("git")
        top = next((folder for folder in target.parents if (folder / ".git").exists() and (folder == base or base in folder.parents)), None)
        if git is None or top is None:
            return None
        def ask(*arguments: str) -> str:
            return subprocess.run([git, "-C", str(top), *arguments], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        try:
            commit, remote = ask("rev-parse", "HEAD"), ask("config", "--default", "", "--get", "remote.origin.url")
            changed = ask("status", "--porcelain", "--", str(target.relative_to(top)))
        except (OSError, subprocess.SubprocessError):
            return None
        parsed = urlsplit(remote)
        if parsed.scheme and parsed.hostname:
            remote = urlunsplit((parsed.scheme, parsed.hostname + (f":{parsed.port}" if parsed.port else ""), parsed.path, "", ""))
        try:
            return MachineGitOrigin(repository=remote[:500] or None, commit=commit, path=target.relative_to(top).as_posix(), clean=not changed)
        except ValidationError:
            return None


class MachineDataFiles(BaseModel):
    """The owner's FILE ROOTS as the Data screen and the agents its owner allowed read them
    (MachineDataRequest): beneath ONE named root, walked part by part from that root's own
    descriptor with O_NOFOLLOW — a link anywhere on the way, a hidden name, anything but a folder or
    a plain single-link file is refused, even when it would land inside another root. Read-only."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    config: MachineConfig
    MAX_ENTRIES: ClassVar[int] = 500

    def roots(self) -> dict[str, Path]:
        base = self.config.working_directory.resolve()
        return {root.relative_to(base).as_posix(): root for root in self.config.usable_file_roots()[0]}

    @staticmethod
    def _parts(path: str) -> tuple[str, ...]:
        if not path:
            return ()
        if path.startswith("/") or "\\" in path:
            raise PermissionError("a path is relative to its file root")
        parts = tuple(path.split("/"))
        if any(not part or part in {".", ".."} or part.startswith(".") or MachineFiles.FORBIDDEN.search(part) for part in parts):
            raise PermissionError(f"{path}: hidden names, '.', '..', ':' '\\' and trailing dots or spaces are never reachable")
        return parts

    def _open(self, root: str, path: str, directory: bool) -> int:
        """A descriptor of `path` beneath `root` (a folder when `directory`), opened part by part."""
        try:
            top = self.roots()[root]
        except KeyError:
            raise PermissionError(f"{root} is not one of this machine's file roots; its owner sets them with `interact machine file-roots`") from None
        parts = self._parts(path)
        nofollow, flags_dir = getattr(os, "O_NOFOLLOW", 0), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        if os.open not in os.supports_dir_fd:
            # No descriptor walk on this system (Windows): every part is checked to be no link first.
            current = top
            for part in parts:
                current = current / part
                if current.is_symlink() or getattr(os.lstat(current), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                    raise PermissionError(f"{path}: links are never followed")
            return os.open(current, flags_dir if directory else os.O_RDONLY | getattr(os, "O_BINARY", 0))
        descriptor = os.open(top, flags_dir | nofollow)
        try:
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                flags = (flags_dir if directory or not last else os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)) | nofollow
                following = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _identity(facts: os.stat_result) -> str:
        return f"{facts.st_dev}:{facts.st_ino}:{facts.st_mtime_ns}"

    def answer(self, request: MachineDataRequest) -> MachineDataAnswer:
        if request.op == "list" and not request.root:
            return MachineDataAnswer(request_id=request.id, kind="folder", entries=tuple(MachineFileEntry(name=name, kind="folder") for name in self.roots()))
        if request.op == "list":
            descriptor = self._open(request.root, request.path, directory=True)
            try:
                entries, truncated = [], False
                names = sorted(os.listdir(descriptor), key=str.lower)
                for name in names:
                    if name.startswith(".") or MachineFiles.FORBIDDEN.search(name):
                        continue
                    facts = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if stat.S_ISDIR(facts.st_mode) or (stat.S_ISREG(facts.st_mode) and facts.st_nlink == 1):
                        if len(entries) == self.MAX_ENTRIES:
                            truncated = True
                            break
                        entries.append(MachineFileEntry(name=name, kind="folder" if stat.S_ISDIR(facts.st_mode) else "file", size=None if stat.S_ISDIR(facts.st_mode) else facts.st_size))
                entries.sort(key=lambda entry: (entry.kind != "folder", entry.name.lower()))
                return MachineDataAnswer(request_id=request.id, kind="folder", entries=tuple(entries), truncated=truncated)
            finally:
                os.close(descriptor)
        descriptor = self._open(request.root, request.path, directory=False)
        try:
            facts = os.fstat(descriptor)
            if stat.S_ISDIR(facts.st_mode) and request.op == "stat":
                return MachineDataAnswer(request_id=request.id, kind="folder", modified_at=datetime.fromtimestamp(facts.st_mtime, UTC), identity=self._identity(facts))
            if not stat.S_ISREG(facts.st_mode) or facts.st_nlink != 1:
                raise PermissionError(f"{request.path}: only plain files are read")
            facts_answer = {"request_id": request.id, "kind": "file", "size": facts.st_size, "modified_at": datetime.fromtimestamp(facts.st_mtime, UTC), "identity": self._identity(facts)}
            if request.op == "stat":
                return MachineDataAnswer(**facts_answer)
            if hasattr(os, "pread"):
                data = os.pread(descriptor, request.length, request.offset)
            else:
                os.lseek(descriptor, request.offset, os.SEEK_SET)
                data = os.read(descriptor, request.length)
            return MachineDataAnswer(**facts_answer, offset=request.offset, data=base64.b64encode(data).decode())
        finally:
            os.close(descriptor)


class CommandFiles(MachineFiles):
    """The files one command moves: an input file (an `ArtifactRef` value) is downloaded from the
    server into `<working directory>/.interact/transfers/<run>/` and handed to the step as a local
    path; a file op reads or writes one file INSIDE one of the owner's file roots
    (`MachineConfig.file_roots`). Every byte crosses the server over HTTP with this machine's own
    token, scoped to this command while it is in flight (`MachineCommand.input_file_path` /
    `upload_path`), its sha256 checked on arrival; every file op is logged locally."""

    command: MachineCommand
    #: Ports whose value was a file, now a local path.
    fetched: frozenset[str] = frozenset()
    UNSAFE: ClassVar[re.Pattern] = re.compile(r"[^A-Za-z0-9._-]+")
    #: Most files one command may receive.
    MAX_INPUT_FILES: ClassVar[int] = 16
    #: Largest file a read step uploads (the server refuses more anyway).
    MAX_READ_BYTES: ClassVar[int] = 1024 * 1024 * 1024

    def _request(self, path: str, **options) -> urllib.request.Request:
        headers = {"Authorization": f"Bearer {self.config.token.get_secret_value()}", "User-Agent": USER_AGENT, **options.pop("headers", {})}
        return urllib.request.Request(f"{self.config.server_url}{path}", headers=headers, **options)

    @property
    def inbox(self) -> Path:
        """Where this command's received files land: the runner's own folder, never a
        workflow-chosen path; removed when the command ends (`discard`)."""
        return self.config.working_directory.resolve() / ".interact" / "transfers" / str(self.command.run_id) / str(self.command.id)

    def _inbox(self) -> Path:
        self.inbox.mkdir(parents=True, exist_ok=True)
        return self.inbox

    def discard(self) -> None:
        """The received copies are the step's inputs only: gone once it has run."""
        shutil.rmtree(self.inbox, ignore_errors=True)
        try:
            self.inbox.parent.rmdir()
        except OSError:
            pass

    def _audit(self, op: str, path: Path, digest: str, size: int) -> None:
        MachineRunner.audit("file-audit.log", {"op": op, "path": str(path), "digest": digest, "size": size, "run_id": str(self.command.run_id),
                                                "workflow_id": str(self.command.workflow.key.id), "node_id": str(self.command.node_id),
                                                "initiator_account": None if self.command.initiator_account is None else str(self.command.initiator_account)})

    def fetch_inputs(self) -> "CommandFiles":
        """Every file input downloaded and checked; the command's inputs now hold local paths. A
        step this machine will refuse anyway (a write on a read-only machine) downloads nothing."""
        if self.command.impl.kind == "builtin" and self.command.impl.op == "write_artifact" and self.config.permission_ceiling != "full_access":
            raise PermissionError("this machine is read-only: it cannot save files")
        inputs, fetched, count = dict(self.command.inputs), set(), 0
        for port, value in self.command.inputs.items():
            items = value if isinstance(value, list) else [value]
            refs = [self._artifact(item) for item in items]
            if not items or any(ref is None for ref in refs):
                continue
            count += len(refs)
            if count > self.MAX_INPUT_FILES:
                raise PermissionError(f"a step receives at most {self.MAX_INPUT_FILES} files")
            paths = [str(self._download(port, index, ref)) for index, ref in enumerate(refs)]
            inputs[port] = paths if isinstance(value, list) else paths[0]
            fetched.add(port)
        return self.model_copy(update={"command": self.command.model_copy(update={"inputs": inputs}), "fetched": frozenset(fetched)})

    @staticmethod
    def _artifact(value: object) -> ArtifactRef | None:
        if not isinstance(value, dict) or set(value) != set(ArtifactRef.model_fields):
            return None
        try:
            return ArtifactRef.model_validate(value)
        except ValidationError:
            return None

    def _download(self, port: str, index: int, ref: ArtifactRef) -> Path:
        target = self._inbox() / f"{ref.digest[:16]}-{self.UNSAFE.sub('_', ref.path.rsplit('/', 1)[-1]).lstrip('.') or 'file'}"
        partial = target.with_name(f".{target.name}.{os.getpid()}.part")
        digest, size = hashlib.sha256(), 0
        try:
            with urllib.request.urlopen(self._request(self.command.input_file_path(port, index)), timeout=300) as response, partial.open("wb") as stream:
                while chunk := response.read(1 << 20):
                    size += len(chunk)
                    if size > ref.size:
                        raise RuntimeError(f"{port}: the server sent more bytes than the file holds")
                    digest.update(chunk)
                    stream.write(chunk)
            if digest.hexdigest() != ref.digest:
                raise RuntimeError(f"{port}: the file arrived damaged (sha256 mismatch)")
            os.replace(partial, target)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"{port}: the server refused the file (HTTP {error.code})") from error
        finally:
            partial.unlink(missing_ok=True)
        logger.info("received %s (%d bytes, sha256 %s)", ref.path, size, ref.digest[:12])
        return target

    def _path(self) -> Path:
        """The file a file op names: a wired `path` input wins over the node's `artifact_path`."""
        return self.inside(self.command.inputs.get("path", self.command.config.get("artifact_path")))

    @staticmethod
    def _replace(target: Path, content: bytes) -> None:
        """Written beside `target`, then moved over it in one step: never through a symlink or a
        hard link planted at the destination (the link would carry the write elsewhere)."""
        try:
            existing = os.lstat(target)
        except FileNotFoundError:
            existing = None
        if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode) or existing.st_nlink > 1):
            raise PermissionError(f"{target.name} is a link or not a plain file: it is never overwritten")
        partial = target.with_name(f".{target.name}.{uuid4().hex}.part")
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)

    def write(self) -> dict[str, object]:
        """Saves the `value` input as a file (a received file copied as is, any other value as its
        text; several received files into `path` as a folder); answers what was saved. Needs the
        machine's full-access ceiling."""
        if self.config.permission_ceiling != "full_access":
            raise PermissionError("this machine is read-only: it cannot save files")
        target, value = self._path(), self.command.inputs.get("value", "")
        target.parent.mkdir(parents=True, exist_ok=True)
        if "value" in self.fetched and isinstance(value, list):
            target.mkdir(exist_ok=True)
            names = []
            for received in value:
                name = Path(received).name
                self._replace(self.inside(str((target / name).relative_to(self.config.working_directory.resolve()))), Path(received).read_bytes())
                names.append(name)
            content = json.dumps(sorted(names)).encode()
        else:
            content = Path(value).read_bytes() if "value" in self.fetched else (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).encode()
            self._replace(target, content)
        digest = hashlib.sha256(content).hexdigest()
        relative = target.relative_to(self.config.working_directory.resolve()).as_posix()
        self._audit("write", target, digest, len(content))
        logger.info("saved %s (%d bytes)", relative, len(content))
        return {"machine": str(self.config.machine_id), "path": relative, "digest": digest, "size": len(content)}

    def read(self) -> dict[str, object]:
        """Uploads one file of a file root, streamed; answers the `ArtifactRef` the server stored."""
        source = self._path()
        # Non-blocking: a named pipe planted in a root must not hang the step (refused just below).
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        facts = os.fstat(descriptor)
        if not stat.S_ISREG(facts.st_mode) or facts.st_nlink > 1:
            os.close(descriptor)
            raise PermissionError(f"{source.name} is not a plain file of the machine's workflow folders")
        with os.fdopen(descriptor, "rb") as stream:
            if facts.st_size > self.MAX_READ_BYTES:
                raise PermissionError(f"{source.name} is larger than a step may send")
            digest = hashlib.sha256()
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
            stream.seek(0)
            query = urlencode({"name": source.name})
            request = self._request(f"{self.command.upload_path}?{query}", data=stream, method="POST",
                                    headers={"x-interact-digest": digest.hexdigest(), "Content-Length": str(facts.st_size), "Content-Type": mimetypes.guess_type(source.name)[0] or "application/octet-stream"})
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    stored = json.loads(response.read())
            except urllib.error.HTTPError as error:
                raise RuntimeError(f"the server refused {source.name} (HTTP {error.code}): {error.read()[:200].decode(errors='replace')}") from error
        self._audit("read", source, digest.hexdigest(), facts.st_size)
        logger.info("sent %s (%d bytes, sha256 %s)", source.name, facts.st_size, digest.hexdigest()[:12])
        return ArtifactRef.model_validate(stored).model_dump(mode="json")


#: Runner log lines at or above this level travel to the server inside the command they belong to.
FORWARDED_LOG_LEVEL = logging.INFO


def level_word(record: logging.LogRecord) -> str:
    """The server's level vocabulary (debug / info / warn / error) for a stdlib record."""
    return {"WARNING": "warn", "CRITICAL": "error"}.get(record.levelname, record.levelname.lower())


class JsonLines(logging.Formatter):
    """One JSON object per line (journald keeps it): the server's record fields, `source=machine`,
    plus whatever the call site bound in `extra` (machine, workspace, command, run, node)."""

    _BOUND = ("machine_id", "workspace_id", "command_id", "run_id", "node_id", "action", "status", "duration_ms")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(), "level": level_word(record),
            "source": "machine", "name": record.name, "message": record.getMessage(),
        }
        payload.update({key: str(getattr(record, key)) for key in self._BOUND if getattr(record, key, None) is not None})
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = f"{type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


class CommandLogs(logging.Handler):
    """Collects the runner's own log lines while one command runs, so they reach the server as
    `progress` events with `kind: log` — inside that command's span on the run's trace."""

    def __init__(self) -> None:
        super().__init__(FORWARDED_LOG_LEVEL)
        self.lines: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.lines) < 200:
            # `at`: when the line was written; the lines travel after the step, so the send time would lie.
            self.lines.append({"kind": "log", "level": level_word(record), "logger": record.name, "text": record.getMessage()[:2000], "at": datetime.fromtimestamp(record.created, UTC).isoformat()})

    def drain(self) -> list[dict[str, object]]:
        lines, self.lines = self.lines, []
        return lines


#: Agent events a machine forwards to the server while a step runs (the step's live transcript).
FORWARDED_EVENT_KINDS: frozenset[str] = frozenset({"text", "tool", "tool_result", "thinking", "error", "rate_limit", "done"})


class EnrollmentChanged(PermissionError):
    """The machine file on disk names another enrollment (machine, workspace, server or token)
    than the connection holds: nothing runs under the old one, the runner reconnects as the new."""


class ScriptExecution(BaseModel):
    """The program a Script step runs through here, chosen from what the owner approves (language,
    code, a file's own `interpreter`): the approval preview shows it, the runner resolves it. A file
    naming its interpreter runs through that; Python declaring its packages (PEP 723) through
    `uv run --script`, which installs them into a cached throwaway environment; other inline Python
    on the runner's own interpreter, a machine's Python file on its `python3`; shell on `/bin/sh`."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    program: Literal["runner_python", "python3", "uv", "shell", "custom"]
    #: The program a file names (a command on PATH or an absolute path); only for "custom".
    interpreter: str | None = None
    #: PEP 723 inline script metadata: `# /// script` … `# ///` (the editor writes a script's packages there).
    METADATA: ClassVar[re.Pattern] = re.compile(r"(?m)^# /// script\r?\n(?:^#(?: .*)?\r?\n)*?^# ///$")
    DESCRIPTIONS: ClassVar[dict[str, str]] = {"runner_python": "interact's own Python", "python3": "python3", "uv": "uv", "shell": "/bin/sh"}

    @model_validator(mode="after")
    def custom_interpreter(self) -> Self:
        if (self.program == "custom") != (self.interpreter is not None):
            raise ValueError("only a custom program names its interpreter")
        return self

    @classmethod
    def select(cls, language: Literal["python", "shell"], source: str, file: ScriptFile | None = None) -> Self:
        if file is not None and file.interpreter is not None:
            return cls(program="custom", interpreter=file.interpreter)
        if language == "shell":
            return cls(program="shell")
        return cls(program="uv" if cls.METADATA.search(source) else "python3" if file is not None else "runner_python")

    @property
    def options(self) -> tuple[str, ...]:
        return ("run", "--quiet", "--no-project", "--script") if self.program == "uv" else ()

    @property
    def description(self) -> str:
        return " ".join((self.interpreter if self.program == "custom" else self.DESCRIPTIONS[self.program], *self.options))


class MachineRunner:
    heartbeat_seconds = 3
    reconnect_seconds = (1, 2, 5, 10, 20)

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or self.default_config_path()
        #: Next event sequence per command in flight: every event of one command, from any path, is ordered.
        self._sequences: dict[UUID, int] = {}
        #: The vision model runtime kept loaded between steps (`MachineConfig.model_keep_warm_seconds`).
        self._vision: VisionWorker | None = None
        #: File queries being answered (held so the loop does not drop them mid-answer).
        self._queries: set[asyncio.Task] = set()
        #: Agent actions already accepted (id -> expiry): each is taken once (`_answer_agent_request`).
        self._agent_requests: dict[UUID, datetime] = {}

    def _vision_worker(self, config: MachineConfig) -> VisionWorker:
        if self._vision is None or self._vision.keep_warm != config.model_keep_warm_seconds:
            if self._vision is not None:
                self._vision.close()
            self._vision = VisionWorker(config.model_keep_warm_seconds)
        return self._vision

    def _next_sequence(self, command_id: UUID) -> int:
        self._sequences[command_id] = self._sequences.get(command_id, 0) + 1
        return self._sequences[command_id]

    async def _event(self, socket, command_id: UUID, kind: str, payload: dict[str, object] | None = None) -> None:
        event = MachineEvent(command_id=command_id, sequence=self._next_sequence(command_id), kind=kind, timestamp=datetime.now(UTC), payload=payload or {})
        await socket.send(json.dumps({"type": "event", "event": event.model_dump(mode="json")}))

    @staticmethod
    def default_config_path() -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return base / "interact" / "machine.json"

    def _config_lock(self):
        """Held by every writer of the machine file (`machine.lock` beside it, never replaced)."""
        self.config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return exclusive(os.open(self.config_path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))

    def save(self, config: MachineConfig) -> None:
        with self._config_lock():
            self._save(config)

    def update(self, change: Callable[[MachineConfig], MachineConfig]) -> MachineConfig:
        """Read, merge and atomically replace under the same lock as enrollment and owner edits."""
        with self._config_lock():
            config = MachineConfig.model_validate(change(self.load()).model_dump())
            self._save(config)
            return config

    def _save(self, config: MachineConfig) -> None:
        self.config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.config_path.parent.chmod(0o700)
        temporary = self.config_path.with_name(f".{self.config_path.name}.{uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                values = config.model_dump(mode="json")
                values["token"] = config.token.get_secret_value()
                stream.write(json.dumps(values, separators=(",", ":")))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config_path)
            self.config_path.parent.chmod(0o700)
            directory = os.open(self.config_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> MachineConfig:
        info = self.config_path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError(f"machine config must be a regular 0600 file: {self.config_path}")
        return MachineConfig.model_validate_json(self.config_path.read_text(encoding="utf-8"))

    async def connect(self, config: MachineConfig) -> None:
        delay_index = 0
        while True:
            endpoint = self._channel_url(config.server_url)
            try:
                async with websockets.connect(
                    endpoint,
                    additional_headers={"Authorization": f"Bearer {config.token.get_secret_value()}"},
                    user_agent_header=USER_AGENT,
                    open_timeout=10,
                    close_timeout=3,
                    ping_interval=15,
                    max_size=1024 * 1024,
                ) as socket:
                    delay_index = 0
                    logger.info("connected to %s", endpoint, extra={"machine_id": config.machine_id, "workspace_id": config.workspace_id})
                    await socket.send(json.dumps({"type": "hello", "features": list(self.FEATURES), "runtimes": self._runtimes(config), "accelerators": self._accelerators(), "functions": self._functions(), "resources": self._resources(config.working_directory), "file_roots": self._file_roots(config)}))
                    if await self._serve(socket, config):
                        return
            except EnrollmentChanged:
                config = self.load()
                logger.warning("machine file changed its enrollment; reconnecting with it", extra={"machine_id": config.machine_id})
                continue
            except PermissionError:
                raise
            except websockets.ConnectionClosed as error:
                if error.code in {4401, 4403}:
                    logger.error("machine token was revoked or rejected; connection stopped", extra={"machine_id": config.machine_id})
                    print("Machine token was revoked or rejected; connection stopped.", file=sys.stderr)
                    return
                logger.warning("connection closed (code %s)", error.code, extra={"machine_id": config.machine_id})
            except websockets.InvalidStatus as error:
                # Refused at the handshake: a revoked or unknown token answers 401/403 before the
                # socket opens (the 4401/4403 close codes above never arrive). Stop, like a close.
                if error.response.status_code in {401, 403}:
                    logger.error("machine token was refused (HTTP %s); connection stopped", error.response.status_code, extra={"machine_id": config.machine_id})
                    print("Machine token was revoked or rejected; connection stopped.", file=sys.stderr)
                    return
                logger.warning("server refused the connection: HTTP %s", error.response.status_code, extra={"machine_id": config.machine_id})
            except (OSError, TimeoutError, websockets.InvalidHandshake) as error:
                # A server mid-deploy answers the upgrade with no or a non-101 response: retry, never exit.
                logger.warning("server unreachable: %s", error or type(error).__name__, extra={"machine_id": config.machine_id})
            delay = self.reconnect_seconds[min(delay_index, len(self.reconnect_seconds) - 1)]
            logger.info("reconnecting in %s s", delay, extra={"machine_id": config.machine_id})
            await asyncio.sleep(delay)
            delay_index += 1

    async def _serve(self, socket, config: MachineConfig) -> bool:
        # Only the receiver consumes the websocket; only the worker executes commands.
        # Ending a connection drops queued work but joins the active command before reconnecting:
        # cancelling to_thread would leave its subprocess running beside the next command.
        commands: asyncio.Queue[MachineCommand | None] = asyncio.Queue(maxsize=32)
        receiver = asyncio.create_task(self._receive(socket, config, commands))
        worker = asyncio.create_task(self._command_worker(socket, config, commands))
        heartbeat = asyncio.create_task(self._heartbeat(socket, config))
        try:
            done, _ = await asyncio.wait((receiver, worker, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            return receiver in done and receiver.result()
        finally:
            receiver.cancel()
            heartbeat.cancel()
            while not commands.empty():
                commands.get_nowait()
            commands.put_nowait(None)
            for query in self._queries:
                query.cancel()
            await asyncio.gather(receiver, heartbeat, *self._queries, return_exceptions=True)
            await asyncio.shield(worker)

    async def _command_worker(self, socket, config: MachineConfig, commands: asyncio.Queue[MachineCommand | None]) -> None:
        while (command := await commands.get()) is not None:
            await self._execute(socket, config, command)

    async def _receive(self, socket, config: MachineConfig, commands: asyncio.Queue[MachineCommand | None]) -> bool:
        async for payload in socket:
            response = json.loads(payload)
            if response.get("type") == "command":
                # A full queue holds this read, as the one-at-a-time loop did: never an exit.
                await commands.put(MachineCommand.model_validate(response["command"]))
            elif response.get("type") in {"file_query", "data_request"}:
                answer = (self._answer_file_query(socket, config, response["query"]) if response["type"] == "file_query"
                          else self._answer_data_request(socket, config, response["request"]))
                task = asyncio.create_task(answer)
                self._queries.add(task)
                task.add_done_callback(self._query_finished)
            elif response.get("type") == "agent_request":
                task = asyncio.create_task(self._answer_agent_request(socket, config, response["request"]))
                self._queries.add(task)
                task.add_done_callback(self._query_finished)
            elif response.get("type") == "revoked":
                return True
        return False

    def _query_finished(self, task: asyncio.Task) -> None:
        self._queries.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("machine query failed", exc_info=error)

    #: What this runner can do beyond the base protocol (the server's `MachineChannel.require_feature`):
    #: file queries browse the owner's script roots; a script file runs from them.
    FEATURES: ClassVar[tuple[str, ...]] = ("file_query", "script_file", "file_read", "agent_control")

    @staticmethod
    def _verify_signed(config: MachineConfig, message: MachineCommand | MachineFileQuery | MachineDataRequest | MachineAgentRequest, what: str) -> None:
        """Not expired, and signed by the server with this machine's key: the server stores
        SHA-256(token), so both sides sign with the same derived key."""
        if message.expires_at.tzinfo is None or message.expires_at <= datetime.now(UTC):
            raise PermissionError(f"{what} expired")
        unsigned = message.model_dump(mode="json", exclude={"signature"})
        key = hashlib.sha256(config.token.get_secret_value().encode()).digest()
        expected = hmac.new(key, json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, message.signature):
            raise PermissionError(f"{what} signature is invalid")

    def _current_config(self, connected: MachineConfig) -> MachineConfig:
        """Current owner permissions, or refusal. A stale connection can never restore access."""
        current = self.load()
        self._same_enrollment(current, connected)
        return current

    @staticmethod
    def _same_enrollment(current: MachineConfig, connected: MachineConfig) -> None:
        if (current.machine_id, current.workspace_id, current.server_url, current.token) != (connected.machine_id, connected.workspace_id, connected.server_url, connected.token):
            raise EnrollmentChanged("machine enrollment changed on this machine; reconnecting with it")

    async def _answer_file_query(self, socket, config: MachineConfig, payload: object) -> None:
        """What a person picking a script sees of this machine: checked like a command (this
        machine, this workspace, signed, not expired), answered from the owner's file roots only."""
        try:
            query = MachineFileQuery.model_validate(payload)
        except ValidationError:
            logger.warning("malformed file query dropped")
            return
        try:
            if query.machine.id != config.machine_id or query.workspace_id != config.workspace_id:
                raise PermissionError("file query targets another machine")
            self._verify_signed(config, query, "file query")
            listing = await asyncio.to_thread(MachineFiles(config=self._current_config(config), area="scripts").listing, query.path)
            logger.info("file query %s: %s", query.path or "(roots)", listing.kind)
            answer = MachineFileQueryResult(query_id=query.id, listing=listing)
        except (PermissionError, OSError, ValueError) as error:
            reason = str(error) if isinstance(error, (PermissionError, ValueError)) else f"{query.path}: {error.strerror or type(error).__name__}"
            answer = MachineFileQueryResult(query_id=query.id, error=reason[:400] or type(error).__name__)
        await socket.send(json.dumps({"type": "file_listing", "result": answer.model_dump(mode="json")}))

    async def _answer_data_request(self, socket, config: MachineConfig, payload: object) -> None:
        """The Data screen reading this machine's FILE ROOTS: checked like a command (this machine,
        this workspace, signed, not expired), answered beneath the named root only."""
        try:
            request = MachineDataRequest.model_validate(payload)
        except ValidationError:
            logger.warning("malformed data request dropped")
            return
        try:
            if request.machine.id != config.machine_id or request.workspace_id != config.workspace_id:
                raise PermissionError("data request targets another machine")
            self._verify_signed(config, request, "data request")
            answer = await asyncio.to_thread(MachineDataFiles(config=self._current_config(config)).answer, request)
            logger.info("data %s %s/%s", request.op, request.root or "(roots)", request.path)
        except (PermissionError, OSError, ValueError) as error:
            reason = str(error) if isinstance(error, (PermissionError, ValueError)) else f"{request.path}: {error.strerror or type(error).__name__}"
            answer = MachineDataAnswer(request_id=request.id, error=reason[:400] or type(error).__name__)
        await socket.send(json.dumps({"type": "data_answer", "result": answer.model_dump(mode="json")}))

    async def _answer_agent_request(self, socket, config: MachineConfig, payload: object) -> None:
        """The owner driving agents here from the web (`MachineAgents`): checked like a command (this
        machine, this workspace, signed, not expired; an action's id accepted once), answered from
        the owner's CURRENT settings on this machine."""
        try:
            request = MachineAgentRequest.model_validate(payload)
        except ValidationError:
            logger.warning("malformed agent request dropped")
            return
        try:
            if request.machine.id != config.machine_id or request.workspace_id != config.workspace_id:
                raise PermissionError("agent request targets another machine")
            self._verify_signed(config, request, "agent request")
            if request.op in MACHINE_AGENT_ACTIONS:
                now = datetime.now(UTC)
                self._agent_requests = {key: until for key, until in self._agent_requests.items() if until > now}
                if request.id in self._agent_requests:
                    raise PermissionError("agent request was already used")
                self._agent_requests[request.id] = request.expires_at
            current = self._current_config(config)
            agents = MachineAgents(config=current, runs=WebRuns(path=self.default_config_path().with_name("machine-agent-runs.json")), environment=self._safe_environment())
            answer = await asyncio.to_thread(agents.answer, request)
            logger.info("agent %s %s", request.op, answer.run_id or request.run_id or request.root or "(folders)")
        except (PermissionError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            reason = str(error) if isinstance(error, (PermissionError, ValueError, RuntimeError)) else f"{type(error).__name__}: {error}"
            answer = MachineAgentAnswer(request_id=request.id, error=reason[:400] or type(error).__name__)
        if request.op in MACHINE_AGENT_ACTIONS:
            # The whole brief / message stays HERE, in the owner's local log; the server keeps a digest.
            self.audit("agents.log", {"op": request.op, "account": str(request.initiator_account), "root": request.root, "path": request.path, "role": request.role,
                                      "provider": request.provider, "run_id": str(answer.run_id or request.run_id or ""), "text": request.text, "error": answer.error})
        await socket.send(json.dumps({"type": "agent_answer", "result": answer.model_dump(mode="json")}))

    def _accept_command(self, config: MachineConfig, connected: MachineConfig, command: MachineCommand) -> MachineConfig:
        self._same_enrollment(config, connected)
        if command.machine.id != config.machine_id:
            raise PermissionError("machine command targets another machine")
        # A pooled command legitimately carries a DIFFERENT workspace_id (the TENANT borrowing
        # this machine) than `config.workspace_id` (this machine's OWNER) — the HMAC signature
        # below, keyed on this machine's own token, is what proves the OWNER's server authorized
        # it; the equality check stays absolute for every non-pooled command (today's 1:1 shape,
        # unchanged: threat-model-shared-compute-2026-09-24.md threat #7's invariant).
        if command.tenancy == "owner" and command.workspace_id != config.workspace_id:
            raise PermissionError("machine command targets another workspace")
        if command.tenancy == "pooled" and command.impl.kind != "script":
            # Function/model pooled dispatch needs its own sandboxed fetch/invoke path
            # (interact.model_safety's safetensors gate, a sandboxed function call) — not built
            # yet; refuse rather than silently run either unsandboxed or not at all.
            raise PermissionError(f"pooled dispatch of a {command.impl.kind!r} step is not supported yet; script only")
        self._verify_signed(config, command, "machine command")
        if command.nonce in config.seen_nonces:
            raise PermissionError("machine command nonce was already used")
        return config.model_copy(update={"seen_nonces": (*config.seen_nonces[-9998:], command.nonce)})

    async def _execute(self, socket, config: MachineConfig, command: MachineCommand) -> None:
        config = await asyncio.to_thread(self.update, lambda current: self._accept_command(current, config, command))
        bound = {"machine_id": config.machine_id, "command_id": command.id, "run_id": command.run_id, "node_id": command.node_id, "action": command.impl.kind}
        # Everything the runner logs from here to the result reaches the server inside this step.
        forwarded = CommandLogs()
        package = logging.getLogger("interact")
        package.addHandler(forwarded)
        logger.info("running %s step", command.impl.kind, extra=bound)
        await self._event(socket, command.id, "started")
        started = time.perf_counter()
        try:
            try:
                if command.tenancy == "pooled":
                    output = json.dumps(await asyncio.to_thread(self._run_script_pooled, command, config), separators=(",", ":"))
                elif command.impl.kind == "agent":
                    if not config.run_agents:
                        raise PermissionError("agent steps are off on this computer; its owner turns them on there with `interact machine agents on`")
                    output = await self._run_agent(command.impl.agent, str(command.inputs["task"]), config, socket, command.id)
                else:
                    # Files first: a step never sees a server reference, only a checked local copy.
                    files = CommandFiles(config=config, command=command)
                    try:
                        files = await asyncio.to_thread(files.fetch_inputs)
                        if command.impl.kind == "builtin":
                            answer = await asyncio.to_thread(files.write if command.impl.op == "write_artifact" else files.read)
                        elif command.impl.kind == "model":
                            answer = await asyncio.to_thread(self._run_model, files.command, config, self._reporter(socket, command.id))
                        else:
                            answer = await asyncio.to_thread({"function": self._run_function, "script": self._run_script}[command.impl.kind], files.command, config)
                    finally:
                        files.discard()
                    output = json.dumps(answer, separators=(",", ":"))
                result = MachineCommandResult(command_id=command.id, nonce=command.nonce, status="succeeded", result=output[:256 * 1024])
            except Exception as error:
                logger.error("%s step failed", command.impl.kind, exc_info=error, extra=bound)
                result = MachineCommandResult(command_id=command.id, nonce=command.nonce, status="failed", error=str(error)[:400] or type(error).__name__)
        finally:
            # Threat #1c: a pooled tenant's VRAM must be scrubbed whether its run succeeded,
            # failed, or crashed — never conditioned on the run's own outcome.
            if command.tenancy == "pooled":
                await self._scrub_gpu_if_present(socket, command.id)
        duration = round((time.perf_counter() - started) * 1000)
        logger.info("%s step %s in %d ms", command.impl.kind, result.status, duration, extra={**bound, "status": result.status, "duration_ms": duration})
        package.removeHandler(forwarded)
        try:
            for line in forwarded.drain():
                await self._event(socket, command.id, "progress", line)
            await socket.send(json.dumps({"type": "result", "result": result.model_dump(mode="json")}))
        finally:
            self._sequences.pop(command.id, None)

    def _reporter(self, socket, command_id: UUID) -> Report:
        """What a step running on a worker thread uses to tell the server its phase (a `phase`
        progress event, `interact_core.MACHINE_PHASES`); never fails the step."""
        loop = asyncio.get_running_loop()

        def report(phase: str, detail: str) -> None:
            try:
                asyncio.run_coroutine_threadsafe(self._event(socket, command_id, "progress", {"kind": "phase", "phase": phase, "detail": detail[:240]}), loop).result(timeout=10)
            except Exception:
                logger.warning("step phase not sent", exc_info=True)
        return report

    def _run_model(self, command: MachineCommand, config: MachineConfig, report: Report = lambda phase, detail: None) -> dict[str, object]:
        """Run one registered model on paths inside the machine's approved workspace: the vendor
        catalog (`interact_core.MACHINE_MODELS`, `provider == "huggingface"`) below, or — the
        whole point of "run inferences on any kind of model" — a workspace's OWN registered model
        (`provider == "workspace"`, `_run_user_model`), dispatched first since its `impl.model` is
        a `UserModel` UUID, never a `MACHINE_MODELS` key.

        Inference itself runs in `vision_infer.py`, inside the isolated venv `ensure_vision_env`
        provisions under `~/.interact/models/env` — torch and transformers never load into this
        process, so a machine with no vision node configured never pays for them."""
        if command.impl.provider == "workspace":
            return self._run_user_model(command, config, report)
        model_id = command.impl.model
        images = command.inputs.get("images", command.config.get("images", ()))
        score_threshold = command.config.get("score_threshold", 0.5)
        # The one registry (interact_core.MACHINE_MODELS); the runner script names tasks its own way.
        spec = MACHINE_MODELS[model_id]
        task_name, license_name = {"object-detection": "detection", "image-segmentation": "segmentation"}[spec.task], spec.license
        cache_root = Path.home() / ".interact" / "models"
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache_root.chmod(0o700)

        run_dir = cache_root / "runs" / str(command.run_id) / str(command.node_id)
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir.chmod(0o700)
        image_paths = [str(self._model_input_path(raw_path, config.working_directory)) for raw_path in (images if isinstance(images, list) else [images])]

        logger.info("%s (%s) on %d image(s), score threshold %s", model_id, spec.task, len(image_paths), score_threshold)
        python = ensure_vision_env(cache_root, report=report)
        request_path = run_dir / "request.json"
        request_path.write_text(json.dumps({
            "model": model_id,
            "task": task_name,
            "license": license_name,
            "cache_root": str(cache_root),
            "run_dir": str(run_dir),
            "score_threshold": score_threshold,
            "image_paths": image_paths,
        }), encoding="utf-8")
        output = self._vision_worker(config).run(python, request_path, report)
        found = {key: len(value) for key, value in output.items() if isinstance(value, list)} if isinstance(output, dict) else {}
        logger.info("%s answered: %s", model_id, ", ".join(f"{count} {key}" for key, count in found.items()) or "no list output")
        return output

    def _run_user_model(self, command: MachineCommand, config: MachineConfig, report: Report = lambda phase, detail: None) -> dict[str, object]:
        """Run one workspace-registered model (`interact_core.UserModel`, `impl.provider ==
        "workspace"`): the server resolved its CURRENT `origin` at dispatch time and signed it
        into `command.config["_user_model"]` (`server.machines.channel.MachineChannel.
        _dispatch_sync`, never stale — a later edit to the registration is read on the NEXT run,
        same as every other resource this app resolves at dispatch time). This machine
        independently re-validates that shape (`UserModelOrigin.model_validate`, never a bare dict
        trusted as-is), fetches the weights (`interact.user_models`, byte-verified safetensors-only
        inside that call) and only then hands a LOCAL path to the same isolated `vision_env`
        subprocess the vendor catalog models use above — origin bytes never touch this process,
        torch/transformers still never import here.

        Two capability boundaries this machine refuses past, by name, rather than pretending to
        cross them: a `docker_image` origin is pulled (real, useful — proves the image is still
        reachable) but never RUN (no proven container I/O contract, no verified GPU passthrough);
        a `trusts_remote_code` origin is fetched and byte-verified but never LOADED (loading it
        would execute the repo's own Python, which this machine only sandboxes at the same gVisor
        tier a script node gets — unverified for GPU passthrough on this build, threat-model
        `pool.py` docstring). Both refusals happen AFTER their real, safe half of the work, never
        instead of it."""
        payload = command.config.get("_user_model")
        if not isinstance(payload, dict) or not isinstance(payload.get("origin"), dict):
            raise RuntimeError("server did not resolve this workspace model's origin; reconnect and re-run")
        origin = UserModelOrigin.model_validate(payload["origin"])
        licence = str(payload.get("licence") or "")
        task = command.impl.task
        if task not in user_models.SUPPORTED_TASKS:
            raise RuntimeError(f"this machine's runner does not support task {task!r} yet (supported: {', '.join(sorted(user_models.SUPPORTED_TASKS))})")
        cache_root = Path.home() / ".interact" / "models"
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache_root.chmod(0o700)
        label = f"{origin.repo_id} @ {origin.revision or 'main'}" if origin.repo_id else command.impl.model
        if origin.kind == "docker_image":
            pulled = user_models.pull_docker_image(origin)
            raise RuntimeError(f"pulled and verified {pulled}; running a Docker-image model is not implemented on this machine yet")
        logger.info("fetching workspace model %s (%s) from %s", label, task, origin.kind)
        weights_dir = user_models.fetch_weights_dir(origin, command.impl.model, cache_root, config.server_url, config.token.get_secret_value())
        if origin.trusts_remote_code:
            raise RuntimeError(f"fetched and byte-verified {label}'s weights, but this machine has no sandboxed runtime for trust_remote_code models yet")

        images = command.inputs.get("images", command.config.get("images", ()))
        score_threshold = command.config.get("score_threshold", 0.5)
        task_name = {"object-detection": "detection", "image-segmentation": "segmentation"}[task]
        run_dir = cache_root / "runs" / str(command.run_id) / str(command.node_id)
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir.chmod(0o700)
        image_paths = [str(self._model_input_path(raw_path, config.working_directory)) for raw_path in (images if isinstance(images, list) else [images])]

        logger.info("%s (%s) on %d image(s), score threshold %s", label, task, len(image_paths), score_threshold)
        python = ensure_vision_env(cache_root, report=report)
        request_path = run_dir / "request.json"
        request_path.write_text(json.dumps({
            "model": label,
            "task": task_name,
            "license": licence,
            "cache_root": str(cache_root),
            "run_dir": str(run_dir),
            "score_threshold": score_threshold,
            "image_paths": image_paths,
            "weights_dir": str(weights_dir),
        }), encoding="utf-8")
        output = self._vision_worker(config).run(python, request_path, report)
        found = {key: len(value) for key, value in output.items() if isinstance(value, list)} if isinstance(output, dict) else {}
        logger.info("%s answered: %s", label, ", ".join(f"{count} {key}" for key, count in found.items()) or "no list output")
        return output

    def _run_function(self, command: MachineCommand, config: MachineConfig) -> object:
        """Runs one `@interact.function`-decorated Python callable or registered shell command,
        the inbound half of PC integration (`_run_model`'s sibling: local compute reached FROM a
        workflow). `function_version` is re-checked against the entry's OWN current content hash
        — a workflow node stays pinned to the exact signature it was wired against, so a local
        edit that changes ports fails loudly here instead of silently coercing arguments — and
        the LOCAL permission ceiling still gates a `full_access` function exactly like every
        other machine command."""
        name = command.impl.name
        entry = FunctionRegistry().get(name)
        if entry.version != command.impl.version:
            raise RuntimeError(f"function {name!r} changed on this machine since the workflow was saved; re-select it")
        if entry.permission == "full_access" and config.permission_ceiling != "full_access":
            raise PermissionError(f"function {name!r} needs full-access permission; this machine is read-only")
        # A port's constant (the node's config) first, its wired value over it.
        arguments = {**command.config, **command.inputs}
        logger.info("function %s (%s) with %s", name, entry.version[:8], ", ".join(sorted(arguments)) or "no arguments")
        return invoke_function(entry, arguments, working_directory=config.working_directory)

    def _run_script(self, command: MachineCommand, config: MachineConfig, timeout: float = 120) -> str:
        """Runs a script the machine owner has already approved by its EXACT digest — a server-side
        gate (`MachineStore.script_approved`) checked BEFORE this ever dispatches; re-verified here
        too (defense in depth: two independent checks of the same bytes, never one trusted alone):
        inline source against its digest, a file of the machine (`ScriptFile`) against the sha256
        it was picked with, read NOW (commands run one at a time, so no other step rewrites it
        between this check and the start). Threat-modeler mitigations applied: #2 mandatory
        full_access (arbitrary code exec is de facto full_access regardless of any local setting);
        #4 one audit line per ATTEMPT, written before running, so a crash still leaves a record; #5
        a real subprocess (genuine timeout kill), the same stripped credential-free environment
        `_run_agent` uses, confined to the working directory (a file: its folder or its `cwd`)."""
        language, digest = command.impl.language, command.impl.source_digest
        if config.permission_ceiling != "full_access":
            raise PermissionError("script nodes always need full-access permission; this machine is read-only")
        if command.impl.origin == "machine_file":
            spec = command.impl.script_file(command.config)
            files = MachineFiles(config=self._current_config(config), area="scripts")
            path = files.inside(spec.path)
            content = files.script_bytes(path)
            found = hashlib.sha256(content).hexdigest()
            if found != spec.file_digest:
                raise RuntimeError(f"{spec.path} changed on this machine since it was picked (its sha256 is now {found[:12]}, it was {spec.file_digest[:12]}): check the file again in the step, then its owner approves the new digest")
            self._audit_script(command, spec.path)
            logger.info("%s script file %s (%s), timeout %s s", language, spec.path, found[:12], timeout)
            argv = [*self._interpreter(language, content.decode(errors="replace"), spec), str(path), *spec.args]
            if any(Path(argv[0]).resolve() == root or root in Path(argv[0]).resolve().parents for root in MachineFiles(config=files.config).roots()):
                raise PermissionError(f"{spec.interpreter}: the program that runs a script cannot live in a folder workflow file steps write to")
            return self._run_process(argv, files.inside(spec.cwd) if spec.cwd else path.parent, timeout)
        source = str(command.config["source"])
        try:
            command.impl.check(command.config)  # the same rule the server applied: source, language and pin agree
        except ValueError as error:
            raise RuntimeError(f"{error}; refusing to run") from error
        self._audit_script(command)
        logger.info("%s script %s (%d lines), timeout %s s", language, digest[:12], source.count("\n") + 1, timeout)
        with tempfile.TemporaryDirectory(prefix="interact-script-") as scratch:
            script_path = Path(scratch) / ("script.py" if language == "python" else "script.sh")
            script_path.write_text(source, encoding="utf-8")
            script_path.chmod(0o700)
            return self._run_process([*self._interpreter(language, source), str(script_path)], config.working_directory, timeout)

    def _run_process(self, argv: list[str], cwd: Path, timeout: float) -> str:
        """One script process: its standard output is the step's result, a non-zero exit its error."""
        try:
            completed = subprocess.run(argv, cwd=cwd, env=self._safe_environment(), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"script exceeded its {timeout}s timeout") from error
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or f"script exited {completed.returncode}").strip()[-2000:])
        if completed.stderr.strip():
            logger.warning("script wrote to stderr: %s", completed.stderr.strip()[-600:])
        logger.info("script exited 0, %d bytes of output", len(completed.stdout))
        return completed.stdout

    @classmethod
    def _interpreter(cls, language: Literal["python", "shell"], source: str, spec: ScriptFile | None = None) -> list[str]:
        """Resolve the same execution description the owner sees before approval."""
        execution = ScriptExecution.select(language, source, spec)
        if execution.program == "custom":
            interpreter = execution.interpreter
            if "/" in interpreter and not Path(interpreter).is_absolute():
                raise RuntimeError(f"{interpreter}: name the program by its full path, or by a name on the machine's PATH")
            found = interpreter if Path(interpreter).is_absolute() else shutil.which(interpreter, path=cls._safe_environment().get("PATH"))
            if found is None or not os.access(found, os.X_OK):
                raise RuntimeError(f"{interpreter} is not a program this machine can run (not found on its PATH)")
        elif execution.program == "runner_python":
            found = sys.executable
        elif execution.program == "shell":
            found = "/bin/sh"
        elif execution.program == "python3":
            found = shutil.which("python3", path=cls._safe_environment().get("PATH"))
            if found is None:
                raise RuntimeError("python3 is not installed on this machine's PATH: name the interpreter in the step")
        else:
            found = shutil.which("uv")
            if found is None:
                raise RuntimeError("this script declares the packages it needs (PEP 723): install uv on this machine to run it")
        return [found, *execution.options]

    def _run_script_pooled(self, command: MachineCommand, config: MachineConfig, timeout: float = 120) -> str:
        """The POOLED sibling of `_run_script`: same digest re-check and audit log, but the
        source runs inside a fresh gVisor container (`interact.sandbox.run_pooled`) instead of a
        bare subprocess on this host — the tenant did not enroll this machine and never gets its
        real filesystem, PID namespace, or unrestricted network. `_pool_egress_allow` (a plain
        list of `{host,port}` the server stamped into `command.config`) becomes a typed
        `EgressPolicy`; empty means no network at all, the sandbox's own default."""
        language, digest, source = command.impl.language, command.impl.source_digest, str(command.config["source"])
        try:
            command.impl.check(command.config)  # the same rule the server applied: source, language and pin agree
        except ValueError as error:
            raise RuntimeError(f"{error}; refusing to run") from error
        if config.permission_ceiling != "full_access":
            raise PermissionError("script nodes always need full-access permission; this machine is read-only")
        self._audit_script(command)
        raw_allow = command.config.get("_pool_egress_allow", [])
        policy = EgressPolicy(allow=tuple(EgressAllowEntry.model_validate(entry) for entry in raw_allow)) if isinstance(raw_allow, list) else EgressPolicy()
        script_name = "script.py" if language == "python" else "script.sh"
        interpreter = "python3" if language == "python" else "/bin/sh"
        logger.info("pooled %s script %s (%d lines), timeout %s s, egress allow=%d host(s)", language, digest[:12], source.count("\n") + 1, timeout, len(policy.allow))
        result = run_pooled(run_id=command.run_id, command=[interpreter, script_name], input_files={script_name: source.encode()}, egress=policy, timeout=timeout)
        if result.exit_code != 0:
            raise RuntimeError((result.stderr or f"pooled script exited {result.exit_code}").strip()[-2000:])
        if result.stderr.strip():
            logger.warning("pooled script wrote to stderr: %s", result.stderr.strip()[-600:])
        logger.info("pooled script exited 0, %d bytes of output", len(result.stdout))
        return result.stdout

    async def _scrub_gpu_if_present(self, socket, command_id: UUID) -> None:
        """Runs after EVERY pooled command, success or failure — threat #1c: a machine reporting a
        CUDA accelerator gets its free VRAM scrubbed (`interact.gpu_scrub`) before the next tenant
        can be placed on it, and the server only trusts a fresh `GpuScrubRecord` from THIS event,
        never from the run having merely succeeded."""
        if not self._cuda_accelerators():
            return
        try:
            scrubbed_bytes = await asyncio.to_thread(gpu_scrub.scrub_all_free_memory)
            kind = "device_reset" if gpu_scrub.gpu_reset_supported() else "scrubbed"
            logger.info("pooled GPU scrub: %d bytes, kind=%s", scrubbed_bytes, kind)
            # `kind="gpu_scrub"` here is the PROGRESS PAYLOAD's own content-type tag (like
            # `CommandLogs`' `{"kind": "log", ...}`), never the outer `MachineEvent.kind` (that
            # stays "progress" — `_event`'s third argument, below); the server reads
            # `event.payload["kind"]`, not `event.kind`, to recognise this one.
            await self._event(socket, command_id, "progress", {"kind": "gpu_scrub", "accelerator_index": 0, "scrub_kind": kind})
        except gpu_scrub.CudaUnavailable as error:
            # A machine that REPORTED a CUDA accelerator but cannot actually reach the driver is a
            # machine this server must never trust with the next tenant's GPU placement — logged
            # loudly rather than silently letting `gpu_scrub_fresh` stay stale on the server side.
            logger.error("pooled GPU scrub failed, device may be left dirty for the next tenant: %s", error)

    @staticmethod
    def _audit_script(command: MachineCommand, path: str | None = None) -> None:
        """A durable local record of every script ATTEMPT (not just successes): source digest,
        the workflow/node/run that triggered it. Threat-modeler mitigation #4 — deliberately
        machine-side, next to the running process, so the record survives even a server outage;
        pairs with the server's OWN `machine_script_approvals.approved_by_account_id` for who
        allowlisted the digest in the first place."""
        MachineRunner.audit("script-audit.log", {
            "language": command.impl.language, "source_digest": command.impl.source_digest, "approval_digest": command.impl.approval_digest(command.config), "workflow_id": str(command.workflow.key.id),
            "workflow_revision": str(command.workflow.revision), "node_id": str(command.node_id), "run_id": str(command.run_id),
            **({"path": path} if path is not None else {}),
        })

    @staticmethod
    def audit(log: str, entry: dict[str, object]) -> None:
        """Appends one timestamped JSON line to a local, owner-only audit log beside the runner's
        configuration: what workflows made this machine do, surviving a server outage."""
        path = FunctionRegistry().config_path.parent / log
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": datetime.now(UTC).isoformat(), **entry}, separators=(",", ":")) + "\n")

    @staticmethod
    def _functions() -> list[dict[str, object]]:
        try:
            entries = FunctionRegistry().load()
        except PermissionError:
            return []
        return [entry.summary().model_dump(mode="json") for entry in entries]

    @staticmethod
    def _model_input_path(raw_path: str, working_directory: Path) -> Path:
        root = working_directory.resolve(strict=True)
        candidate = Path(raw_path)
        image_path = candidate.resolve(strict=True) if candidate.is_absolute() else (root / candidate).resolve(strict=True)
        if not image_path.is_relative_to(root) or not image_path.is_file():
            raise ValueError("model image paths must be files under the machine working directory")
        return image_path

    async def _run_agent(self, agent: AgentRevisionRef, task: str, config: MachineConfig, socket, command_id: UUID) -> str:
        safe_environment = self._safe_environment()
        original_environment = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(safe_environment)
            handle = await run_agent(
                None,
                task,
                agent_ref=AgentCatalog.reference(agent.id, agent.revision),
                cwd=str(config.working_directory),
                permission_mode=config.permission_ceiling,
            )
            logger.info("agent run %s started", handle.run_id, extra={"machine_id": config.machine_id, "command_id": command_id})
            await self._event(socket, command_id, "progress", {"kind": "log", "level": "info", "logger": __name__, "text": f"agent run {handle.run_id} started", "agent_run_id": handle.run_id})
            observed = 0

            async def forward(events: list[AgentEvent]) -> None:
                """Every event the server's step view reads, as it happens: what the agent said, each
                tool call WITH its arguments and — paired by `tool_id` — what the tool returned, and
                each event's token / cost figures, whose sum is the run's total."""
                nonlocal observed
                for event in events[observed:]:
                    observed += 1
                    if event.kind not in FORWARDED_EVENT_KINDS:
                        continue
                    payload = {"kind": event.kind, "text": event.text[:2000], "tool": event.tool, "tool_input": event.tool_input[:2000], "tool_id": event.tool_id,
                               "input_tokens": event.input_tokens, "output_tokens": event.output_tokens, "cost_usd": event.cost_usd}
                    await self._event(socket, command_id, "progress", {key: value for key, value in payload.items() if value not in (None, "")})

            while handle.process.returncode is None:
                await forward(reg.read_events(handle.run_id))
                await asyncio.sleep(0.3)
            exit_code = await handle.wait()
            events = reg.read_events(handle.run_id)
            # The loop exits before the final events land (the terminal "done" carries the totals).
            await forward(events)
            result = self._final_text(events)
            if exit_code != 0:
                raise RuntimeError(result or f"agent exited {exit_code}")
            if not result:
                raise RuntimeError("agent completed without a text result")
            return result
        finally:
            os.environ.clear()
            os.environ.update(original_environment)

    @staticmethod
    def _final_text(events: list[AgentEvent]) -> str:
        terminal = next((event for event in reversed(events) if event.kind == "done" and event.final_text and event.text.strip()), None)
        if terminal is not None:
            return terminal.text
        last = next((event for event in reversed(events) if event.kind == "text" and event.text.strip()), None)
        return "" if last is None else last.text

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        fixed = {"HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"}
        return {key: value for key, value in os.environ.items() if key in fixed or key in ALLOWED_ENV or key.startswith("LC_")}

    @staticmethod
    def _runtimes(config: MachineConfig) -> list[dict[str, str]]:
        """The agent CLIs the server may place agent steps on: none while agent steps are off here."""
        if not config.run_agents:
            return []
        return [MachineRuntime(provider=provider.name).model_dump(mode="json", exclude_none=True) for provider in agent_providers.PROVIDERS.values() if provider.name in {"claude", "codex"} and provider.available()]

    @staticmethod
    def _resources(working_directory: Path | None = None) -> dict[str, int]:
        """CPU count, total RAM and free disk on the machine's own working directory — the other
        half of a placement fit check next to `_accelerators`. POSIX `sysconf` (no `psutil`
        dependency, no torch import) covers Linux and macOS, the two platforms the runner
        actually ships on; `cpu_count() or 1` matches the stdlib's own documented fallback."""
        ram_mb = 0
        try:
            ram_mb = (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) // (1024 * 1024)
        except (ValueError, OSError, AttributeError):
            pass
        try:
            disk_free_gb = shutil.disk_usage(working_directory or Path.home()).free // (1024 ** 3)
        except OSError:
            disk_free_gb = 0
        return {"cpu_count": os.cpu_count() or 1, "ram_mb": ram_mb, "disk_free_gb": disk_free_gb}

    @staticmethod
    def _accelerators() -> list[dict[str, object]]:
        """GPUs on this machine, so a `model` step (detection, segmentation) defaults here.

        Torch-free: `nvidia-smi` is queried directly and Apple's unified GPU is inferred from the
        platform, never by importing torch just to ask "is there a GPU" and dragging its multi-GB
        wheel onto a machine that runs no vision node. `none` is reported explicitly (not an empty
        list) so a runner that checked and found nothing is distinguishable from an older runner
        that never reported accelerators at all."""
        found = MachineRunner._cuda_accelerators() or MachineRunner._mps_accelerator()
        accelerators = found or (MachineAccelerator(kind="none", name="none", memory_mb=0),)
        return [item.model_dump(mode="json") for item in accelerators]

    @staticmethod
    def _cuda_accelerators() -> tuple[MachineAccelerator, ...]:
        if shutil.which("nvidia-smi") is None:
            return ()
        try:
            completed = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        if completed.returncode != 0:
            return ()
        accelerators = []
        for line in completed.stdout.splitlines():
            name, _, memory = line.partition(",")
            name, memory = name.strip(), memory.strip()
            if name and memory.isdigit():
                accelerators.append(MachineAccelerator(kind="cuda", name=name, memory_mb=int(memory)))
        return tuple(accelerators)

    @staticmethod
    def _mps_accelerator() -> tuple[MachineAccelerator, ...]:
        if sys.platform != "darwin" or platform.machine() != "arm64":
            return ()
        return (MachineAccelerator(kind="mps", name="Apple GPU", memory_mb=0),)

    async def _heartbeat(self, socket, config: MachineConfig) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            await socket.send(json.dumps({"type": "heartbeat", "runtimes": self._runtimes(config), "accelerators": self._accelerators(), "functions": self._functions(), "resources": self._resources(config.working_directory), "file_roots": self._file_roots(config)}))

    def _file_roots(self, connected: MachineConfig) -> list[str]:
        """The folders file nodes may use, as the machine file on disk says NOW: an owner's
        `interact machine file-roots` change reaches the server with the next beat."""
        return self._current_config(connected).reported_file_roots()

    @staticmethod
    def _channel_url(server_url: str) -> str:
        parsed = urlsplit(server_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit((scheme, parsed.netloc, "/v1/machine-channel", "", ""))


def shell_path(current: str, shell: str | None = None, timeout: float = 10) -> str:
    """The owner's shell PATH, ahead of `current`, deduplicated.

    A user service starts with the service manager's PATH, not the shell's, so a CLI placed on
    PATH by a shell rc file (nvm's node bin, an npm prefix) is invisible to `shutil.which`: an
    installed codex went unreported. The shell is asked once, interactive + login (rc files hold
    those additions), detached from any terminal and bounded by `timeout`; rc-file noise around
    the markers is ignored, and any failure keeps `current` unchanged."""
    marker = "__interact_path__"
    try:
        completed = subprocess.run(
            [shell or os.environ.get("SHELL") or "/bin/sh", "-ilc", f'printf "{marker}%s{marker}" "$PATH"'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return current
    parts = completed.stdout.split(marker)
    found = parts[1] if len(parts) >= 3 else ""
    return ":".join(dict.fromkeys(entry for entry in (*found.split(":"), *current.split(":")) if entry))


async def connect_command(server_url: str | None, workspace_id: UUID | None, machine_id: UUID | None, token: str | None, permission_ceiling: Literal["read_only", "full_access"], working_directory: Path | None, serve: bool) -> None:
    runner = MachineRunner()
    try:
        if server_url is None and workspace_id is None and machine_id is None and token is None:
            config = runner.load()
        else:
            if server_url is None or workspace_id is None or machine_id is None:
                raise ValueError("setup needs server URL, workspace ID, and machine ID")
            secret = token or getpass.getpass("Machine token: ")
            config = MachineConfig(server_url=server_url, workspace_id=workspace_id, machine_id=machine_id, token=secret, permission_ceiling=permission_ceiling, working_directory=working_directory or Path.cwd())
            runner.save(config)
        if not serve:
            print(f"Saved machine configuration to {runner.config_path}; enable the interact-machine user service to keep it connected.")
            return
        os.environ["PATH"] = shell_path(os.environ.get("PATH", ""))
        # The service's journal gets one JSON object per line, the same fields the server stores.
        printer = logging.StreamHandler(sys.stderr)
        printer.setFormatter(JsonLines())
        package = logging.getLogger("interact")
        package.addHandler(printer)
        package.setLevel(logging.INFO)
        await runner.connect(config)
    except (OSError, PermissionError, ValidationError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
