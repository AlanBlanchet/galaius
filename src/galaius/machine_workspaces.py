"""Workspaces the owner prepares on one of his PCs from the web: a repository (and its submodules)
cloned into a new folder beneath an agent root, with the PC's OWN git credentials (its SSH agent /
keys, its credential helper) - the server never sees them and never names a command.

What a clone may do is fixed here, whatever the server asks (threat model run a0c18d82, MUST 5-7):
only repositories the owner's `clone_origins` cover - the repository AND every submodule, nested
ones included, each checked before it is fetched; only https and ssh; no hook runs (an empty hooks folder), no redirect, no password prompt, no LFS
download; SSH only to a host this PC already knows (`StrictHostKeyChecking=yes`, no agent or port
forwarding); a host resolving to a loopback / private / link-local address is refused. The clone
lands in a hidden `.<name>.partial` folder (never reachable as an agent folder) and is renamed
into place only once complete; an existing folder is never touched."""

import ipaddress
import re
import json
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
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from galaius_core import GitRemote, MachineWorkspaceJob, WorkspacePrepareRequest
from galaius.file_lock import exclusive
from galaius.paths import UserPaths
from galaius.private_files import PRIVATE_FILES


class CloneFailure(BaseModel):
    """One way `git clone` fails, told in plain words: `marks` are pieces of git's / ssh's error
    output, `said` the sentence the owner reads ({host} filled in)."""

    model_config = ConfigDict(frozen=True)
    marks: tuple[str, ...]
    said: str

    #: First match wins: the most specific cause first.
    KNOWN: ClassVar[tuple["CloneFailure", ...]]

    @classmethod
    def explain(cls, output: str, host: str) -> str:
        lowered = output.lower()
        found = next((failure for failure in cls.KNOWN if any(mark in lowered for mark in failure.marks)), None)
        if found is not None:
            return found.said.format(host=host)
        last = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "no output")
        return f"git clone failed: {last}"[:600]


CloneFailure.KNOWN = (
    CloneFailure(marks=("host key verification failed", "host key is known", "no matching host key", "remote host identification has changed"),
                 said="this PC has never connected to {host} over SSH, so it does not know its host key: use the https address, or connect to {host} once from this PC"),
    CloneFailure(marks=("permission denied (publickey",), said="{host} refused this PC's SSH key: add this PC's public key to your account there, or use the https address"),
    CloneFailure(marks=("could not read username", "terminal prompts disabled", "authentication failed", "invalid username or password", "could not read password"),
                 said="this PC has no saved git sign-in for {host}: sign in to {host} once with git on this PC, or use an address this PC can already read"),
    CloneFailure(marks=("repository not found", "does not appear to be a git repository", "not found"),
                 said="{host} says this repository does not exist, or this PC's git account cannot see it"),
    CloneFailure(marks=("could not resolve host", "name or service not known", "temporary failure in name resolution"), said="this PC cannot reach {host} (its name does not resolve)"),
    CloneFailure(marks=("transport", "not allowed"), said="a submodule uses a transport other than https or ssh, which is refused"),
    CloneFailure(marks=("no space left on device",), said="this PC ran out of disk space during the clone"),
)


class Git(BaseModel):
    """git as a clone from the web runs it: fixed options and environment, one shared deadline, a
    failure told in plain words (`CloneFailure`)."""

    model_config = ConfigDict(frozen=True)
    options: tuple[str, ...]
    environment: dict[str, str]
    host: str
    deadline: float

    def run(self, *arguments: str, allow: tuple[int, ...] = ()) -> str:
        """git's output; a failure (an exit code outside 0 and `allow`) raises RuntimeError."""
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("clone deadline passed")
        done = subprocess.run(["git", *self.options, *arguments], env=self.environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=left, check=False)
        if done.returncode != 0 and done.returncode not in allow:
            raise RuntimeError(CloneFailure.explain(done.stderr + "\n" + done.stdout, self.host))
        return done.stdout


class WorkspaceJobs(BaseModel):
    """The clones asked of this PC, newest first, kept beside its machine file."""

    model_config = ConfigDict(frozen=True)
    path: Path
    keep: int = 50

    def read(self) -> tuple[MachineWorkspaceJob, ...]:
        try:
            return tuple(MachineWorkspaceJob.model_validate(item) for item in json.loads(PRIVATE_FILES.read_text(self.path)))
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
        """A clone still `running` from an earlier runner process ended with it."""
        for job in self.read():
            if job.state == "running":
                self.put(job.model_copy(update={"state": "failed", "detail": "interrupted when galaius restarted on this PC; prepare it again", "finished_at": datetime.now(UTC)}))


