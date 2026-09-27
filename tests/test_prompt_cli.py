"""`interact prompts` — the CLI users type.

Formerly split across `test_prompt_editor_cli` (write / lock / conflict semantics of `interact
prompts read|write`, whose CAS + advisory lock guards the editor and any concurrent second
writer) and `test_prompts_cli` (the git-shaped workflow: clone, commit, pull, push, resolve,
compile, install, publish, sync). Same CLI, two facets — kept as two clearly labelled blocks.

Every test drives the real `interact prompts` binary in a subprocess with `HOME` and
`XDG_DATA_HOME` redirected under `tmp_path`; nothing here reaches for the owner's configured
prompt server.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from interact.cli import prompts as prompt_commands
from interact.config import UserConfig
from tests.support import commit_all, init_repo, run_git


# ── Editor CAS + advisory lock (`interact prompts catalog|read|write`) ─────────────────────


@pytest.fixture(autouse=True)
def unconfigured_prompt_home(tmp_path, monkeypatch):
    """Local-Git tests must not inherit the owner's configured server connection."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(UserConfig, "PATH", tmp_path / "home" / ".interact" / "config.env")


def _prompt_cli(data_home: Path, *arguments: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update({"XDG_DATA_HOME": str(data_home), "UV_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    return subprocess.run(
        ["uv", "run", "interact", "prompts", *arguments],
        input=stdin, text=True, capture_output=True, timeout=30, env=environment,
    )


def test_prompt_editor_cli_catalog_read_and_cas_conflict_preserve_disk_and_buffer(tmp_path: Path) -> None:
    source = tmp_path / "interact" / "prompts"
    (source / "agents").mkdir(parents=True)
    prompt = source / "agents" / "review.md"
    prompt.write_text("first")
    (source / "outside.md").write_text("outside")
    (source / "agents" / "link.md").symlink_to(source / "outside.md")

    catalog = _prompt_cli(tmp_path, "catalog")
    assert catalog.returncode == 0
    assert json.loads(catalog.stdout) == {"ok": True, "files": ["agents/review.md", "outside.md"]}
    read = _prompt_cli(tmp_path, "read", "agents/review.md")
    payload = json.loads(read.stdout)
    assert payload["content"] == "first"
    prompt.write_text("other client")
    conflict = _prompt_cli(tmp_path, "write", "agents/review.md", payload["digest"], stdin="my buffer")
    assert conflict.returncode == 2
    assert json.loads(conflict.stdout)["error"].startswith("prompt source changed")
    assert prompt.read_text() == "other client"


def test_prompt_editor_rejects_a_symlinked_parent_outside_the_source(tmp_path: Path) -> None:
    source = tmp_path / "interact" / "prompts"
    outside = tmp_path / "outside"
    source.mkdir(parents=True)
    outside.mkdir()
    (outside / "escaped.md").write_text("private")
    (source / "linked").symlink_to(outside, target_is_directory=True)

    for command in (("read", "linked/escaped.md"),
                    ("write", "linked/escaped.md", hashlib.sha256(b"private").hexdigest())):
        result = _prompt_cli(tmp_path, *command, stdin="replacement")
        assert result.returncode == 2
    assert (outside / "escaped.md").read_text() == "private"


def test_prompt_editor_releases_its_lock_when_post_lock_validation_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "interact" / "prompts"
    source.mkdir(parents=True)
    target = source / "instructions.md"
    target.write_text("old")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(prompt_commands.os, "rename", lambda *args, **kwargs: (_ for _ in ()).throw(
        OSError("path changed after lock")
    ))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"new")))
    with pytest.raises(SystemExit) as raised:
        prompt_commands.write("instructions.md", hashlib.sha256(b"old").hexdigest())
    assert raised.value.code == 2
    assert not (source / ".instructions.md.interact.lock").exists()


