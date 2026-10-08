"""Windows: from a galaius process with no console window, the programs it starts open none
either (`galaius.windowless`); a caller asking for a console of its own keeps it; a terminal is left alone."""

import asyncio
import ctypes
import subprocess
import sys
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("Windows console windows", allow_module_level=True)

from galaius.machine_service import WindowsMachineService  # noqa: E402
from galaius.windowless import hide_child_consoles  # noqa: E402

PROBE = "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The creationflags each spawn really asks for, in a process with no console window."""
    real, flags = subprocess.Popen.__init__, []

    def spy(self, *args, creationflags: int = 0, **options) -> None:
        flags.append(creationflags)
        real(self, *args, creationflags=creationflags & ~subprocess.CREATE_NEW_CONSOLE, **options)  # never open one in CI

    monkeypatch.setattr(subprocess.Popen, "__init__", spy)                        # restored after the test
    monkeypatch.setattr(ctypes.windll.kernel32, "GetConsoleWindow", lambda: 0)   # as under pythonw
    hide_child_consoles()
    hide_child_consoles()  # once only
    return flags


@pytest.mark.parametrize("asked, sent", [
    (0, subprocess.CREATE_NO_WINDOW),
    (subprocess.CREATE_NEW_PROCESS_GROUP, subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW),
    (subprocess.CREATE_NEW_CONSOLE, subprocess.CREATE_NEW_CONSOLE),  # asked for its own console: kept
])
def test_children_of_a_windowless_process_get_no_console_window(seen: list[int], asked: int, sent: int) -> None:
    window = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, check=True, creationflags=asked).stdout.strip()
    assert seen == [sent]
    if sent & subprocess.CREATE_NO_WINDOW:
        assert window == "0"  # the child's console has no window


def test_asyncio_spawns_too(seen: list[int]) -> None:
    async def run() -> bytes:
        child = await asyncio.create_subprocess_exec(sys.executable, "-c", PROBE, stdout=subprocess.PIPE)
        return (await child.communicate())[0]

    assert asyncio.run(run()).strip() == b"0" and seen == [subprocess.CREATE_NO_WINDOW]


def test_a_program_run_from_a_terminal_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess.Popen, "__init__", subprocess.Popen.__init__)
    monkeypatch.setattr(ctypes.windll.kernel32, "GetConsoleWindow", lambda: 1234)
    before = subprocess.Popen.__init__
    hide_child_consoles()
    assert subprocess.Popen.__init__ is before


def test_every_entry_point_has_it_from_the_package_import(tmp_path: Path) -> None:
    """pythonw (no console window), any entry point: `import galaius` alone installs it."""
    pythonw, said = Path(sys.executable).with_name("pythonw.exe"), tmp_path / "said.txt"
    subprocess.run([str(pythonw), "-c", "import sys, subprocess, galaius.agents.agent_queue; "
                    "open(sys.argv[1], 'w').write(str(getattr(subprocess.Popen.__init__, 'hides_consoles', False)))", str(said)], check=True, timeout=120)
    assert said.read_text() == "True"


def test_python_children_get_the_console_interpreter_under_pythonw(tmp_path: Path) -> None:
    """Under pythonw a Python child is started as python.exe, whose hidden console what it starts shares."""
    pythonw, said = Path(sys.executable).with_name("pythonw.exe"), tmp_path / "said.txt"
    subprocess.run([str(pythonw), "-c", "import sys; from galaius.windowless import console_python, windowless_process; "
                    "open(sys.argv[1], 'w').write(console_python() + '|' + str(windowless_process()))", str(said)], check=True, timeout=120)
    interpreter, windowless = said.read_text().split("|")
    assert Path(interpreter).name.lower() == "python.exe" and windowless == "True"


def test_the_service_logs_from_a_hidden_console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker in a hidden console has a stdout nobody sees: the service still writes its log."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("galaius.machine_service.windowless_process", lambda: True)
    monkeypatch.setattr(WindowsMachineService, "held_children", staticmethod(lambda: type("Job", (), {"Close": lambda self: None})()))
    monkeypatch.setattr("galaius.machine_service.MachineService.run", lambda self: print("connecting"))
    stdout, stderr = sys.stdout, sys.stderr
    try:
        WindowsMachineService().run()
    finally:
        if sys.stdout is not stdout:
            sys.stdout.close()
        sys.stdout, sys.stderr = stdout, stderr
    assert "connecting" in WindowsMachineService.log_path().read_text(encoding="utf-8")
