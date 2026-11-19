"""Files the server reads and writes on this machine, inside the places its owner allowed."""

import base64
import hashlib
import json
import mimetypes
import os
import re
import urllib.error
import urllib.request
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID
from pydantic import BaseModel, ConfigDict, ValidationError
from galaius_core import ArtifactRef, MachineCommand, MachineDataAnswer, MachineDataRequest, MachineFileEntry, MachineFileListing, MachineGitOrigin, PlaceLevel
from galaius.host.atomic_file import replace_held_open
from galaius.account.tls import server_tls
from galaius.host.pinned_directory import PinnedDirectory
from galaius.machine.places.levels import IN_PLACE_WRITES, INSTRUCTION_NAMES, LEVEL_RANK, split
from galaius.machine.places.place_reviews import PlaceReviews, write_plain
import logging

from galaius.machine.connection.config import MachineConfig
from galaius.machine.connection.logs import audit

logger = logging.getLogger(__package__)


class MachineFiles(BaseModel):
    """One of the machine owner's folder sets as a workflow reaches it — `files`, the folders the
    owner set a level on (`MachineConfig.places`: `read` to read, `write_on_review` or later to
    write), or `scripts`, the script roots Script steps run files from (`MachineConfig.script_roots`):
    a path is resolved inside one of them or refused; a person picking a script sees a folder's
    entries or a file's sha256 (`listing`, answering a `MachineFileQuery`), never anything else."""

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
        """The owner's folders of this area that are safe to use: script roots, or the folders
        whose level lets a workflow read them."""
        if self.area == "files":
            return tuple(folder for folder, _ in self.config.place_map().in_force("read").values())
        usable, refused = self.config.usable_script_roots()
        for name in refused:
            logger.warning("script root %s is refused: it must be a real folder inside the working directory, below the home folder, apart from every folder written in", name)
        return usable

    def inside(self, relative: object, need: Literal["read", "write"] = "read") -> Path:
        """`relative` inside one of the owner's folders, else refused: an absolute or drive path,
        `..`, a link on the way, a hidden (dot) or credential name, a stream or separator
        character; for `files`, a folder whose level does not give `need`."""
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise PermissionError("a file path is relative to the machine's working directory")
        parts = Path(relative).parts
        if any(part.startswith(".") or self.FORBIDDEN.search(part) for part in parts):
            raise PermissionError(f"{relative}: hidden names, ':' '\\' and trailing dots or spaces are never reachable from a workflow")
        base = self.config.working_directory.resolve()
        target = (base / relative).resolve()
        if self.area == "files":
            level = self.config.place_map().reach(parts)
            if LEVEL_RANK[level] < LEVEL_RANK["write_on_review" if need == "write" else "read"]:
                raise PermissionError(f"{relative} is {level.replace('_', ' ')} on this machine: a workflow {'writes' if need == 'write' else 'reads'} only where its owner "
                                      f"set {'write after review, sandbox or write' if need == 'write' else 'read or later'} (`galaius machine places`)")
            if target != base.joinpath(*parts):
                raise PermissionError(f"{relative}: links are never followed")
            return target
        if not any(target == root or root in target.parents for root in self.roots()):
            raise PermissionError(f"{relative} is outside this machine's script folders ({', '.join(self.config.script_roots) or 'none'}); its owner sets them with `galaius machine script-roots`")
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
                if child.name.startswith(".") or self.FORBIDDEN.search(child.name) or PinnedDirectory.link_like(child.lstat()):
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
        with PinnedDirectory.open(target.parent) as folder:
            descriptor = folder.file(target.name, os.O_RDONLY | PinnedDirectory.NONBLOCK)
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
    """The owner's folders as the Data screen and the agents its owner allowed read them
    (MachineDataRequest): each folder set to `see` or later is a root; beneath ONE named root,
    walked part by part without following a link (`PinnedDirectory`) — a link anywhere on the way,
    a hidden or credential name, anything but a folder or a plain single-link file is refused, even
    when it would land inside another root. `see` lists names, sizes and dates; `read` or later
    also gives the bytes; a deeper folder set to `hidden` is left out. Read-only."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    config: MachineConfig
    MAX_ENTRIES: ClassVar[int] = 500

    def roots(self) -> dict[str, Path]:
        return {path: folder for path, (folder, _) in self.config.place_map().in_force("see").items()}

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

    def _folder(self, root: str, parts: tuple[str, ...]):
        """The folder `parts` beneath `root`, reached without following a link."""
        try:
            top = self.roots()[root]
        except KeyError:
            raise PermissionError(f"{root} is not one of this machine's file roots; its owner sets levels with `galaius machine places`") from None
        return PinnedDirectory.open(top, *parts)

    def _level(self, root: str, parts: tuple[str, ...]) -> PlaceLevel:
        return self.config.place_map().reach((*split(root), *parts))

    @staticmethod
    def _identity(facts: os.stat_result) -> str:
        return f"{facts.st_dev}:{facts.st_ino}:{facts.st_mtime_ns}"

    def answer(self, request: MachineDataRequest) -> MachineDataAnswer:
        if request.op == "list" and not request.root:
            return MachineDataAnswer(request_id=request.id, kind="folder", entries=tuple(MachineFileEntry(name=name, kind="folder") for name in self.roots()))
        parts = self._parts(request.path)
        needed = "see" if request.op == "list" or (request.op == "stat" and not parts) else "read"
        if request.root in self.roots() and LEVEL_RANK[level := self._level(request.root, parts)] < LEVEL_RANK[needed]:
            said = "only names are listed there" if level == "see" else "nothing there is reachable"
            raise PermissionError(f"{'/'.join(filter(None, (request.root, request.path)))} is {level} on this machine: {said}")
        if request.op == "list":
            with self._folder(request.root, parts) as folder:
                entries, truncated = [], False
                for name in sorted(folder.names(), key=str.lower):
                    if name.startswith(".") or MachineFiles.FORBIDDEN.search(name) or self._level(request.root, (*parts, name)) == "hidden":
                        continue
                    facts = folder.stat(name)
                    if folder.link_like(facts):
                        continue
                    if stat.S_ISDIR(facts.st_mode) or (stat.S_ISREG(facts.st_mode) and facts.st_nlink == 1):
                        if len(entries) == self.MAX_ENTRIES:
                            truncated = True
                            break
                        entries.append(MachineFileEntry(name=name, kind="folder" if stat.S_ISDIR(facts.st_mode) else "file", size=None if stat.S_ISDIR(facts.st_mode) else facts.st_size))
            entries.sort(key=lambda entry: (entry.kind != "folder", entry.name.lower()))
            return MachineDataAnswer(request_id=request.id, kind="folder", entries=tuple(entries), truncated=truncated)
        with self._folder(request.root, parts[:-1]) as folder:
            facts = folder.stat(parts[-1]) if parts else folder.stat()
            if stat.S_ISDIR(facts.st_mode) and not folder.link_like(facts):
                if request.op != "stat":
                    raise PermissionError(f"{request.path}: only plain files are read")
                return MachineDataAnswer(request_id=request.id, kind="folder", modified_at=datetime.fromtimestamp(facts.st_mtime, UTC), identity=self._identity(facts))
            descriptor = folder.file(parts[-1], os.O_RDONLY | PinnedDirectory.NONBLOCK)
        try:
            facts = os.fstat(descriptor)
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
    server into `<working directory>/.galaius/transfers/<run>/` and handed to the step as a local
    path; a file op reads or writes one file INSIDE a folder whose level allows it
    (`MachineConfig.places`; a `write_on_review` folder receives the write in a staging copy the
    owner accepts on the machine, `galaius.machine.places.place_reviews`). Every byte crosses the server over HTTP with this machine's own
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
        return urllib.request.Request(self.config.endpoint(path), headers={**self.config.authorization, **options.pop("headers", {})}, **options)

    @property
    def inbox(self) -> Path:
        """Where this command's received files land: the runner's own folder, never a
        workflow-chosen path; removed when the command ends (`discard`)."""
        return self.config.working_directory.resolve() / ".galaius" / "transfers" / str(self.command.run_id) / str(self.command.id)

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
        audit("file-audit.log", {"op": op, "path": str(path), "digest": digest, "size": size, "run_id": str(self.command.run_id),
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
            with urllib.request.urlopen(self._request(self.command.input_file_path(port, index)), timeout=300, context=server_tls()) as response, partial.open("wb") as stream:
                while chunk := response.read(1 << 20):
                    size += len(chunk)
                    if size > ref.size:
                        raise RuntimeError(f"{port}: the server sent more bytes than the file holds")
                    digest.update(chunk)
                    stream.write(chunk)
            if digest.hexdigest() != ref.digest:
                raise RuntimeError(f"{port}: the file arrived damaged (sha256 mismatch)")
            replace_held_open(partial, target)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"{port}: the server refused the file (HTTP {error.code})") from error
        finally:
            partial.unlink(missing_ok=True)
        logger.info("received %s (%d bytes, sha256 %s)", ref.path, size, ref.digest[:12])
        return target

    def _path(self, need: Literal["read", "write"] = "read") -> Path:
        """The file a file op names: a wired `path` input wins over the node's `artifact_path`."""
        return self.inside(self.command.inputs.get("path", self.command.config.get("artifact_path")), need)

    def _put(self, target: Path, content: bytes) -> UUID | None:
        """`content` saved at `target` as its folder's level says: in place (`sandbox`, `write`:
        never an instruction file an agent would obey, outside a sandbox), or held for the owner's
        review (`write_on_review`: the review's id)."""
        places = self.config.place_map()
        parts = target.relative_to(places.base).parts
        level = places.reach(parts)
        if level == "write_on_review":
            place, below = places.place_of(parts)
            return PlaceReviews.default().stage_file(place, places.base.joinpath(*split(place)), below, content, origin="workflow", run_id=self.command.run_id)
        if level not in IN_PLACE_WRITES:
            raise PermissionError(f"{target.name}: this folder is {level} on this machine; a workflow writes only where its owner allowed it")
        if level == "write" and target.name.casefold() in INSTRUCTION_NAMES:
            raise PermissionError(f"{target.name} steers the agents started there: a workflow writes it only in a sandbox, or after review")
        with PinnedDirectory.open(places.base, *parts[:-1], create=True) as folder:
            write_plain(folder, parts[-1], content)
        return None

    def write(self) -> dict[str, object]:
        """Saves the `value` input as a file (a received file copied as is, any other value as its
        text; several received files into `path` as a folder); answers what was saved, and the
        review holding it when its folder is write-after-review. Needs the machine's full-access
        ceiling."""
        if self.config.permission_ceiling != "full_access":
            raise PermissionError("this machine is read-only: it cannot save files")
        target, value = self._path("write"), self.command.inputs.get("value", "")
        base = self.config.working_directory.resolve()
        if "value" in self.fetched and isinstance(value, list):
            names, reviews = [], set()
            for received in value:
                name = Path(received).name
                reviews.add(self._put(self.inside(str((target / name).relative_to(base)), "write"), Path(received).read_bytes()))
                names.append(name)
            content, review = json.dumps(sorted(names)).encode(), next(iter(reviews - {None}), None)
        else:
            content = Path(value).read_bytes() if "value" in self.fetched else (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).encode()
            review = self._put(target, content)
        digest = hashlib.sha256(content).hexdigest()
        relative = target.relative_to(base).as_posix()
        self._audit("staged" if review else "write", target, digest, len(content))
        logger.info("%s %s (%d bytes)", "held for review" if review else "saved", relative, len(content))
        return {"machine": str(self.config.machine_id), "path": relative, "digest": digest, "size": len(content), **({"review": str(review)} if review else {})}

    def read(self) -> dict[str, object]:
        """Uploads one file of a file root, streamed; answers the `ArtifactRef` the server stored."""
        source = self._path()
        # Non-blocking: a named pipe planted in a root must not hang the step (refused just below).
        with PinnedDirectory.open(source.parent) as folder:
            descriptor = folder.file(source.name, os.O_RDONLY | PinnedDirectory.NONBLOCK)
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
                                    headers={"x-galaius-digest": digest.hexdigest(), "Content-Length": str(facts.st_size), "Content-Type": mimetypes.guess_type(source.name)[0] or "application/octet-stream"})
            try:
                with urllib.request.urlopen(request, timeout=300, context=server_tls()) as response:
                    stored = json.loads(response.read())
            except urllib.error.HTTPError as error:
                raise RuntimeError(f"the server refused {source.name} (HTTP {error.code}): {error.read()[:200].decode(errors='replace')}") from error
        self._audit("read", source, digest.hexdigest(), facts.st_size)
        logger.info("sent %s (%d bytes, sha256 %s)", source.name, facts.st_size, digest.hexdigest()[:12])
        return ArtifactRef.model_validate(stored).model_dump(mode="json")