def test_prompt_editor_cli_writes_exact_stdin_after_matching_digest(tmp_path: Path) -> None:
    source = tmp_path / "interact" / "prompts"
    source.mkdir(parents=True)
    prompt = source / "instructions.md"
    prompt.write_text("old")
    expected = hashlib.sha256(b"old").hexdigest()
    written = _prompt_cli(tmp_path, "write", "instructions.md", expected, stdin="new content")
    assert written.returncode == 0, written.stderr
    assert prompt.read_text() == "new content"
    assert json.loads(written.stdout)["digest"] == hashlib.sha256(b"new content").hexdigest()


def test_two_overlapping_prompt_writes_have_one_winner_and_one_typed_conflict(tmp_path: Path) -> None:
    source = tmp_path / "interact" / "prompts"
    source.mkdir(parents=True)
    prompt = source / "instructions.md"
    prompt.write_text("old")
    expected = hashlib.sha256(b"old").hexdigest()
    environment = os.environ.copy()
    environment.update({"XDG_DATA_HOME": str(tmp_path), "UV_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    processes = [subprocess.Popen(
        ["uv", "run", "interact", "prompts", "write", "instructions.md", expected],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
    ) for _ in range(2)]
    results = [process.communicate(content, timeout=30) + (process.returncode,)
               for process, content in zip(processes, ("first", "second"), strict=True)]
    assert sorted(result[2] for result in results) == [0, 2]
    loser = next(result for result in results if result[2] == 2)
    assert json.loads(loser[0])["code"] == "conflict"
    assert prompt.read_text() in {"first", "second"}
    assert not list(source.glob(".*.interact-*"))


def test_contenders_never_remove_the_active_editor_lock(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "interact" / "prompts"
    source.mkdir(parents=True)
    prompt = source / "instructions.md"
    prompt.write_text("old")
    expected = hashlib.sha256(b"old").hexdigest()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"owner")))
    entered, release = threading.Event(), threading.Event()
    original_fsync = prompt_commands.os.fsync

    def paused_fsync(descriptor: int) -> None:
        entered.set()
        assert release.wait(10)
        original_fsync(descriptor)

    monkeypatch.setattr(prompt_commands.os, "fsync", paused_fsync)
    owner = threading.Thread(target=prompt_commands.write, args=("instructions.md", expected))
    owner.start()
    assert entered.wait(10)
    lock = source / ".instructions.md.interact.lock"
    identity = (lock.stat().st_ino, lock.read_bytes())
    try:
        contenders = [_prompt_cli(tmp_path, "write", "instructions.md", expected, stdin=value)
                      for value in ("second", "third")]
        assert all(result.returncode == 2 and json.loads(result.stdout)["code"] == "conflict"
                   for result in contenders)
        assert lock.exists() and (lock.stat().st_ino, lock.read_bytes()) == identity
    finally:
        release.set()
        owner.join(10)
    assert not owner.is_alive()
    assert prompt.read_text() == "owner"
    assert not lock.exists()


def test_visible_prompt_actions_cross_the_canonical_git_and_install_boundaries(tmp_path: Path) -> None:
    source = tmp_path / "data" / "interact" / "prompts"
    shutil.copytree(Path("tests/fixtures/prompt_source"), source)
    (source / "hooks" / "hook.sh").chmod(0o755)
    init_repo(source, branch="main")
    commit_all(source, "initial")
    environment = os.environ.copy()
    environment.update({
        "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "INTERACT_PROMPT_CONSUMER_ROOT": str(tmp_path / "consumers"),
        "INTERACT_PROMPT_VSCODE_ROOT": str(tmp_path / "vscode"), "UV_OFFLINE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_AUTHOR_NAME": "Fixture Author", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture Author", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_ASKPASS": str(tmp_path / "must-not-run-askpass"),
        "SSH_ASKPASS": str(tmp_path / "must-not-run-ssh-askpass"),
        "BROWSER": str(tmp_path / "must-not-run-browser"),
    })
    def action(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["uv", "run", "interact", "prompts", *args], text=True,
                              capture_output=True, timeout=60, env=environment)
    assert action("status").returncode == 0
    assert action("log").returncode == 0
    (source / "instructions.md").write_text("changed\n")
    committed = action("commit", "--message", "dated prompt edit")
    assert committed.returncode == 0, committed.stderr
    assert "dated prompt edit" in action("log").stdout
    assert action("compile").returncode == 0
    assert action("install").returncode == 0
    (source / "instructions.md").write_text("dirty\n")
    before = (source / "instructions.md").read_bytes()
    for args in (("pull",), ("sync",), ("install",),
                 ("publish", "https://unavailable.invalid", str(tmp_path / "token"))):
        failed = action(*args)
        assert failed.returncode != 0, args
        assert (source / "instructions.md").read_bytes() == before