class MachineWorkspaces(BaseModel):
    """Prepares and lists workspaces beneath this PC's agent roots (`roots`: name -> folder)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    roots: dict[str, Path]
    origins: tuple[str, ...]
    jobs: WorkspaceJobs
    environment: dict[str, str]
    #: Where an existing checkout is looked for besides the agent roots (the PC's working directory).
    working_directory: Path | None = None
    #: Makes a folder (relative to the working directory) an agent root on this PC; whether it is usable.
    register_root: Callable[[str], bool] | None = None
    #: How deep below each place an existing checkout is looked for, and how many folders at most.
    SCAN_DEPTH: ClassVar[int] = 3
    SCAN_LIMIT: ClassVar[int] = 20000
    SCAN_SKIP: ClassVar[frozenset[str]] = frozenset({"node_modules", "__pycache__", "venv", "site-packages", "target", "dist", "build"})
    timeout: float = 30 * 60
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
    #: Only one clone runs at a time on a PC.
    _running: ClassVar[threading.Lock] = threading.Lock()

    def prepare(self, request: WorkspacePrepareRequest) -> MachineWorkspaceJob:
        """An existing checkout of the repository on this PC, registered as an agent folder (ready at
        once); else the clone, started (answered at once, `running`); or a refusal saying why."""
        remote = request.url
        if not remote.allowed_by(self.origins):
            raise PermissionError(f"{remote.origin} is not among the repositories this PC may use; allow it on this PC's page first")
        if (found := self.found(request.url)) is not None:
            return found
        self._public_host(remote.host)
        base = self.roots.get(request.root)
        if base is None:
            raise PermissionError(f"{request.root!r} is not an agent folder on this computer")
        name = request.name or remote.name
        target = base / name
        if target.exists() or target.is_symlink():
            raise PermissionError(f"{request.root}/{name} already exists on this computer")
        if shutil.disk_usage(base).free < self.disk_floor:
            raise PermissionError(f"less than {self.disk_floor // 1024**3} GB free under {request.root} on this computer")
        if not self._running.acquire(blocking=False):
            raise PermissionError("another workspace is being prepared on this computer; wait for it to finish")
        try:
            job = MachineWorkspaceJob(id=uuid4(), root=request.root, name=name, origin=remote.origin, state="running", started_at=datetime.now(UTC))
            self.jobs.put(job)
            threading.Thread(target=self._clone, args=(job, remote, request.submodules, target), name=f"workspace-{name}", daemon=True).start()
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
    def origin_of(cls, checkout: Path) -> str | None:
        """The repository `checkout` comes from (its `origin` remote, `host/owner/repo` lowercased)."""
        git = cls.git_dir(checkout)
        try:
            match = cls._ORIGIN.search((git / "config").read_text(errors="replace")) if git is not None else None
            return GitRemote(url=match[1]).origin.lower() if match is not None else None
        except (OSError, ValueError):
            return None

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
                children = sorted(entry for entry in folder.iterdir() if not entry.name.startswith(".") and entry.name not in self.SCAN_SKIP and entry.is_dir() and not entry.is_symlink())
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
        """A clone runs now (this process must not leave for a new runtime mid-clone)."""
        return cls._running.locked()

    def prepared(self, folder: Path) -> bool:
        """Whether `folder` lies in a workspace cloned here from the web: code its owner did not
        write, so its own agent settings (hooks, permissions) are never loaded there."""
        resolved = folder.resolve()
        return any(job.state == "ready" and not job.found and job.root in self.roots and (resolved == (place := (self.roots[job.root] / job.name).resolve()) or place in resolved.parents)
                   for job in self.jobs.read())

    @staticmethod
    def _public_host(host: str) -> None:
        try:
            answers = {item[4][0] for item in socket.getaddrinfo(host, None)}
        except OSError:
            raise PermissionError(f"this PC cannot reach {host} (its name does not resolve)") from None
        for answer in answers:
            address = ipaddress.ip_address(answer.split("%")[0])
            address = address.ipv4_mapped or address if isinstance(address, ipaddress.IPv6Address) else address
            if not address.is_global:
                raise PermissionError(f"{host} resolves to a local or private address ({address}); only public hosts are cloned from the web")

    def _clone(self, job: MachineWorkspaceJob, remote: GitRemote, submodules: bool, target: Path) -> None:
        partial = target.with_name(f".{target.name}.partial")
        try:
            self._remove(partial)
            hooks = UserPaths.data() / "empty-git-hooks"
            hooks.mkdir(parents=True, exist_ok=True)
            git = Git(options=(*self.GIT_OPTIONS, "-c", f"core.hooksPath={hooks}"), host=remote.host, deadline=time.monotonic() + self.timeout,
                      environment={**self.environment, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_SSH_COMMAND": self.SSH_COMMAND})
            git.run("clone", "--", remote.url, str(partial))
            if submodules:
                self._submodules(git, partial, partial)
            partial.rename(target)
            finished = job.model_copy(update={"state": "ready", "detail": "", "finished_at": datetime.now(UTC)})
        except FileNotFoundError:
            finished = job.model_copy(update={"state": "failed", "detail": "git is not installed on this PC", "finished_at": datetime.now(UTC)})
        except (subprocess.TimeoutExpired, TimeoutError):
            finished = job.model_copy(update={"state": "failed", "detail": f"the clone took longer than {int(self.timeout // 60)} min and was stopped", "finished_at": datetime.now(UTC)})
        except (RuntimeError, OSError) as error:
            finished = job.model_copy(update={"state": "failed", "detail": str(error)[:600], "finished_at": datetime.now(UTC)})
        finally:
            self._running.release()
        if finished.state == "failed":
            self._remove(partial)
        self.jobs.put(finished)

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

    @staticmethod
    def _remove(folder: Path) -> None:
        """Deletes a partial clone (git marks its objects read-only: Windows refuses those)."""
        def writable(function, path, _error) -> None:
            os.chmod(path, stat.S_IWRITE)
            function(path)
        if folder.exists():
            shutil.rmtree(folder, onerror=writable)
