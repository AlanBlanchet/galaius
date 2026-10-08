"""Windows: from a program with no console window, the programs it starts open none either
(`hide_child_consoles`); a caller asking for a console of its own keeps it."""

import ctypes
import subprocess
import sys

import pytest

from galaius.processes import hide_child_consoles

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows console windows")

PROBE = "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"


@pytest.mark.parametrize("asked, sent", [
    (0, subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
    (subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0, (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if sys.platform == "win32" else 0),
    (subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0, subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0),  # asked for its own console: kept
])
def test_children_of_a_windowless_program_get_no_console_window(monkeypatch: pytest.MonkeyPatch, asked: int, sent: int) -> None:
    real = subprocess.Popen.__init__
    seen: list[int] = []

    def spy(self, *args, creationflags: int = 0, **options) -> None:
        seen.append(creationflags)
        real(self, *args, creationflags=creationflags & ~subprocess.CREATE_NEW_CONSOLE, **options)  # never open one in CI

    monkeypatch.setattr(subprocess.Popen, "__init__", spy)                        # restored after the test
    monkeypatch.setattr(ctypes.windll.kernel32, "GetConsoleWindow", lambda: 0)   # as under pythonw
    hide_child_consoles()
    hide_child_consoles()  # once only
    window = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, check=True, creationflags=asked).stdout.strip()
    assert seen == [sent]
    if sent & subprocess.CREATE_NO_WINDOW:
        assert window == "0"  # the child's console has no window


def test_a_program_run_from_a_terminal_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess.Popen, "__init__", subprocess.Popen.__init__)
    monkeypatch.setattr(ctypes.windll.kernel32, "GetConsoleWindow", lambda: 1234)
    before = subprocess.Popen.__init__
    hide_child_consoles()
    assert subprocess.Popen.__init__ is before