# ── Git-shaped workflow (`clone / status / commit / pull / push / resolve / …`) ───────────


COMMANDS = (
    "clone", "status", "diff", "commit", "log", "pull", "push", "resolve", "compile",
    "install", "publish", "sync",
)


def _git_environment(data_home: Path) -> dict[str, str]:
    return {
        "GIT_AUTHOR_EMAIL": "prompts@example.invalid",
        "GIT_AUTHOR_NAME": "Prompt Author",
        "GIT_COMMITTER_EMAIL": "prompts@example.invalid",
        "GIT_COMMITTER_NAME": "Prompt Author",
        "LANG": "C.UTF-8",
        "PATH": os.environ["PATH"],
        "XDG_DATA_HOME": str(data_home),
        "HOME": str(data_home / "isolated-home"),
    }


def _interact(data_home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(Path(sys.executable).with_name("interact")), "prompts", *arguments],
        capture_output=True,
        env=_git_environment(data_home),
        text=True,
        timeout=20,
    )


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return run_git(repository, *arguments, env=_git_environment(repository.parent),
                    check=False, timeout=20)


def test_prompts_help_exposes_the_complete_local_first_workflow(tmp_path):
    result = _interact(tmp_path / "data", "--help")

    assert result.returncode == 0, result.stderr
    for command in COMMANDS:
        assert command in result.stdout


def test_prompts_clone_creates_two_independent_worktrees_from_one_local_remote(tmp_path):
    remote = tmp_path / "prompts.git"
    init_repo(remote, bare=True)

    for client in ("client-a", "client-b"):
        data_home = tmp_path / client
        cloned = _interact(data_home, "clone", str(remote))
        worktree = data_home / "interact" / "prompts"

        assert cloned.returncode == 0, cloned.stderr
        assert (worktree / ".git").is_dir()
        origin = run_git(worktree, "remote", "get-url", "origin", check=False, timeout=20)
        assert origin.returncode == 0, origin.stderr
        assert Path(origin.stdout.strip()).resolve() == remote.resolve()


