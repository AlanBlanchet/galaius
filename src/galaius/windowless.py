"""Windows: a galaius process with no console window of its own (the PC program, its agent
dispatcher and the detached sign-in run under pythonw or without a console) gives every program it
starts none either. Without it each console child (an agent CLI turn, a script step, `taskkill`,
`schtasks`, git) opens a black window on the person's desktop.

Installed once, when the `galaius` package is imported, so every entry point (the `galaius`
program, `python -m galaius`, `python -m galaius.agents.agent_queue`, the hooks) has it before
it starts anything; at the one place every spawn goes through, `subprocess.Popen` (asyncio's and
the libraries' spawns included). A Python child is started with the console interpreter
(`console_python`), never `pythonw`: its hidden console is then shared by everything IT starts (a
script step's own git, a worker's agent CLI), which a windowless `pythonw` child would show again.
Standard library only: importing galaius stays cheap."""

import ctypes
import functools
import subprocess
import sys
from pathlib import Path


def hide_child_consoles() -> None:
    """In a process with no console window, every `subprocess.Popen` gets `CREATE_NO_WINDOW`
    unless its caller asks for a console of its own or a detached process. A program run from a
    terminal is left alone: its children share that terminal."""
    if sys.platform != "win32" or getattr(subprocess.Popen.__init__, "hides_consoles", False) or ctypes.windll.kernel32.GetConsoleWindow():
        return
    spawn = subprocess.Popen.__init__
    keeps_own = subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS

    @functools.wraps(spawn)
    def windowless(self, *args, creationflags: int = 0, **options) -> None:
        spawn(self, *args, creationflags=creationflags if creationflags & keeps_own else creationflags | subprocess.CREATE_NO_WINDOW, **options)

    windowless.hides_consoles = True
    subprocess.Popen.__init__ = windowless


def console_python() -> str:
    """This environment's console interpreter, for a Python child: `python.exe` beside a running
    `pythonw.exe` (its window hidden by `hide_child_consoles`), else this interpreter."""
    beside = Path(sys.executable).with_name("python.exe")
    return str(beside) if Path(sys.executable).name.lower() == "pythonw.exe" and beside.is_file() else sys.executable
