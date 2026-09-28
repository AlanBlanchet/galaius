"""Owner-only files (`PRIVATE_FILES`): what `interact login` and the machine runner save their
credentials in, on POSIX (mode + uid) and Windows (a DACL naming only this user, DPAPI-sealed)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from interact.file_lock import exclusive
from interact.private_files import PRIVATE_FILES
from tests.support.private_files import loosen

WINDOWS = sys.platform == "win32"


def test_secret_round_trips_sealed_at_rest_and_private(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "server.key"
    PRIVATE_FILES.write_secret(path, "ik_secret-value")
    assert PRIVATE_FILES.read_secret(path) == "ik_secret-value"
    PRIVATE_FILES.check(path)
    PRIVATE_FILES.check(path.parent)
    assert ("ik_secret-value" in path.read_text()) != WINDOWS  # Windows keeps it DPAPI-sealed on disk
    PRIVATE_FILES.write_secret(path, "ik_rotated")  # replaced atomically, still private
    assert PRIVATE_FILES.read_secret(path) == "ik_rotated"
    assert [item.name for item in path.parent.iterdir()] == ["server.key"]  # no temporary left behind


@pytest.mark.parametrize("case", ["loosened", "link", "oversize", "empty"])
def test_secret_file_refused_unless_private_plain_and_bounded(tmp_path: Path, case: str) -> None:
    path = tmp_path / "token"
    PRIVATE_FILES.write_text(path, {"oversize": "x" * 4097, "empty": "\n"}.get(case, "test-token\n"))
    if case == "loosened":
        loosen(path)
    if case == "link":
        path = tmp_path / "linked"
        path.symlink_to(tmp_path / "token")
    with pytest.raises(ValueError, match="token file"):
        PRIVATE_FILES.read_secret(path)


def test_hand_written_token_is_read_as_written(tmp_path: Path) -> None:
    """A token its owner wrote (never sealed) still reads once the file is private."""
    path = tmp_path / "token"
    path.write_text("owner-token\n")
    PRIVATE_FILES.restrict(path)
    assert PRIVATE_FILES.read_secret(path) == "owner-token"


def test_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    lock = tmp_path / "state.lock"
    with exclusive(os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)):
        probe = subprocess.run([sys.executable, "-c", (
            "import os, sys, threading; from interact.file_lock import exclusive\n"
            f"d = os.open({str(lock)!r}, os.O_RDWR)\n"
            "t = threading.Thread(target=lambda: exclusive(d).__enter__(), daemon=True); t.start(); t.join(1.5)\n"
            "sys.exit(0 if t.is_alive() else 3)")], timeout=30)
        assert probe.returncode == 0  # another process waits while this one holds it
    with exclusive(os.open(lock, os.O_RDWR)):
        pass  # released: taken again at once
