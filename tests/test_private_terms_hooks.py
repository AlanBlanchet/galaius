"""The tracked hooks refuse private material listed in the untracked `<git-common-dir>/info/private-terms`,
through every channel it could leave by: a staged diff, a commit message, a pushed commit's message, a
pushed branch name. A clean push still goes through, so the guard is a filter, not a wall."""

import os
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / ".githooks"
TERM = "zz-sample-private-term"


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", f"core.hooksPath={HOOKS}")
    return subprocess.run(["git", *identity, *args], cwd=cwd, env=env, text=True, capture_output=True, check=False)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / ".git" / "info" / "private-terms").write_text(f"# sample list\n{TERM}\n")
    git(work, "remote", "add", "origin", str(remote))
    (work / "seed.txt").write_text("seed\n")
    git(work, "add", "seed.txt")
    assert git(work, "commit", "-q", "--no-verify", "-m", "seed").returncode == 0
    return work


def commit_unchecked(work: Path, text: str, message: str) -> None:
    (work / "file.txt").write_text(text)
    git(work, "add", "file.txt")
    assert git(work, "commit", "-q", "--no-verify", "-m", message).returncode == 0


def test_commit_refuses_a_staged_diff_naming_a_private_term(work: Path) -> None:
    (work / "file.txt").write_text(f"see {TERM.upper()} for details\n")
    git(work, "add", "file.txt")
    result = git(work, "commit", "-m", "clean message")
    assert result.returncode != 0 and "private material" in result.stdout + result.stderr
    assert git(work, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_commit_refuses_a_message_naming_a_private_term(work: Path) -> None:
    result = subprocess.run([HOOKS / "commit-msg", "/dev/stdin"], cwd=work, input=f"fix: drop {TERM}\n", text=True, capture_output=True, check=False)
    assert result.returncode != 0 and "commit message" in result.stderr


@pytest.mark.parametrize(
    ("text", "message", "ref", "channel"),
    [
        (f"{TERM}\n", "clean", "main", "in a diff"),
        ("clean\n", f"mentions {TERM}", "main", "in a commit message"),
        ("clean\n", "clean", TERM, "in a branch name"),
    ],
)
def test_push_refuses_every_channel(work: Path, text: str, message: str, ref: str, channel: str) -> None:
    commit_unchecked(work, text, message)
    result = git(work, "push", "origin", f"HEAD:refs/heads/{ref}")
    assert result.returncode != 0 and f"private term {channel}" in result.stderr
    assert git(work, "ls-remote", "origin").stdout == ""


def test_push_lets_clean_commits_through(work: Path) -> None:
    commit_unchecked(work, "clean\n", "clean")
    assert git(work, "push", "origin", "HEAD:refs/heads/main").returncode == 0


def test_force_push_over_a_remote_tip_this_clone_never_saw_is_still_scanned(work: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote.git"), str(other)], check=True)
    commit_unchecked(other, "theirs\n", "theirs")
    assert git(other, "push", "-q", "origin", "HEAD:refs/heads/main").returncode == 0
    commit_unchecked(work, f"{TERM}\n", "clean")
    result = git(work, "push", "--force", "origin", "HEAD:refs/heads/main")
    assert result.returncode != 0 and "private term in a diff" in result.stderr


def test_push_refuses_adding_private_notes_but_lets_their_removal_through(work: Path) -> None:
    notes = work / ".github" / "research" / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("notes\n")
    git(work, "add", "-f", str(notes))
    assert git(work, "commit", "-q", "--no-verify", "-m", "notes").returncode == 0
    result = git(work, "push", "origin", "HEAD:refs/heads/main")
    assert result.returncode != 0 and "private notes/research files" in result.stderr
    assert git(work, "push", "-q", "--no-verify", "origin", "HEAD:refs/heads/main").returncode == 0  # an old leak
    git(work, "rm", "-q", "--cached", str(notes))
    assert git(work, "commit", "-q", "--no-verify", "-m", "drop notes").returncode == 0
    assert git(work, "push", "origin", "HEAD:refs/heads/main").returncode == 0
