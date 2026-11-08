"""POSIX: ending a run's process tree signals only that tree (`processes.end_process_tree`). Every signal is recorded
here, never sent: 2026-11-08 a run record holding pid 1 made it `killpg(1)` (`kill(-1)`), every process of the user,
and ended his desktop session twice."""

import os
import sys

import pytest

if sys.platform == "win32":
    pytest.skip("POSIX process groups", allow_module_level=True)

from galaius.host import processes  # noqa: E402

OURS = 4242


@pytest.fixture
def signalled(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Each signal asked: ("group", pgid) or ("pid", pid). Process groups: pid 10 leads its own, pid 11 sits in 10's,
    pid 12 in this process's (`OURS`), pid 1 in group 1."""
    sent: list[tuple[str, int]] = []
    groups = {1: 1, 10: 10, 11: 10, 12: OURS, OURS: OURS}
    monkeypatch.setattr(os, "getpgrp", lambda: OURS)
    monkeypatch.setattr(os, "getpgid", lambda pid: groups[pid])
    monkeypatch.setattr(os, "killpg", lambda group, sig: sent.append(("group", group)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(("pid", pid)))
    return sent


@pytest.mark.parametrize(("pid", "expected", "ended"), [
    pytest.param(10, [("group", 10)], True, id="a-group-leader-its-whole-tree"),
    pytest.param(11, [("pid", 11)], True, id="a-pid-leading-no-group-alone"),
    pytest.param(12, [("pid", 12)], True, id="one-in-this-process-group-alone-never-the-group"),
    pytest.param(OURS, [("pid", OURS)], True, id="this-process-group-leader-alone"),
    pytest.param(1, [], False, id="pid-1-never-kill-minus-1"),
    pytest.param(0, [], False, id="pid-0-never-its-own-group"),
    pytest.param(-1, [], False, id="negative-never-every-process"),
])
def test_ending_a_tree_signals_that_tree_only(signalled, pid, expected, ended):
    assert processes.end_process_tree(pid) is ended
    assert signalled == expected
