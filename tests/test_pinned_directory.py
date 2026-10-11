"""The portable directory handle: which entries count as links, which names are one entry."""

import os
import stat
import sys
from types import SimpleNamespace

import pytest

from galaius.pinned_directory import PathDirectory, PinnedDirectory


@pytest.mark.parametrize(("mode", "attributes", "tag", "link"), [
    (stat.S_IFLNK, 0, 0, True),  # a symlink, any system
    (stat.S_IFDIR, stat.FILE_ATTRIBUTE_REPARSE_POINT, 0xA0000003, True),  # a Windows junction
    (stat.S_IFREG, stat.FILE_ATTRIBUTE_REPARSE_POINT, 0xA000000C, True),  # a Windows file symlink
    (stat.S_IFREG, stat.FILE_ATTRIBUTE_REPARSE_POINT, 0x9000601A, False),  # a OneDrive placeholder
    (stat.S_IFDIR, stat.FILE_ATTRIBUTE_REPARSE_POINT, 0x80000013, False),  # a deduplicated folder
    (stat.S_IFREG, 0, 0, False),
], ids=["symlink", "junction", "file-symlink", "cloud-placeholder", "dedup", "plain"])
def test_windows_refuses_name_surrogates_only(mode: int, attributes: int, tag: int, link: bool) -> None:
    facts = SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_reparse_tag=tag)
    assert PathDirectory.link_like(facts) is link


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b"])
def test_an_entry_name_is_one_component(tmp_path, directory_backend, name: str) -> None:
    if name == "a\\b" and directory_backend is not PathDirectory:
        pytest.skip("a backslash is an ordinary POSIX file-name character")
    with PinnedDirectory.open(tmp_path) as folder, pytest.raises(ValueError, match="entry name"):
        folder.mkdir(name)


@pytest.mark.skipif(sys.platform == "win32", reason="the descriptor-relative folder is POSIX")
@pytest.mark.parametrize(("flags", "losses", "asked", "opened"), [
    (os.O_RDWR | os.O_CREAT, 1, 2, True),   # lost the creation race once (macOS)
    (os.O_RDWR | os.O_CREAT, 9, 3, False),  # the folder is gone: ENOENT after CREATE_ATTEMPTS
    (os.O_RDONLY, 1, 1, False),             # no create: absent is the answer
], ids=["race", "persistent", "no-create"])
def test_a_create_open_losing_a_creation_race_opens_the_entry(tmp_path, monkeypatch, flags: int, losses: int, asked: int, opened: bool) -> None:
    real, calls = os.open, []

    def racing_open(*args, **kwargs):
        calls.append(args[0])
        if len(calls) <= losses:
            raise FileNotFoundError(2, "No such file or directory", args[0])
        return real(*args, **kwargs)

    with PinnedDirectory.open(tmp_path) as folder:
        monkeypatch.setattr(os, "open", racing_open)
        if opened:
            os.close(folder.file("run.lock", flags))
        else:
            with pytest.raises(FileNotFoundError):
                folder.file("run.lock", flags)
    assert len(calls) == asked
