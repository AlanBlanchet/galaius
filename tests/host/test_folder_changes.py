"""`FolderChanges`: what moved in a folder, as the kernel says, so a poller reads only that."""

import shutil
import sys

import pytest

from galaius.host.folder_changes import FolderChanges

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux's")


def test_writes_appends_renames_and_removals_are_named_and_nothing_else(tmp_path):
    changes = FolderChanges.watch((tmp_path,))
    assert changes is not None and changes.take() == set()
    (tmp_path / "a.json").write_text("1")
    assert changes.take() == {tmp_path / "a.json"}
    assert changes.take() == set()
    with (tmp_path / "a.json").open("a") as stream:   # an append to a file still open is told before its close
        stream.write("2")
        stream.flush()
        assert changes.take() == {tmp_path / "a.json"}
    changes.take()
    (tmp_path / "a.json").rename(tmp_path / "b.json")
    assert changes.take() == {tmp_path / "a.json", tmp_path / "b.json"}
    (tmp_path / "b.json").unlink()
    assert changes.take() == {tmp_path / "b.json"}


def test_a_watched_folder_going_away_ends_the_watch_and_a_missing_one_is_never_watched(tmp_path):
    folder = tmp_path / "runs"
    folder.mkdir()
    changes = FolderChanges.watch((folder,))
    assert changes is not None
    shutil.rmtree(folder)
    assert changes.take() is None and not changes.usable
    assert FolderChanges.watch((folder,)) is None


def test_the_subfolders_of_a_parent_are_watched_those_made_later_too(tmp_path):
    (tmp_path / "old").mkdir()
    changes = FolderChanges.watch(parents=(tmp_path,))
    assert changes is not None
    (tmp_path / "old" / "a.jsonl").write_text("1")
    assert changes.take() == {tmp_path / "old" / "a.jsonl"}
    (tmp_path / "new").mkdir()
    assert changes.take() is None   # what it held before this watch of it: anything may have moved
    (tmp_path / "new" / "b.jsonl").write_text("1")
    assert changes.take() == {tmp_path / "new" / "b.jsonl"}
    shutil.rmtree(tmp_path / "new")
    changed = changes.take()
    assert changed is not None and tmp_path / "new" in changed and changes.usable


def test_a_queue_that_overflowed_says_anything_may_have_moved_once(tmp_path):
    from pathlib import Path

    limit = int(Path("/proc/sys/fs/inotify/max_queued_events").read_text())
    if limit > 40_000:
        pytest.skip("this system's event queue is too long to overflow in a test")
    changes = FolderChanges.watch((tmp_path,))
    assert changes is not None
    for number in range(limit // 2 + 10):   # each file: a creation and a close, at least
        (tmp_path / f"{number}.json").write_text("1")
    assert changes.take() is None
    assert changes.take() == set() and changes.usable
