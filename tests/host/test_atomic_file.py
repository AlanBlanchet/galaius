import os

import pytest

from galaius.host import atomic_file


@pytest.mark.parametrize(("refusals", "replaced"), [
    ([5, 32], True),        # held open by a reader for an instant (2026-11-01 CI: two dispatch threads, one queue file)
    ([5] * 100, False),     # still held past the 2 s bound: the refusal is raised
    ([None], False),        # a POSIX EACCES, or another Windows refusal: raised at once
], ids=["held-for-an-instant", "held-past-the-bound", "not-a-held-file"])
def test_a_rename_over_a_file_held_open_is_tried_again_for_a_bounded_time(tmp_path, monkeypatch, refusals, replaced) -> None:
    source, target = tmp_path / "new", tmp_path / "state.json"
    source.write_text("new")
    target.write_text("old")
    real, left, clock = os.replace, list(refusals), [0.0]

    def replace(a, b):
        if left:
            error = PermissionError(13, "Access is denied")
            error.winerror = left.pop(0)
            raise error
        real(a, b)

    monkeypatch.setattr(atomic_file.os, "replace", replace)
    monkeypatch.setattr(atomic_file.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(atomic_file.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    if replaced:
        atomic_file.replace_held_open(source, target)
    else:
        with pytest.raises(PermissionError):
            atomic_file.replace_held_open(source, target)
    assert target.read_text() == ("new" if replaced else "old")
    assert clock[0] <= 2.05, "never past its bound"
