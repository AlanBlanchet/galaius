"""Workspaces the owner prepares on one of his PCs from the web: a repository (and its submodules)
cloned into a new folder beneath an agent root, with the PC's OWN git credentials (its SSH agent /
keys, its credential helper) - the server never sees them and never names a command.

What a clone may do is fixed here, whatever the server asks (threat model run a0c18d82, MUST 5-7):
only repositories the owner's `clone_origins` cover; only https and ssh, for the repository and
every submodule; no hook runs (an empty hooks folder), no redirect, no password prompt, no LFS
download; SSH only to a host this PC already knows (`StrictHostKeyChecking=yes`, no agent or port
forwarding); a host resolving to a loopback / private / link-local address is refused. The clone
lands in a hidden `.<name>.partial` folder (never reachable as an agent folder) and is renamed
into place only once complete; an existing folder is never touched."""

import ipaddress
import json
import os
import shutil
import socket
import stat
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from interact_core import GitRemote, MachineWorkspaceJob, WorkspacePrepareRequest
from interact.file_lock import exclusive
from interact.paths import UserPaths
from interact.private_files import PRIVATE_FILES


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
        with exclusive(os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)):
            jobs = (job, *(item for item in self.read() if item.id != job.id))[: self.keep]
            PRIVATE_FILES.write_text(self.path, json.dumps([item.model_dump(mode="json") for item in jobs], separators=(",", ":")) + "\n")

    def settle_interrupted(self) -> None:
        """A clone still `running` from an earlier runner process ended with it."""
        for job in self.read():
            if job.state == "running":
                self.put(job.model_copy(update={"state": "failed", "detail": "interrupted when interact restarted on this PC; prepare it again", "finished_at": datetime.now(UTC)}))


class MachineWorkspaces(BaseModel):
    """Prepares and lists workspaces beneath this PC's agent roots (`roots`: name -> folder)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    roots: dict[str, Path]
    origins: tuple[str, ...]
    jobs: WorkspaceJobs
    environment: dict[str, str]
    timeout: float = 30 * 60
    #: Free space a clone needs before it starts.
    disk_floor: int = 5 * 1024**3
    #: What git and ssh may do here, whatever the repository or the server says.
    GIT_OPTIONS: ClassVar[tuple[str, ...]] = (
        "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
        "-c", "http.followRedirects=false", "-c", "credential.interactive=false", "-c", "core.fsmonitor=false",
    )
    SSH_COMMAND: ClassVar[str] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ForwardAgent=no -o ClearAllForwardings=yes -o PermitLocalCommand=no"
    #: Only one clone runs at a time on a PC.
    _running: ClassVar[threading.Lock] = threading.Lock()

    def prepare(self, request: WorkspacePrepareRequest) -> MachineWorkspaceJob:
        """Starts the clone (answered at once, `running`) or refuses it with why."""
        remote = GitRemote(url=request.url)
        if not remote.allowed_by(self.origins):
            raise PermissionError(f"{remote.origin} is not among the repositories this PC may clone; allow it on this PC's page first")
        base = self.roots.get(request.root)
        if base is None:
            raise PermissionError(f"{request.root!r} is not an agent folder on this computer")
        name = request.name or remote.name
        target = base / name
        if target.exists() or target.is_symlink():
            raise PermissionError(f"{request.root}/{name} already exists on this computer")
        if shutil.disk_usage(base).free < self.disk_floor:
            raise PermissionError(f"less than {self.disk_floor // 1024**3} GB free under {request.root} on this computer")
        self._public_host(remote.host)
        if not self._running.acquire(blocking=False):
            raise PermissionError("another workspace is being prepared on this computer; wait for it to finish")
        job = MachineWorkspaceJob(id=uuid4(), root=request.root, name=name, origin=remote.origin, state="running", started_at=datetime.now(UTC))
        self.jobs.put(job)
        threading.Thread(target=self._clone, args=(job, remote, request.submodules, target), name=f"workspace-{name}", daemon=True).start()
        return job

    @classmethod
    def busy(cls) -> bool:
        """A clone runs now (this process must not leave for a new runtime mid-clone)."""
        return cls._running.locked()

    def prepared(self, folder: Path) -> bool:
        """Whether `folder` lies in a workspace cloned here from the web: code its owner did not
        write, so its own agent settings (hooks, permissions) are never loaded there."""
        resolved = folder.resolve()
        return any(job.state == "ready" and job.root in self.roots and (resolved == (place := (self.roots[job.root] / job.name).resolve()) or place in resolved.parents)
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
            environment = {**self.environment, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_SSH_COMMAND": self.SSH_COMMAND}
            argv = ["git", *self.GIT_OPTIONS, "-c", f"core.hooksPath={hooks}", "clone", *(["--recurse-submodules"] if submodules else []), "--", remote.url, str(partial)]
            done = subprocess.run(argv, env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=self.timeout, check=False)
            if done.returncode != 0:
                raise RuntimeError(CloneFailure.explain(done.stderr + "\n" + done.stdout, remote.host))
            partial.rename(target)
            finished = job.model_copy(update={"state": "ready", "detail": "", "finished_at": datetime.now(UTC)})
        except FileNotFoundError:
            finished = job.model_copy(update={"state": "failed", "detail": "git is not installed on this PC", "finished_at": datetime.now(UTC)})
        except subprocess.TimeoutExpired:
            finished = job.model_copy(update={"state": "failed", "detail": f"the clone took longer than {int(self.timeout // 60)} min and was stopped", "finished_at": datetime.now(UTC)})
        except (RuntimeError, OSError) as error:
            finished = job.model_copy(update={"state": "failed", "detail": str(error)[:600], "finished_at": datetime.now(UTC)})
        finally:
            self._running.release()
        if finished.state == "failed":
            self._remove(partial)
        self.jobs.put(finished)

    @staticmethod
    def _remove(folder: Path) -> None:
        """Deletes a partial clone (git marks its objects read-only: Windows refuses those)."""
        def writable(function, path, _error) -> None:
            os.chmod(path, stat.S_IWRITE)
            function(path)
        if folder.exists():
            shutil.rmtree(folder, onerror=writable)
