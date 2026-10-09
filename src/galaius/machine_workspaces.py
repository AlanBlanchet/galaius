"""Project folders the owner puts on one of his PCs from the web, beneath an agent root: a
repository (and its submodules) cloned with the PC's OWN git credentials (its SSH agent / keys, its
credential helper) - the server never sees them and never names a command; a copy of the project's
folder on another of his PCs (`galaius.workspace_copy`); or an empty folder. A failure carries a
`WorkspaceFailureCode` the server words for the owner.

What a clone may do is fixed here, whatever the server asks (threat model run a0c18d82, MUST 5-7):
only repositories the owner's `clone_origins` cover - the repository AND every submodule, nested
ones included, each checked before it is fetched; only https and ssh; no hook runs (an empty hooks folder), no redirect, no password prompt, no LFS
download; SSH only to a host this PC already knows (`StrictHostKeyChecking=yes`, no agent or port
forwarding); a host resolving to a loopback / private / link-local address is refused. The clone
lands in a hidden `.<name>.partial` folder (never reachable as an agent folder) and is renamed
into place only once complete; an existing folder is never touched."""

import contextlib
import errno
import hashlib
import ipaddress
import json
import logging
import re
import os
import shutil
import socket
import stat
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from galaius_core import (
    GitRemote, MachineWorkspaceJob, WorkspaceArchive, WorkspaceCreateRequest, WorkspaceFailure, WorkspacePack, WorkspacePackRequest,
    WorkspacePrepareRequest, WorkspaceRefresh, WorkspaceSpec, WorkspaceUpload,
)
from galaius.file_lock import exclusive
from galaius.paths import UserPaths
from galaius.pinned_directory import PinnedDirectory
from galaius.places import DEPENDENCY_FOLDERS
from galaius.private_files import PRIVATE_FILES
from galaius.workspace_copy import PackedFile, WorkspaceArchiveReader, WorkspaceFiles, WorkspaceTransfers
from galaius.workspace_git import Git, WorkspaceRefused

logger = logging.getLogger(__name__)


class KeptJob(MachineWorkspaceJob):
    """A job as this PC keeps it: also, for a web clone, the sha256 of its `.git/config` once made
    (`MachineWorkspaces.refresh` runs git there only while it is unchanged). Never sent: an answer
    carries `MachineWorkspaceJob` fields only."""

    git_config: str | None = None


class WorkspaceJobs(BaseModel):
    """The project folders asked of this PC, newest first, kept beside its machine file."""

    model_config = ConfigDict(frozen=True)
    path: Path
    keep: int = 50

    def read(self) -> tuple[KeptJob, ...]:
        try:
            return tuple(KeptJob.model_validate(item) for item in json.loads(PRIVATE_FILES.read_text(self.path)))
        except FileNotFoundError:
            return ()

    def put(self, job: MachineWorkspaceJob) -> None:
        """Records `job`; every ready workspace is kept (it marks a folder whose own agent settings
        never load: `MachineWorkspaces.prepared`), the rest trimmed to the newest `keep`."""
        with exclusive(os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)):
            jobs = (job, *(item for item in self.read() if item.id != job.id))
            jobs = tuple(item for index, item in enumerate(jobs) if item.state == "ready" or index < self.keep)
            PRIVATE_FILES.write_text(self.path, json.dumps([item.model_dump(mode="json") for item in jobs], separators=(",", ":")) + "\n")

    def settle_interrupted(self) -> None:
        """A clone or copy still `running` from an earlier runner process ended with it."""
        for job in self.read():
            if job.state == "running":
                self.put(job.model_copy(update={"state": "failed", "code": "interrupted", "detail": "interrupted when galaius restarted on this PC; prepare it again", "finished_at": datetime.now(UTC)}))


