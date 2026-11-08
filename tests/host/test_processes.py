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


GONE = 2**22 + 7   # above Linux's and macOS's pid ceiling: no process holds it


@pytest.mark.parametrize(("call", "target", "outcome"), [
    pytest.param("kill", "started", "sent", id="a-process-it-started"),
    pytest.param("killpg", "started", "sent", id="the-group-a-started-process-leads"),
    pytest.param("kill", "self", "sent", id="itself"),
    pytest.param("kill", 1, "refused", id="pid-1"),
    pytest.param("killpg", 1, "refused", id="group-1-every-process"),
    pytest.param("kill", -1, "refused", id="kill-minus-1"),
    pytest.param("kill", 0, "refused", id="kill-0-its-own-group"),
    pytest.param("killpg", "own-group", "refused", id="its-own-group"),
    pytest.param("kill", "parent", "refused", id="a-live-process-it-did-not-start",
                 marks=pytest.mark.skipif(not sys.platform.startswith("linux"), reason="lineage read from /proc")),
    pytest.param("kill", GONE, "gone", id="a-pid-no-process-holds",
                 marks=pytest.mark.skipif(not sys.platform.startswith("linux"), reason="lineage read from /proc")),
])
def test_a_test_signals_only_what_it_started(call, target, outcome):
    """`tests/support/signals.py`: the guard every test runs under (`tests/conftest.py`). Its kill / killpg here are
    recorders: nothing is ever sent."""
    from tests.support.signals import SignalGuard, alive_pid

    sent: list[tuple[str, int, int]] = []

    def kill(pid: int, sig: int) -> None:
        if sig == 0 and pid == GONE:
            raise ProcessLookupError
        sent.append(("kill", pid, sig))

    guard = SignalGuard(kill, lambda group, sig: sent.append(("killpg", group, sig)))
    pid = {"started": alive_pid(), "self": os.getpid(), "own-group": os.getpgrp(), "parent": os.getppid()}.get(target, target)
    raised = {"sent": None, "refused": PermissionError, "gone": ProcessLookupError}[outcome]
    if raised is None:
        getattr(guard, call)(pid, 15)
    else:
        with pytest.raises(raised):
            getattr(guard, call)(pid, 15)
    assert [entry for entry in sent if entry[2] != 0] == ([(call, pid, 15)] if outcome == "sent" else [])
    assert bool(guard.refused) is (outcome == "refused")
