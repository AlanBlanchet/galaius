"""A project's secrets from the server's vault, written as the `.env` of its checkout on this PC at
a run's start - never as environment variables (a run's later turns are dispatched apart and every
child would inherit them; threat model run 461f2737).

The file goes only into a checkout of the project's own repository (its git origin, read from
`.git/config`), at that checkout's root, as `NAME='value'` lines (names and values were checked by
`interact_core.ProjectSecretName` / `ProjectSecretText`); it is written by interact (its first line
says so) and rewritten whole at each start that brings secrets, so a deleted secret leaves it then.
Never written over a link, an `.env` the owner wrote, or one git tracks; `.env` is added to the
repository's own `.git/info/exclude` (local, never its `.gitignore`)."""

import os
from hashlib import sha256
import subprocess
from pathlib import Path
from uuid import UUID

from interact_core.sealing import SecretsSeal
from pydantic import BaseModel, ConfigDict

from interact.machine_workspaces import MachineWorkspaces

#: The first line of every `.env` interact writes: the only kind it ever rewrites.
MARKER = "# Written by interact from the project's vault"


class ProjectEnv(BaseModel):
    """The `.env` of one checkout of a project's repository."""

    model_config = ConfigDict(frozen=True)
    checkout: Path

    @classmethod
    def locate(cls, folder: Path, top: Path, origin: str) -> "ProjectEnv":
        """The checkout holding `folder` (itself or a parent, never above `top`), when it comes from
        `origin`; PermissionError otherwise."""
        current = folder.resolve()
        top = top.resolve()
        while True:
            if (current / ".git").exists():
                if MachineWorkspaces.origin_of(current) != origin.lower():
                    raise PermissionError(f"this folder is not a checkout of {origin}: the project's secrets go only into its own repository")
                return cls(checkout=current)
            if current == top or top not in current.parents:
                raise PermissionError(f"no checkout of {origin} here: the project's secrets go only into its own repository")
            current = current.parent

    @property
    def path(self) -> Path:
        return self.checkout / ".env"

    def _refusal(self) -> str | None:
        if self.path.is_symlink():
            return ".env is a link here; secrets are never written through one"
        if self.path.exists():
            try:
                with self.path.open(encoding="utf-8", errors="replace") as stream:
                    first = stream.readline().rstrip("\n")
            except OSError as error:
                return f".env cannot be read here ({error.strerror})"
            if not first.startswith(MARKER):
                return "an .env you wrote is already here; interact never writes over it (rename it to let the vault's secrets in)"
        tracked = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(self.checkout), "ls-files", "--error-unmatch", "--", ".env"],
                                 stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
        if tracked.returncode == 0:
            return ".env is tracked by git in this repository; secrets would be committed, so they are not written"
        return None

    def write(self, values: dict[str, str], *, project: UUID) -> Path:
        """Writes the whole file (temporary file in the same folder, then renamed over), 0600; its
        first line names the vault state it holds (sha256 of the values, kept on this PC only)."""
        if (refused := self._refusal()) is not None:
            raise PermissionError(refused)
        revision = sha256(SecretsSeal.plain(values)).hexdigest()
        lines = [f"{MARKER} (project {project}, revision {revision[:12]}): do not commit; rewritten at each start that brings secrets.",
                 *(f"{name}='{value}'" for name, value in sorted(values.items()))]
        temporary = self.checkout / f".env.interact-{os.getpid()}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write("\n".join(lines) + "\n")
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        self._exclude()
        return self.path

    def _exclude(self) -> None:
        """`.env` in the repository's own local exclude list (shared by its worktrees)."""
        git = MachineWorkspaces.git_dir(self.checkout)
        if git is None:
            return
        exclude = git / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        current = exclude.read_text(encoding="utf-8", errors="replace") if exclude.is_file() else ""
        if ".env" not in current.splitlines():
            with exclude.open("a", encoding="utf-8") as stream:
                stream.write(("" if current.endswith("\n") or not current else "\n") + ".env\n")
