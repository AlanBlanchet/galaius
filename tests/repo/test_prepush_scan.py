"""The pre-push scan runs the repository's own rules on every push, whatever the push changes (2026-11-23: a new module made `galaius/agents` hold 21 files, the
affected-tests scan named no test for it, and main went red on GitHub)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCAN = Path(__file__).resolve().parents[2] / ".githooks" / "affected-tests.sh"


def git(folder: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t", *arguments], capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(sys.platform == "win32", reason="the pre-push scan is a POSIX shell script: the owner pushes from Linux, Windows has no `bash` it can rely on")
def test_a_push_that_changes_no_test_still_runs_the_repository_rules_but_not_the_browser_one(tmp_path: Path) -> None:
    (tmp_path / "tests" / "repo").mkdir(parents=True)
    for name in ("test_folder_size.py", "test_module_names.py", "test_workplace_motion.py"):
        (tmp_path / "tests" / "repo" / name).write_text("def test_x(): pass\n")
    (tmp_path / "README.md").write_text("one\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "one")
    (tmp_path / "README.md").write_text("two\n")
    (tmp_path / "newmodule.py").write_text("x = 1\n")   # a source file no test names
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "two")
    done = subprocess.run(["bash", str(SCAN), "HEAD~1..HEAD"], cwd=tmp_path, capture_output=True, text=True, env={**os.environ, "PREPUSH_LIST_ONLY": "1"})
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["tests/repo/test_folder_size.py", "tests/repo/test_module_names.py"]
    empty = subprocess.run(["bash", str(SCAN), "HEAD..HEAD"], cwd=tmp_path, capture_output=True, text=True, env={**os.environ, "PREPUSH_LIST_ONLY": "1"})
    assert empty.stdout == "", "nothing pushed, nothing run"
