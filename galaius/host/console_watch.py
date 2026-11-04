"""Windows: a console window shown by a program of the PC service's own process tree is hidden the
moment it appears, and named.

Every spawn galaius makes hides its child's console (`galaius.host.windowless`), yet a program it
starts can still open one of its own (a detached helper of an agent CLI, a script's `Start-Process`,
`cmd /c start`): a black window on the person's desktop, with nothing saying where it came from
(owner 2026-11-03: « A user's PC still has the cmd popping up without the user's consent »). The
service's supervisor watches every window shown on its desktop through an out-of-context WinEvent
hook (no polling, nothing injected into other programs); a console window whose process descends
from the supervisor is hidden and recorded (`ShownConsole`: the program and its parents, never its
command line) in `console-windows.jsonl`, which the PC's next beat tells its server.

Where Windows Terminal is the default terminal (Windows 11), a new console opens as a Terminal window
owned by the terminal's process, outside the tree: its pseudo-console window is then matched to the
tree's process attached to it (`attached`: a helper attaches to each one's console in turn), and the
Terminal window owning it is hidden. A console the person opened is never one of them."""

import ctypes
import logging
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from galaius.host.paths import UserPaths

logger = logging.getLogger(__name__)

#: A classic console host's window (its process is the console program's own `conhost.exe` or itself).
CLASSIC = "ConsoleWindowClass"
#: The window a pseudo-console (Windows Terminal) gives its program, owned by the Terminal window shown.
PSEUDO = "PseudoConsoleWindow"
#: Run by `attached` with pids: each one's console window, as « pid window » lines (none: no console).
ATTACHED = ("import ctypes, sys\nk = ctypes.windll.kernel32\nk.GetConsoleWindow.restype = ctypes.c_void_p\nk.FreeConsole()\n"
            "for pid in sys.argv[1:]:\n    if k.AttachConsole(int(pid)):\n        print(pid, k.GetConsoleWindow() or 0, flush=True)\n        k.FreeConsole()\n")


class ShownConsole(BaseModel):
    """One console window a program of the service's tree showed (and that was hidden)."""

    model_config = ConfigDict(frozen=True)
    at: datetime
    program: str
    #: Its ancestors' programs, nearest first, down to the service's supervisor.
    parents: tuple[str, ...]


class ConsoleWatch(BaseModel):
    """The watch of one supervisor (`root`): what it hid goes to `path`, the newest `kept` lines."""

    model_config = ConfigDict(frozen=True)
    root: int
    path: Path
    kept: ClassVar[int] = 20

    @staticmethod
    def default_path() -> Path:
        return UserPaths.config() / "console-windows.jsonl"

    def mine(self, lineage: tuple[tuple[int, str], ...], members: frozenset[int] = frozenset()) -> ShownConsole | None:
        """The window's process and its ancestors (`process_lineage`): ours when the supervisor is one of
        them, or one is in the supervisor's job (`members`: what a program left behind when its parent
        exited stays there). The supervisor itself shows none: it runs with its console hidden."""
        pids = [pid for pid, _program in lineage]
        if not lineage or (self.root not in pids[1:] and not members & {pid for pid in pids if pid != self.root}):
            return None
        below = lineage[:pids.index(self.root, 1)] if self.root in pids[1:] else lineage
        return ShownConsole(at=datetime.now(UTC), program=below[0][1], parents=tuple(program for _pid, program in below[1:9]))

    def attached(self, window: int, below: dict[int, tuple[int, str]]) -> ShownConsole | None:
        """The program of the tree (`below`: `process_descendants`) whose console is `window`, a pseudo-console window."""
        import subprocess

        from galaius.host.windowless import console_python

        pids = [str(pid) for pid, (_parent, program) in below.items() if program.lower() not in {"conhost.exe", "openconsole.exe"}]
        if not pids:
            return None
        try:
            answered = subprocess.run([console_python(), "-I", "-c", ATTACHED, *pids], capture_output=True, text=True, timeout=10,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in answered.splitlines():
            pid, _space, shown = line.partition(" ")
            if shown.isdigit() and int(shown) == window and int(pid) in below:
                chain, current = [], below[int(pid)][0]
                while current in below and len(chain) < 16:
                    chain.append(below[current][1])
                    current = below[current][0]
                return ShownConsole(at=datetime.now(UTC), program=below[int(pid)][1], parents=tuple(chain))
        return None

    def record(self, shown: ShownConsole) -> None:
        lines = [line for line in self._lines() if line][-(self.kept - 1):] + [shown.model_dump_json()]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.warning("hid a console window shown by %s (started by %s)", shown.program, " < ".join(shown.parents) or "the service")

    def _lines(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    @classmethod
    def recent(cls, path: Path | None = None) -> list[ShownConsole]:
        """What the watch hid lately, oldest first (an unreadable line skipped)."""
        found = []
        for line in cls(root=0, path=path or cls.default_path())._lines():
            try:
                found.append(ShownConsole.model_validate_json(line))
            except ValidationError:
                continue
        return found

    def start(self, job=None) -> threading.Thread | None:
        """The hook's thread (it runs while this process does), `job` the Windows job holding the
        supervisor's workers (`galaius.upgrade.handoff.Children`); None off Windows."""
        if sys.platform != "win32":
            return None
        thread = threading.Thread(target=self._watch, args=(job,), name="console-watch", daemon=True)
        thread.start()
        return thread

    @staticmethod
    def _in_job(job) -> frozenset[int]:
        if job is None:
            return frozenset()
        import win32job
        try:
            return frozenset(win32job.QueryInformationJobObject(job, win32job.JobObjectBasicProcessIdList))
        except Exception:  # pywintypes.error: the job is closing with its supervisor
            return frozenset()

    def _watch(self, job) -> None:
        from ctypes import wintypes

        from galaius.host.processes import process_descendants, process_lineage, process_programs

        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
        user32.SetWinEventHook.restype = wintypes.HANDLE
        user32.SetWinEventHook.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, callback_type, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.GetWindow.restype = wintypes.HWND
        user32.GetWindow.argtypes = (wintypes.HWND, ctypes.c_uint)

        def shown(_hook, _event, window, object_id, child_id, _thread, _time) -> None:
            if not window or object_id != 0 or child_id != 0:  # OBJID_WINDOW, the window itself
                return
            try:
                name = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(window, name, 64)
                if name.value == CLASSIC:
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
                    if (console := self.mine(process_lineage(pid.value), self._in_job(job))) is not None:
                        user32.ShowWindow(window, 0)  # SW_HIDE
                        self.record(console)
                elif name.value == PSEUDO and (console := self.attached(window, {**process_programs(self._in_job(job)), **process_descendants(self.root)})) is not None:
                    user32.ShowWindow(user32.GetWindow(window, 4) or window, 0)  # GW_OWNER: the Terminal window showing it
                    self.record(console)
            except Exception:  # a callback that raises is only printed by ctypes: say it in the service log
                logger.warning("the console window watch failed on one window", exc_info=True)

        callback = callback_type(shown)
        # EVENT_OBJECT_SHOW, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        if not user32.SetWinEventHook(0x8002, 0x8002, None, callback, 0, 0, 0x0000 | 0x0002):
            logger.warning("no console window watch: Windows refused the window hook")
            return
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
