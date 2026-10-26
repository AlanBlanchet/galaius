"""The badge as a session handles it: up before the first frame, watched with every frame, down
after the last (`ScreenMarker`).

Nothing may watch this screen invisibly: `show` succeeds only once the badge (`screen_badge`, an
always-on-top window) SAID it is on screen, and a badge that disappears ends the session at the next
frame (`ended_itself`). Its Stop and its hotkey exit with `STOPPED`, which switches sharing off
here (`ScreenSwitch`) — the server is never asked, and cannot answer for it.
"""

import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import ClassVar

from galaius.desktop.screen_badge import STOPPED
from galaius.desktop.screen_switch import ScreenSwitch

logger = logging.getLogger("galaius")

BADGE = Path(__file__).with_name("screen_badge.py")


class ScreenMarker:
    """The badge's process, started with the first Python here that can draw it."""

    #: How long a badge may take to say it is on screen before the next interpreter is tried.
    UP_SECONDS: ClassVar[float] = 8.0

    def __init__(self, switch: ScreenSwitch | None = None, interpreters: tuple[str, ...] | None = None) -> None:
        self.switch = switch or ScreenSwitch()
        self.interpreters = interpreters if interpreters is not None else self.candidates()
        self.process: subprocess.Popen | None = None

    @staticmethod
    def candidates() -> tuple[str, ...]:
        """The runtime's own Python first (its windowless twin on Windows: no console flashes up),
        then the system's: on Linux uv's standalone Tk aborts in XCB where the system Tk draws."""
        own = Path(sys.executable)
        if sys.platform == "win32" and own.with_name("pythonw.exe").is_file():
            own = own.with_name("pythonw.exe")
        found = [str(own)]
        if sys.platform != "win32":
            found += [path for path in dict.fromkeys(("/usr/bin/python3", shutil.which("python3") or "")) if path and Path(path).is_file()]
        return tuple(dict.fromkeys(found))

    @staticmethod
    def environment(interpreter: str) -> dict[str, str]:
        """This process's environment, with Tcl/Tk pointed at the RUNTIME's own base install when
        the badge runs on it: a venv over a standalone CPython looks for init.tcl beside the venv."""
        env = dict(os.environ)
        if Path(interpreter).parent != Path(sys.executable).parent:
            return env
        base = Path(sys.base_prefix)
        for variable, pattern, marker in (("TCL_LIBRARY", "tcl8.*", "init.tcl"), ("TK_LIBRARY", "tk8.*", "tk.tcl")):
            found = [folder for sub in ("lib", "tcl") for folder in sorted((base / sub).glob(pattern)) if (folder / marker).is_file()]
            if found and variable not in env:
                env[variable] = str(found[0])
        return env

    @property
    def showing(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def show(self, viewer: str = "") -> bool:
        """Put the badge on this desktop; True once it said it is on screen."""
        if self.showing:
            return True
        for interpreter in self.interpreters:
            try:
                process = subprocess.Popen([interpreter, "-I", str(BADGE), viewer[:80]], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, env=self.environment(interpreter), text=True)
            except OSError:
                continue
            if self._up(process):
                self.process = process
                return True
            self._end(process)
        logger.warning("screen: no Python here could draw the badge (%s), so nothing is shown and nothing streams", ", ".join(self.interpreters))
        return False

    def _up(self, process: subprocess.Popen) -> bool:
        said: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: said.put(process.stdout.readline() if process.stdout else ""), daemon=True).start()
        try:
            return said.get(timeout=self.UP_SECONDS).strip() == "up"
        except queue.Empty:
            return False

    @staticmethod
    def _end(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    def stop(self) -> None:
        """Take the badge down (the session ended). Ending one view leaves the switch as it is."""
        if self.process is not None:
            self._end(self.process)
        self.process = None

    def ended_itself(self) -> bool:
        """True once the badge is gone although the session still runs; its Stop or hotkey also
        switched sharing off here."""
        if self.process is None or self.process.poll() is None:
            return False
        if self.process.returncode == STOPPED and self.switch.live():
            self.switch.turn_off("Stop was pressed on this computer's screen badge.", "stopped_here")
        return True
