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
])
def test_only_a_console_of_the_services_own_tree_is_hidden_and_named(tmp_path: Path, lineage, program, parents) -> None:
    shown = ConsoleWatch(root=ROOT, path=tmp_path / "c.jsonl").mine(lineage)
    assert (shown.program, shown.parents) == (program, parents) if program else shown is None


def test_what_was_hidden_is_kept_newest_last_and_read_back(tmp_path: Path) -> None:
    watch = ConsoleWatch(root=ROOT, path=tmp_path / "c.jsonl")
    for number in range(ConsoleWatch.kept + 3):
        watch.record(watch.mine(((number + 10, f"p{number}.exe"), (ROOT, "python.exe"))))
    (tmp_path / "c.jsonl").write_text((tmp_path / "c.jsonl").read_text() + "cut sh\n")
    recent = ConsoleWatch.recent(tmp_path / "c.jsonl")
    assert len(recent) == ConsoleWatch.kept and recent[-1].program == f"p{ConsoleWatch.kept + 2}.exe"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console windows")
def test_a_new_console_a_child_opens_is_hidden_within_a_second(tmp_path: Path) -> None:
    import ctypes
    import os
    watch = ConsoleWatch(root=os.getpid(), path=tmp_path / "c.jsonl")
    watch.start()
    time.sleep(0.5)  # the hook is set
    child = subprocess.Popen(["cmd.exe", "/c", "ping -n 6 127.0.0.1 >nul"], creationflags=subprocess.CREATE_NEW_CONSOLE)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not ConsoleWatch.recent(watch.path):
            time.sleep(0.05)
        recent = ConsoleWatch.recent(watch.path)
        assert recent and recent[-1].program.lower() in {"cmd.exe", "conhost.exe", "ping.exe"}, recent
        visible = []
        callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(
            lambda window, _: visible.append(window) or True if ctypes.windll.user32.IsWindowVisible(window) and _class(window) == "ConsoleWindowClass" and _pid(window) == child.pid else True)
        ctypes.windll.user32.EnumWindows(callback, None)
        assert visible == [], "the child's console window is still visible"
    finally:
        child.kill()


def _class(window) -> str:
    import ctypes
    name = ctypes.create_unicode_buffer(64)
    ctypes.windll.user32.GetClassNameW(ctypes.c_void_p(window), name, 64)
    return name.value


def _pid(window) -> int:
    import ctypes
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(window), ctypes.byref(pid))
    return pid.value


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
