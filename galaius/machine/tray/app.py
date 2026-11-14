"""The tray icon process (`python -m galaius.machine.tray`): one per signed-in desktop session (`tray.lock`), started by
the machine service each time it starts (`TrayLauncher.ensure`), so an existing install gets it from
its next automatic update. It reads what the connection writes (`galaius.machine.presence`) and acts
only on this computer: open its page, pause or resume the connection, start or stop the service.

Its own process, apart from the connection: on Windows the connection's job bars its programs from
other programs' windows (`WindowsMachineService.ui_limits`), the taskbar's included; anywhere, an icon
that outlives a stopped or crashed service still says « Éteint » and offers to start it."""

import logging
import subprocess
import sys
import threading
import time
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from galaius.machine.presence import PresenceFiles
from galaius.machine.tray.launch import TrayLauncher
from galaius.machine.tray.view import TrayAction, TrayBackend, TrayView, TrayWords

if sys.platform == "win32":
    from galaius.machine.tray.windows import WindowsTray as PlatformTray
elif sys.platform == "darwin":
    from galaius.machine.tray.macos import MacTray as PlatformTray
else:
    from galaius.machine.tray.linux import StatusNotifierTray as PlatformTray

logger = logging.getLogger(__name__)


class TrayApp(BaseModel):
    """What the icon shows and does, whatever system draws it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    files: PresenceFiles
    words: TrayWords
    backend: TrayBackend
    #: The presence files are read this often; the active runtime this often (`moved_on`).
    look_seconds: ClassVar[float] = 1.0
    runtime_seconds: ClassVar[float] = 5.0
    shown: TrayView | None = None
    leaving: bool = False
    _frame: int = 0
    _looked: float = float("-inf")
    _runtime_looked: float = float("-inf")
    _last: tuple = ()

    def tick(self) -> TrayView | None:
        """The view to show now when it changed (a new state, or the next frame of the working pulse); an error
        is logged and the icon keeps what it showed: it never ends on one."""
        try:
            return self._tick()
        except Exception:  # noqa: BLE001 - whatever failed this look, the next one tries again
            logger.exception("tray look failed")
            return None

    def _tick(self) -> TrayView | None:
        now = time.monotonic()
        if now - self._runtime_looked >= self.runtime_seconds:
            self._runtime_looked = now
            if TrayLauncher.moved_on():
                logger.info("a newer release is active; the icon starts again from it")
                TrayLauncher.start()
                self.leaving = True
                return None
        if now - self._looked >= self.look_seconds:
            self._looked = now
            self._last = (self.files.read(), self.files.alive())
        self._frame += 1
        view = TrayView.of(*self._last, self.words, frame=self._frame)
        if view == self.shown:
            return None
        self.shown = view
        return view

    def act(self, action: TrayAction) -> None:
        """A clicked row, done off the UI thread (starting or stopping the service takes seconds)."""
        threading.Thread(target=self._do, args=(action,), name=f"tray-{action}", daemon=True).start()

    def _do(self, action: TrayAction) -> None:
        try:
            match action:
                case "open":
                    presence = self.files.read()
                    # A refused computer's page is gone: the app's home, where it is added again.
                    self.backend.open(presence.page.split("#")[0] if presence.link == "refused" else presence.page)
                case "pause" | "resume":
                    self.files.pause(action == "pause")
                case "start":
                    self.files.pause(False)
                    TrayLauncher.service("start")
                case "quit":
                    self.files.pause(False)
                    try:
                        TrayLauncher.service("stop")
                    finally:
                        self.leaving = True
            self._looked = float("-inf")  # the next tick reads what it changed
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            logger.warning("tray %s failed: %s", action, error)

    @classmethod
    def main(cls) -> int:
        """The icon until quit: 0, also when another icon already runs in this session."""
        files = PresenceFiles.default()
        held = TrayLauncher.hold()
        if held is None:
            return 0
        app = cls(files=files, words=TrayWords.current(), backend=PlatformTray())
        app.backend.run(app)
        return 0