class MachineWorkspaces(BaseModel):
    """Puts project folders beneath this PC's agent roots (`roots`: name -> folder) and lists them;
    sends one of its folders to another PC (`pack`)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    roots: dict[str, Path]
    origins: tuple[str, ...]
    jobs: WorkspaceJobs
    environment: dict[str, str]
    #: The PC's working directory: where the agent roots are walked from (never through a link), and
    #: where an existing checkout is looked for besides them.
    working_directory: Path
    #: A copy's archive crossing the server under this machine's token.
    transfers: WorkspaceTransfers
    #: Makes a folder (relative to the working directory) an agent root on this PC; whether it is usable.
    register_root: Callable[[str], bool] | None = None
    #: How deep below each place an existing checkout is looked for, and how many folders at most.
    SCAN_DEPTH: ClassVar[int] = 3
    SCAN_LIMIT: ClassVar[int] = 20000
    timeout: float = 30 * 60
    #: Longest a refresh before a launch may take (`refresh`): past it the launch goes on as the folder is.
    REFRESH_SECONDS: ClassVar[float] = 20
    #: Free space a clone needs before it starts.
    disk_floor: int = 5 * 1024**3
    #: What git and ssh may do here, whatever the repository or the server says.
    GIT_OPTIONS: ClassVar[tuple[str, ...]] = (
        "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
        "-c", "http.followRedirects=false", "-c", "credential.interactive=false", "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false",
    )
    #: How deep submodules may nest.
    SUBMODULE_DEPTH: ClassVar[int] = 5
    SSH_COMMAND: ClassVar[str] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ForwardAgent=no -o ClearAllForwardings=yes -o PermitLocalCommand=no"
    #: Only one clone or received copy runs at a time on a PC, and one copy is sent at a time.
    _running: ClassVar[threading.Lock] = threading.Lock()
    _sending: ClassVar[threading.Lock] = threading.Lock()

    def prepare(self, request: WorkspacePrepareRequest) -> MachineWorkspaceJob:
        """An existing checkout of the repository on this PC, registered as an agent folder (ready at
        once); else the clone, started (answered at once, `running`); or a refusal saying why."""
        remote = request.url
        if not remote.allowed_by(self.origins):
            raise PermissionError(f"{remote.origin} is not among the repositories this PC may use; allow it on this PC's page first")
        if (found := self.found(request.url)) is not None:
            return found
        if not Git.installed(self.environment):
            raise WorkspaceRefused("git_missing", "git is not installed on this PC")
        self._public_host(remote.host)
        self._room(request.root)
        return self._started(KeptJob(id=uuid4(), root=request.root, name=request.name or remote.name, origin=remote.origin, state="running", started_at=datetime.now(UTC)),
                             self._clone, remote, request.submodules)

    def create(self, request: WorkspaceCreateRequest) -> MachineWorkspaceJob:
        """The project folder `request.name` (or the first free one of its `candidates`): empty
        (ready at once), or the copy another PC sent, started (answered at once, `running`, `done`
        of `total` bytes as it arrives)."""
        if request.copy_of is None:
            self._room(request.root, space=False)
            name = self._placed(request.root, request.name, lambda root, name: root.mkdir(name, 0o755))
            job = KeptJob(id=uuid4(), root=request.root, name=name, source="empty", state="ready", started_at=datetime.now(UTC), finished_at=datetime.now(UTC))
            self.jobs.put(job)
            return job
        self._room(request.root)
        return self._started(KeptJob(id=uuid4(), root=request.root, name=request.name, source="copy", state="running", started_at=datetime.now(UTC),
                                     done=0, total=request.copy_of.size), self._receive, request.copy_of)

    def pack(self, request: WorkspacePackRequest, folder: Path) -> WorkspacePack:
        """What copying `folder` sends (past the copy limits, the files that fit); with
        `request.transfer` also the upload of it, started in the background."""
        measure, files = WorkspaceFiles(folder=folder, environment=self.environment).pack(self.remote_of(folder))
        if request.transfer is None:
            return measure
        if not self._sending.acquire(blocking=False):
            raise PermissionError("another folder is being sent from this computer; wait for it to finish")
        try:
            threading.Thread(target=self._send, args=(request.transfer, folder, files), name=f"workspace-send-{request.transfer}", daemon=True).start()
        except BaseException:
            self._sending.release()
            raise
        return measure

    def _room(self, root: str, space: bool = True) -> None:
        """Refused when `root` is not an agent root here, or (`space`) its disk is nearly full."""
        base = self.roots.get(root)
        if base is None:
            raise PermissionError(f"{root!r} is not an agent folder on this computer")
        if space and shutil.disk_usage(base).free < self.disk_floor:
            raise WorkspaceRefused("disk_full", f"less than {self.disk_floor // 1024**3} GB free under {root} on this computer")

    def _placed(self, root: str, name: str, place: Callable[[PinnedDirectory, str], None]) -> str:
        """`place(root folder, candidate)` with the first of `WorkspaceSpec.candidates(name)` it can
        take, the root walked from the working directory without a link or junction; the name it
        took. `place` raises FileExistsError when the candidate is taken (`PinnedDirectory.mkdir`,
        `PinnedDirectory.move`): the next one is tried, nothing is ever replaced."""
        with PinnedDirectory.open(self.working_directory.resolve(), *root.split("/")) as folder:
            for candidate in WorkspaceSpec.candidates(name):
                try:
                    place(folder, candidate)
                    return candidate
                except FileExistsError:
                    continue
        raise WorkspaceRefused("exists", f"{root}/{name} and its -2 ... -{WorkspaceSpec.TRIES} variants all exist on this computer")

    def _started(self, job: KeptJob, work: Callable[..., KeptJob], *arguments: object) -> MachineWorkspaceJob:
        """`job` recorded and `work(job, *arguments)` running on its own thread (one at a time), whose
        outcome is recorded when it ends, whatever stopped it."""
        if not self._running.acquire(blocking=False):
            raise PermissionError("another project folder is being prepared on this computer; wait for it to finish")
        def run() -> None:
            try:
                finished = work(job, *arguments)
            except WorkspaceRefused as failure:
                finished = job.model_copy(update={"state": "failed", "code": failure.code, "detail": str(failure)[:600]})
            except (subprocess.TimeoutExpired, TimeoutError):
                finished = job.model_copy(update={"state": "failed", "code": "timeout", "detail": f"it took longer than {int(self.timeout // 60)} min and was stopped"})
            except Exception as error:  # the thread's boundary: whatever stopped it, its job says so
                logger.exception("project folder %s/%s failed", job.root, job.name)
                code = "disk_full" if getattr(error, "errno", None) == errno.ENOSPC else "failed"
                finished = job.model_copy(update={"state": "failed", "code": code, "detail": str(error)[:600] or type(error).__name__})
            finally:
                self._running.release()
            self.jobs.put(finished.model_copy(update={"finished_at": datetime.now(UTC)}))
        try:
            self.jobs.put(job)
            threading.Thread(target=run, name=f"workspace-{job.name}", daemon=True).start()
        except BaseException:
            self._running.release()
            raise
        return job

    def found(self, remote: GitRemote) -> MachineWorkspaceJob | None:
        """An existing checkout of `remote` on this PC (its `origin` remote), looked for shallow-first
        in the agent roots, then the working directory: inside an agent root it is used as is,
        elsewhere its folder becomes an agent root. Its own agent settings load (its owner's code)."""
        checkout = self._scan(remote.origin.lower())
        if checkout is None:
            return None
        base = self.working_directory.resolve() if self.working_directory is not None else None
        inside = next(((name, root) for name, root in self.roots.items() if checkout == root or root in checkout.parents), None)
        if inside is not None:
            root, path = inside[0], checkout.relative_to(inside[1]).as_posix() if checkout != inside[1] else ""
        elif base is not None and base in checkout.parents and self.register_root is not None and self.register_root(checkout.relative_to(base).as_posix()):
            root, path = checkout.relative_to(base).as_posix(), ""
        else:
            return None
        job = MachineWorkspaceJob(id=uuid4(), root=root, name=path, origin=remote.origin, state="ready", found=True, started_at=datetime.now(UTC), finished_at=datetime.now(UTC),
                                  detail=f"existing checkout found at {root}{'/' + path if path else ''}")
        self.jobs.put(job)
        return job

    _ORIGIN: ClassVar[re.Pattern[str]] = re.compile(r'\[remote "origin"\][^\[]*?^\s*url\s*=\s*(\S+)', re.MULTILINE)

    @staticmethod
    def git_dir(checkout: Path) -> Path | None:
        """The repository folder git keeps for `checkout`, shared by its worktrees: `.git` itself, or
        for a worktree / submodule (`.git` a file naming it) the common folder it points to; None when
        `checkout` is not one. Read as files: no git runs inside a repository it does not know."""
        marker = checkout / ".git"
        try:
            if marker.is_dir() and not marker.is_symlink():
                return marker
            if not marker.is_file():
                return None
            line = marker.read_text(errors="replace").strip()
            if not line.startswith("gitdir:"):
                return None
            own = (checkout / line.removeprefix("gitdir:").strip()).resolve()
            common = own / "commondir"
            return (own / common.read_text(errors="replace").strip()).resolve() if common.is_file() else own
        except OSError:
            return None

    @classmethod
    def remote_of(cls, checkout: Path) -> GitRemote | None:
        """The repository `checkout` comes from (its `origin` remote, credentials dropped), read as
        a file: no git runs inside a repository it does not know."""
        git = cls.git_dir(checkout)
        try:
            match = cls._ORIGIN.search((git / "config").read_text(errors="replace")) if git is not None else None
        except OSError:
            return None
        return GitRemote.stripped(match[1]) if match is not None else None

    @classmethod
    def origin_of(cls, checkout: Path) -> str | None:
        """`remote_of` as `host/owner/repo`, lowercased."""
        remote = cls.remote_of(checkout)
        return remote.origin.lower() if remote is not None else None

    def _scan(self, origin: str) -> Path | None:
        """The first folder, shallow-first, whose `.git/config` names `origin` as its origin remote
        (read as a file: no git runs inside a repository it does not know)."""
        places = [*self.roots.values(), *([self.working_directory] if self.working_directory is not None else [])]
        queue, seen = deque((place.resolve(), 0) for place in places if place.is_dir()), set()
        while queue and len(seen) < self.SCAN_LIMIT:
            folder, depth = queue.popleft()
            if folder in seen:
                continue
            seen.add(folder)
            if (folder / ".git").exists():
                if self.origin_of(folder) == origin:
                    return folder
                continue
            if depth >= self.SCAN_DEPTH:
                continue
            try:
                children = sorted(entry for entry in folder.iterdir() if not entry.name.startswith(".") and entry.name not in DEPENDENCY_FOLDERS and entry.is_dir() and not entry.is_symlink())
            except OSError:
                continue
            queue.extend((child, depth + 1) for child in children)
        return None

    def _allowed(self, remote: GitRemote) -> None:
        """Refuses a repository outside the owner's origins, or on a host that is not public."""
        if not remote.allowed_by(self.origins):
            raise PermissionError(f"{remote.origin} is not among the repositories this PC may clone; allow it on this PC's page first")
        self._public_host(remote.host)

    @classmethod
    def busy(cls) -> bool:
        """A project folder is being put in place or sent now (this process must not leave for a new runtime mid-way)."""
        return cls._running.locked() or cls._sending.locked()

    def prepared(self, folder: Path) -> bool:
        """Whether `folder` lies in a project folder cloned or copied here from the web (`job_at`):
        code this PC's owner did not write here, so its own agent settings (hooks, permissions) are
        never loaded there. An empty folder made from the web holds only what is written in it here."""
        return self.job_at(folder) is not None

    @staticmethod
    def _public_host(host: str) -> None:
        try:
            answers = {item[4][0] for item in socket.getaddrinfo(host, None)}
        except OSError:
            raise WorkspaceRefused("host_unreachable", f"this PC cannot reach {host} (its name does not resolve)") from None
        for answer in answers:
            address = ipaddress.ip_address(answer.split("%")[0])
            address = address.ipv4_mapped or address if isinstance(address, ipaddress.IPv6Address) else address
            if not address.is_global:
                raise WorkspaceRefused("host_unreachable", f"{host} resolves to a local or private address ({address}); only public hosts are cloned from the web")

    def _partial(self, job: MachineWorkspaceJob) -> str:
        """The hidden folder a job fills before it is renamed into place (never an agent folder)."""
        return f".{job.id}.partial"

    def _discard(self, job: MachineWorkspaceJob) -> None:
        with contextlib.suppress(OSError), PinnedDirectory.open(self.working_directory.resolve(), *job.root.split("/")) as root:
            if self._partial(job) in root.names():
                root.rmtree(self._partial(job))

    def _clone(self, job: KeptJob, remote: GitRemote, submodules: bool) -> KeptJob:
        partial = self.roots[job.root] / self._partial(job)
        try:
            git = self._git(remote.host, self.timeout)
            git.run("clone", "--", remote.url, str(partial))
            if submodules:
                self._submodules(git, partial, partial)
            config = hashlib.sha256((partial / ".git" / "config").read_bytes()).hexdigest()
            name = self._placed(job.root, job.name, lambda root, name: root.move(partial.name, root, name))
        except BaseException:
            self._discard(job)
            raise
        return job.model_copy(update={"name": name, "state": "ready", "detail": "", "git_config": config})

    def _git(self, host: str, seconds: float, **environment: str) -> Git:
        """git as a clone or a refresh from the web runs it: no hook, no prompt, no LFS download, the
        fixed SSH options, plus `environment`."""
        hooks = UserPaths.data() / "empty-git-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        return Git(options=(*self.GIT_OPTIONS, "-c", f"core.hooksPath={hooks}"), host=host, deadline=time.monotonic() + seconds,
                   environment={**self.environment, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_SSH_COMMAND": self.SSH_COMMAND,
                                **environment})

    def job_at(self, folder: Path) -> tuple[KeptJob, Path] | None:
        """The ready project folder put here from the web (a clone or a copy, never one found or
        created empty) that `folder` lies in, and its top folder; None for any other folder."""
        resolved = folder.resolve()
        for job in self.jobs.read():
            if job.state == "ready" and job.source in {"clone", "copy"} and not job.found and job.root in self.roots:
                place = (self.roots[job.root] / job.name).resolve()
                if resolved == place or place in resolved.parents:
                    return job, place
        return None

    def refresh(self, folder: Path) -> WorkspaceRefresh | None:
        """Before a launch in `folder`: a clone made here from the web, fast-forwarded to its remote
        when nothing tracked in it is uncommitted (`WorkspaceRefresh`); None for any other folder.
        git runs there only while its `.git` is a plain folder with no `commondir` and the `config`
        the clone wrote (an agent that changed either could make git run a command of its choosing
        outside any fence), with `GIT_DIR` / `GIT_COMMON_DIR` pinned to it, only toward a remote the
        owner allows on a public host; submodules stay as they are."""
        found = self.job_at(folder)
        if found is None or found[0].source != "clone":
            return None
        job, checkout = found
        repository = checkout / ".git"
        try:
            if (job.git_config is None or not stat.S_ISDIR(repository.lstat().st_mode) or (repository / "commondir").exists() or (repository / "config").is_symlink()
                    or hashlib.sha256((repository / "config").read_bytes()).hexdigest() != job.git_config):
                return "dirty"
            remote = self.remote_of(checkout)
            if remote is None or not remote.allowed_by(self.origins):
                return "unreachable"
            self._public_host(remote.host)
            # git reads this repository only: no `commondir`, no `gitdir:` redirect to another config.
            git = self._git(remote.host, self.REFRESH_SECONDS, GIT_DIR=str(repository), GIT_COMMON_DIR=str(repository), GIT_WORK_TREE=str(checkout))
            if git.run("-C", str(checkout), "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all").strip():
                return "dirty"
            git.run("-C", str(checkout), "fetch", "--quiet", "--no-recurse-submodules", "origin")
        except (PermissionError, OSError, subprocess.TimeoutExpired, TimeoutError):
            return "unreachable"
        try:
            before = git.run("-C", str(checkout), "rev-parse", "HEAD").strip()
            git.run("-C", str(checkout), "merge", "--ff-only", "--quiet", "@{upstream}")
            return "current" if git.run("-C", str(checkout), "rev-parse", "HEAD").strip() == before else "updated"
        except (PermissionError, OSError, subprocess.TimeoutExpired, TimeoutError):
            return "diverged"

    def _receive(self, job: KeptJob, archive: WorkspaceArchive) -> KeptJob:
        """The copy downloaded (`done` recorded part by part), its files written into the job's
        hidden partial folder through `PinnedDirectory`, then moved to the first free name: never
        over anything there, nothing left behind on a failure."""
        root_parts, partial = tuple(job.root.split("/")), self._partial(job)
        received = UserPaths.data() / "transfers" / f"{archive.transfer}.tar.gz"
        try:
            received.parent.mkdir(parents=True, exist_ok=True)
            self.transfers.download(archive, received, lambda done: self.jobs.put(job.model_copy(update={"done": done})))
            with PinnedDirectory.open(self.working_directory.resolve(), *root_parts) as root:
                root.mkdir(partial, 0o755)
            WorkspaceArchiveReader(archive=archive, base=self.working_directory.resolve(), parts=(*root_parts, partial)).extract(received)
            name = self._placed(job.root, job.name, lambda root, name: root.move(partial, root, name))
        except BaseException:
            self._discard(job)
            raise
        finally:
            received.unlink(missing_ok=True)
        return job.model_copy(update={"name": name, "state": "ready", "done": archive.size, "detail": ""})

    def _send(self, transfer: UUID, folder: Path, files: list[PackedFile]) -> None:
        """The upload of `files`, packed beside the runner's own data; its outcome posted to the
        server either way (a failure as its code), the packed archive removed."""
        packed = UserPaths.data() / "transfers" / f"{transfer}.send.tar.gz"
        try:
            packed.parent.mkdir(parents=True, exist_ok=True)
            WorkspaceFiles(folder=folder, environment=self.environment).write(files, packed)
            self.transfers.upload(transfer, packed, len(files))
        except Exception as error:  # the thread's boundary: whatever stopped it, the server hears it
            code = error.code if isinstance(error, WorkspaceRefused) else "disk_full" if getattr(error, "errno", None) == errno.ENOSPC else "failed"
            logger.exception("sending folder for transfer %s failed", transfer)
            try:
                self.transfers.ended(transfer, WorkspaceUpload(outcome=WorkspaceFailure(code=code, detail=str(error)[:600] or type(error).__name__)))
            except WorkspaceRefused as unreachable:
                logger.warning("the server never heard transfer %s failed: %s", transfer, unreachable)
        finally:
            packed.unlink(missing_ok=True)
            self._sending.release()

    def _submodules(self, git: "Git", folder: Path, clone: Path, depth: int = 0) -> None:
        """Fetches `folder`'s submodules, nested ones too, each only once its address (as git
        resolved it, relative ones included) is checked like the repository's own. The walk follows
        only the submodules git itself records in `folder` (its gitlinks), never `.gitmodules`
        paths, and never leaves `clone`."""
        if depth >= self.SUBMODULE_DEPTH:
            raise RuntimeError(f"submodules nest deeper than {self.SUBMODULE_DEPTH} levels; refused")
        linked = [entry.split("\t", 1)[1] for entry in git.run("-C", str(folder), "ls-files", "-z", "--stage").split("\0") if entry.startswith("160000 ")]
        if not linked:
            return
        git.run("-C", str(folder), "submodule", "init")
        for entry in git.run("-C", str(folder), "config", "--local", "-z", "--get-regexp", r"^submodule\..*\.url$", allow=(1,)).split("\0"):
            key, _, url = entry.partition("\n")
            if not key:
                continue
            try:
                remote = GitRemote(url=url)
            except ValueError:
                raise RuntimeError(f"submodule {key.removeprefix('submodule.').removesuffix('.url')} has an address this PC never clones from the web") from None
            try:
                self._allowed(remote)
            except PermissionError as refusal:
                raise RuntimeError(f"submodule {remote.origin}: {refusal}") from None
        git.run("-C", str(folder), "submodule", "update")
        for path in linked:
            nested = folder / path
            if nested.is_symlink() or not nested.resolve().is_relative_to(clone.resolve()):
                raise RuntimeError(f"submodule path {path!r} leaves the workspace; refused")
            self._submodules(git, nested, clone, depth + 1)