def test_real_git_clients_converge_and_preserve_renames_deletes_and_history(tmp_path):
    remote = tmp_path / "prompts.git"
    init_repo(remote, bare=True)
    homes = [tmp_path / name for name in ("a", "b", "c")]
    assert _interact(homes[0], "clone", str(remote)).returncode == 0
    worktree_a = homes[0] / "interact" / "prompts"
    (worktree_a / "agents").mkdir()
    (worktree_a / "agents" / "librarian.md").write_text("first\n")
    (worktree_a / "agents" / "obsolete.md").write_text("remove me\n")
    committed = _interact(homes[0], "commit", "-m", "initial prompt")
    assert committed.returncode == 0, committed.stderr
    pushed = _interact(homes[0], "push")
    assert pushed.returncode == 0, pushed.stderr

    assert _interact(homes[1], "clone", str(remote)).returncode == 0
    worktree_b = homes[1] / "interact" / "prompts"
    (worktree_a / "agents" / "author.md").write_text("from a\n")
    assert _interact(homes[0], "commit", "-m", "independent a").returncode == 0
    assert _interact(homes[0], "push").returncode == 0
    (worktree_b / "agents" / "librarian.md").rename(worktree_b / "agents" / "prompt-librarian.md")
    (worktree_b / "agents" / "obsolete.md").unlink()
    (worktree_b / "agents" / "tester.md").write_text("test\n")
    assert _interact(homes[1], "commit", "-m", "rename and add").returncode == 0
    assert _interact(homes[1], "pull").returncode == 0
    assert _interact(homes[1], "push").returncode == 0

    (worktree_a / "agents" / "librarian.md").unlink()
    pulled = _interact(homes[0], "pull")
    assert pulled.returncode != 0
    assert (worktree_a / "agents" / "librarian.md").exists() is False
    assert (worktree_a / "agents" / "prompt-librarian.md").exists() is False
    _git(worktree_a, "restore", "agents/librarian.md")
    assert _interact(homes[0], "pull").returncode == 0

    assert _interact(homes[2], "clone", str(remote)).returncode == 0
    worktree_c = homes[2] / "interact" / "prompts"
    assert (worktree_c / "agents" / "prompt-librarian.md").read_bytes() == b"first\n"
    assert (worktree_c / "agents" / "tester.md").read_bytes() == b"test\n"
    assert (worktree_c / "agents" / "author.md").read_bytes() == b"from a\n"
    assert (worktree_c / "agents" / "obsolete.md").exists() is False
    assert _git(worktree_c, "rev-list", "--all").stdout == _git(worktree_a, "rev-list", "--all").stdout
    history = _interact(homes[2], "log")
    assert history.returncode == 0, history.stderr
    assert "initial prompt" in history.stdout and "rename and add" in history.stdout


def test_divergent_clients_surface_conflict_and_stage_only_explicit_resolution(tmp_path):
    remote = tmp_path / "prompts.git"
    init_repo(remote, bare=True)
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    assert _interact(home_a, "clone", str(remote)).returncode == 0
    worktree_a = home_a / "interact" / "prompts"
    (worktree_a / "shared.md").write_text("base\n")
    assert _interact(home_a, "commit", "-m", "base").returncode == 0
    assert _interact(home_a, "push").returncode == 0
    assert _interact(home_b, "clone", str(remote)).returncode == 0
    worktree_b = home_b / "interact" / "prompts"

    (worktree_a / "shared.md").write_text("from a\n")
    assert _interact(home_a, "commit", "-m", "a edit").returncode == 0
    assert _interact(home_a, "push").returncode == 0
    (worktree_b / "shared.md").write_text("from b\n")
    assert _interact(home_b, "commit", "-m", "b edit").returncode == 0
    conflicted = _interact(home_b, "pull")

    assert conflicted.returncode != 0
    assert "<<<<<<<" in (worktree_b / "shared.md").read_text()
    rejected = _interact(home_b, "resolve", "not-conflicted.md")
    assert rejected.returncode != 0
    (worktree_b / "shared.md").write_text("from a\nfrom b\n")
    resolved = _interact(home_b, "resolve", "shared.md")
    assert resolved.returncode == 0, resolved.stderr
    assert _git(worktree_b, "diff", "--name-only", "--diff-filter=U").stdout == ""


def test_remote_outage_preserves_local_commit_and_working_file(tmp_path):
    remote = tmp_path / "prompts.git"
    init_repo(remote, bare=True)
    home = tmp_path / "client"
    assert _interact(home, "clone", str(remote)).returncode == 0
    worktree = home / "interact" / "prompts"
    authored = worktree / "offline.md"
    authored.write_text("available offline\n")
    assert _interact(home, "commit", "-m", "offline source").returncode == 0
    assert _interact(home, "push").returncode == 0
    head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    remote.rename(tmp_path / "remote-unavailable")

    failed = _interact(home, "sync")

    assert failed.returncode != 0
    assert authored.read_text() == "available offline\n"
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == head
    assert _interact(home, "status").returncode == 0
    assert _interact(home, "log").returncode == 0
