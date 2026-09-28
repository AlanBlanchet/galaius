"""The portable directory handle: which entries count as links, which names are one entry."""

import stat
from types import SimpleNamespace

import pytest

from interact.pinned_directory import PathDirectory, PinnedDirectory


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
