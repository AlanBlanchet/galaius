"""A console window shown by a program of the PC service's tree is hidden at once and named (`galaius.host.console_watch`).

Owner 2026-11-03: « A user's PC still has the cmd popping up without the user's consent which is actually horrific »."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from galaius.host.console_watch import ConsoleWatch, ShownConsole

ROOT = 4100


@pytest.mark.parametrize(("lineage", "program", "parents"), [
    (((7, "cmd.exe"), (6, "node.exe"), (5, "python.exe"), (ROOT, "python.exe"), (3, "pythonw.exe")), "cmd.exe", ("node.exe", "python.exe")),
    (((7, "conhost.exe"), (ROOT, "python.exe")), "conhost.exe", ()),
    (((7, "cmd.exe"), (2, "explorer.exe")), None, None),       # the person's own terminal: never touched
    (((ROOT, "python.exe"), (3, "pythonw.exe")), None, None),  # the supervisor's own (hidden) console
    ((), None, None),                                          # gone before it was read
    (((9, "cmd.exe"), (8, "explorer.exe")), "cmd.exe", ("explorer.exe",)),  # in the job: its parent exited, the pid was taken again
])
def test_only_a_console_of_the_services_own_tree_is_hidden_and_named(tmp_path: Path, lineage, program, parents) -> None:
    shown = ConsoleWatch(root=ROOT, path=tmp_path / "c.jsonl").mine(lineage, frozenset({9, 11}))
    assert (shown.program, shown.parents) == (program, parents) if program else shown is None


def test_what_was_hidden_is_kept_newest_last_and_read_back(tmp_path: Path) -> None:
    watch = ConsoleWatch(root=ROOT, path=tmp_path / "c.jsonl")
    for number in range(ConsoleWatch.kept + 3):
        watch.record(watch.mine(((number + 10, f"p{number}.exe"), (ROOT, "python.exe"))))
    (tmp_path / "c.jsonl").write_text((tmp_path / "c.jsonl").read_text() + "cut sh\n")
    recent = ConsoleWatch.recent(tmp_path / "c.jsonl")
    assert len(recent) == ConsoleWatch.kept and recent[-1].program == f"p{ConsoleWatch.kept + 2}.exe"


#: Each way a console window appears: (how it is started, whether it is the service tree's).
STARTS = {
    "child_new_console": (["cmd.exe", "/c", "ping -n 8 127.0.0.1 >nul"], "CREATE_NEW_CONSOLE", True),
    "grandchild_of_a_consoleless_child": (["cmd.exe", "/c", "start", "/b", "cmd.exe", "/c", "ping -n 8 127.0.0.1"], "DETACHED_PROCESS", True),
    "outside_the_tree": (["powershell", "-NoProfile", "-Command", "Invoke-CimMethod Win32_Process -MethodName Create -Arguments @{CommandLine='cmd.exe /c ping -n 8 127.0.0.1'} | Out-Null"], "CREATE_NO_WINDOW", False),
}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console windows")
@pytest.mark.parametrize("how", STARTS)
def test_a_console_window_of_the_tree_is_hidden_and_named_and_no_other(tmp_path: Path, how: str) -> None:
    """Classic console host and Windows Terminal alike (a GitHub Windows runner has Terminal as its default)."""
    import os
    command, flag, ours = STARTS[how]
    from galaius.upgrade.handoff import Children
    watch, children = ConsoleWatch(root=os.getpid(), path=tmp_path / "c.jsonl"), Children()
    watch.start(children.job)
    time.sleep(0.5)  # the hook is set
    before = _visible_consoles()
    child = subprocess.Popen(command, creationflags=getattr(subprocess, flag))
    children.hold(child)  # as the supervisor holds its worker: what it leaves behind stays in the job
    try:
        time.sleep(4)
        appeared = _visible_consoles() - before
        recorded = ConsoleWatch.recent(watch.path)
        if ours:
            assert recorded and recorded[-1].program.lower() in {"cmd.exe", "conhost.exe", "ping.exe"}, recorded
            assert appeared == set(), f"still visible: {appeared}"
        else:
            assert recorded == [] and appeared, (recorded, appeared)
    finally:
        child.kill()
        subprocess.run(["taskkill", "/F", "/IM", "ping.exe"], capture_output=True)


def _visible_consoles() -> set[int]:
    """Visible top-level windows of a console: classic host or Windows Terminal."""
    import ctypes
    from ctypes import wintypes
    found: set[int] = set()
    user32 = ctypes.windll.user32
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)

    def each(window, _param) -> bool:
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(window, name, 64)
        if user32.IsWindowVisible(window) and name.value in {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"}:
            found.add(window)
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(each), 0)
    return found


def test_every_beat_tells_the_server_what_the_service_hid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ConsoleWatch, "default_path", staticmethod(lambda: tmp_path / "c.jsonl"))
    ConsoleWatch(root=ROOT, path=tmp_path / "c.jsonl").record(ShownConsole.model_validate({"at": "2026-11-04T10:00:00Z", "program": "cmd.exe", "parents": ["node.exe"]}))
    from galaius.machine.connection import MachineConfig, MachineRunner
    from uuid import uuid4
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = MachineRunner(tmp_path / "config" / "galaius" / "machine.json")
    connected = MachineConfig(server_url="http://127.0.0.1:8817", workspace_id=uuid4(), machine_id=uuid4(), token="t" * 40, permission_ceiling="read_only", working_directory=tmp_path)
    runner.save(connected)
    assert runner._beat(connected)["console_windows"] == [{"at": "2026-11-04T10:00:00Z", "program": "cmd.exe", "parents": ["node.exe"]}]
