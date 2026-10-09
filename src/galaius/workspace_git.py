"""git as galaius runs it for the web on one PC: fixed options and environment, one shared
deadline, a failure told as a `WorkspaceFailureCode` plus the PC's own sentence (`CloneFailure`)."""

import shutil
import subprocess
import time
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from galaius_core import WorkspaceFailureCode


class WorkspaceRefused(PermissionError):
    """A project folder that cannot be put in place, refreshed or sent, with the code the server
    words for the owner (`WorkspaceFailureCode`)."""

    def __init__(self, code: WorkspaceFailureCode, said: str) -> None:
        super().__init__(said)
        self.code = code


class CloneFailure(BaseModel):
    """One way `git clone` fails: `marks` are pieces of git's / ssh's error output, `code` what the
    server words for the owner, `said` the PC's own sentence for its log ({host} filled in)."""

    model_config = ConfigDict(frozen=True)
    marks: tuple[str, ...]
    code: WorkspaceFailureCode
    said: str

    #: First match wins: the most specific cause first.
    KNOWN: ClassVar[tuple["CloneFailure", ...]]

    @classmethod
    def explain(cls, output: str, host: str) -> WorkspaceRefused:
        lowered = output.lower()
        found = next((failure for failure in cls.KNOWN if any(mark in lowered for mark in failure.marks)), None)
        if found is not None:
            return WorkspaceRefused(found.code, found.said.format(host=host))
        last = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "no output")
        return WorkspaceRefused("failed", f"git clone failed: {last}"[:600])


CloneFailure.KNOWN = (
    CloneFailure(marks=("host key verification failed", "host key is known", "no matching host key", "remote host identification has changed"), code="host_unknown",
                 said="this PC has never connected to {host} over SSH, so it does not know its host key: use the https address, or connect to {host} once from this PC"),
    CloneFailure(marks=("permission denied (publickey",), code="clone_auth_refused", said="{host} refused this PC's SSH key: add this PC's public key to your account there, or use the https address"),
    CloneFailure(marks=("could not read username", "terminal prompts disabled", "authentication failed", "invalid username or password", "could not read password"), code="clone_auth_refused",
                 said="this PC has no saved git sign-in for {host}: sign in to {host} once with git on this PC, or use an address this PC can already read"),
    CloneFailure(marks=("repository not found", "does not appear to be a git repository", "not found"), code="repository_not_found",
                 said="{host} says this repository does not exist, or this PC's git account cannot see it"),
    CloneFailure(marks=("could not resolve host", "name or service not known", "temporary failure in name resolution"), code="host_unreachable", said="this PC cannot reach {host} (its name does not resolve)"),
    CloneFailure(marks=("transport", "not allowed"), code="failed", said="a submodule uses a transport other than https or ssh, which is refused"),
    CloneFailure(marks=("no space left on device",), code="disk_full", said="this PC ran out of disk space during the clone"),
)


class Git(BaseModel):
    """git as galaius runs it for the web: fixed options and environment, one shared deadline, a
    failure told as a code (`CloneFailure`). Whatever a repository's own config says, git speaks only
    `PROTOCOLS` (`GIT_ALLOW_PROTOCOL` beats every protocol setting in config, a per-protocol
    `allow` included)."""

    model_config = ConfigDict(frozen=True)
    options: tuple[str, ...]
    environment: dict[str, str]
    host: str
    deadline: float
    PROTOCOLS: ClassVar[str] = "https:ssh"

    @staticmethod
    def installed(environment: dict[str, str]) -> bool:
        return shutil.which("git", path=environment.get("PATH")) is not None

    def run(self, *arguments: str, allow: tuple[int, ...] = ()) -> str:
        """git's output (undecodable bytes kept as surrogates); a failure (an exit code outside 0 and
        `allow`) raises `WorkspaceRefused`, git missing `git_missing`."""
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("git deadline passed")
        try:
            done = subprocess.run(["git", *self.options, *arguments], env={**self.environment, "GIT_ALLOW_PROTOCOL": self.PROTOCOLS}, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  errors="surrogateescape", timeout=left, check=False)
        except FileNotFoundError:
            raise WorkspaceRefused("git_missing", "git is not installed on this PC") from None
        if done.returncode != 0 and done.returncode not in allow:
            raise CloneFailure.explain(done.stderr + "\n" + done.stdout, self.host)
        return done.stdout
