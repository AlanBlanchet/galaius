"""Running real `git` against a test-owned repository — the same subprocess shape two files
rebuilt (one wanting stripped stdout text and check=True-or-raise, the other a full
`CompletedProcess` under a sandboxed environment)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(
    repository: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """`git -C repository *arguments`, captured as text. Raises on a non-zero exit unless
    ``check=False`` — a caller inspecting failure (a conflicted merge, a rejected push) wants
    that off."""
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        env=env,
        check=check,
        timeout=timeout,
    )


def git_out(repository: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    """`run_git`, for the common fixture-setup case that only wants stdout text and a hard
    failure on a non-zero exit."""
    return run_git(repository, *arguments, env=env).stdout


def init_repo(path: Path, *, bare: bool = False, branch: str = "main") -> Path:
    """A fresh `git init`, with a stable initial-branch name. A non-bare repo also gets a
    throwaway committer identity configured locally, so a plain `git commit` (no `-c`
    overrides needed at every call site) succeeds even under an isolated HOME."""
    path.mkdir(parents=True, exist_ok=True)
    args = ["init", "-q", f"--initial-branch={branch}"]
    if bare:
        args.append("--bare")
    run_git(path, *args)
    if not bare:
        run_git(path, "config", "user.email", "test@example.invalid")
        run_git(path, "config", "user.name", "Test")
    return path


def commit_all(repository: Path, message: str) -> None:
    """Stage every change and commit it under a throwaway identity — passed as `-c` overrides
    so it commits whether or not the repository has a local identity configured (`init_repo`
    sets one; a clone under a fixture's own isolated environment may not)."""
    run_git(repository, "add", "-A")
    run_git(
        repository,
        "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid",
        "commit", "-qm", message,
    )
