"""A machine's file roots as Data reads them (MachineDataRequest): beneath one named root, walked
without following links; hidden names, hard links, '..' and other roots are unreachable."""

import base64
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from interact_core import MachineDataRequest, MachineRef

from interact.machines import MachineConfig, MachineDataFiles

pytestmark = pytest.mark.usefixtures("directory_backend")


@pytest.fixture
def files(tmp_path: Path) -> MachineDataFiles:
    shared, private = tmp_path / "shared", tmp_path / "private"
    (shared / "reports").mkdir(parents=True)
    private.mkdir()
    (shared / "reports" / "q3.txt").write_bytes(b"Q3 revenue up, costs flat")
    (shared / ".env").write_bytes(b"SECRET")
    (private / "salaries.csv").write_bytes(b"name,salary")
    (shared / "to-private").symlink_to(private)  # a link from one root into another
    (shared / "copy.csv").hardlink_to(private / "salaries.csv")  # a second name for a private file
    config = MachineConfig(server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(), token="iwm_" + "x" * 48,
                           permission_ceiling="read_only", working_directory=tmp_path, file_roots=("shared", "private"))
    return MachineDataFiles(config=config)


def _ask(files: MachineDataFiles, op: str, root: str = "", path: str = "", offset: int = 0, length: int = 0):
    request = MachineDataRequest(id=uuid4(), machine=MachineRef(id=files.config.machine_id), workspace_id=files.config.workspace_id, op=op, root=root, path=path,
                                 offset=offset, length=length, expires_at=datetime.now(UTC) + timedelta(seconds=15), signature="0" * 64)
    return files.answer(request)


def test_lists_roots_and_visible_plain_entries_only(files) -> None:
    assert [entry.name for entry in _ask(files, "list").entries] == ["shared", "private"]
    assert [(entry.name, entry.kind) for entry in _ask(files, "list", "shared").entries] == [("reports", "folder")]  # no link, no hidden, no hard link


def test_reads_a_file_by_slices_with_its_identity(files) -> None:
    first = _ask(files, "read", "shared", "reports/q3.txt", 0, 5)
    rest = _ask(files, "read", "shared", "reports/q3.txt", 5, 100)
    assert base64.b64decode(first.data) + base64.b64decode(rest.data) == b"Q3 revenue up, costs flat"
    assert first.size == 25 and first.identity == rest.identity


@pytest.mark.parametrize("path", ["to-private/salaries.csv", "copy.csv", ".env", "../private/salaries.csv", "reports/../../private/salaries.csv", "/etc/passwd"])
def test_nothing_outside_the_named_root_is_reachable(files, path) -> None:
    with pytest.raises((PermissionError, OSError)):
        _ask(files, "read", "shared", path, 0, 10)


def test_an_unknown_root_is_refused(files) -> None:
    with pytest.raises(PermissionError, match="not one of this machine's file roots"):
        _ask(files, "list", "elsewhere")
