"""Starting the tray icon process (`python -m galaius.machine.tray`, `galaius.machine.tray.app`) once per desktop session,
from the active runtime, apart from whatever starts it."""

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import ClassVar, Literal

from galaius.host.file_lock import FILE_LOCK
from galaius.host.private_files import PRIVATE_FILES
from galaius.machine.presence import PresenceFiles
from galaius.upgrade.store import Runtime, RuntimeStore

logger = logging.getLogger(__name__)


class TrayLauncher:
    """Starts the icon process once per session: `tray.lock` (beside the machine file) is held by the
    running icon; a start finding it held does nothing."""

    #: A newly started icon waits this long for the one it replaces (an older release's) to let go.
    handover_seconds: ClassVar[float] = 10.0

    @staticmethod
    def lock_path() -> Path:
        return PresenceFiles.default().folder / "tray.lock"

    @classmethod
    def _open(cls) -> int:
        PRIVATE_FILES.directory(cls.lock_path().parent)
        return os.open(cls.lock_path(), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)

    @classmethod
    def hold(cls) -> int | None:
        """The icon's lock, taken within `handover_seconds`, kept until the process ends; None: another icon runs."""
        descriptor = cls._open()
        deadline = time.monotonic() + cls.handover_seconds
        while not FILE_LOCK.try_acquire(descriptor):
            if time.monotonic() >= deadline:
                os.close(descriptor)
                return None
            time.sleep(0.2)
        return descriptor

    @classmethod
    def running(cls) -> bool:
        try:
            descriptor = cls._open()
        except OSError:
            return False
        try:
            if not FILE_LOCK.try_acquire(descriptor):
                return True
            FILE_LOCK.release(descriptor)
            return False
        finally:
            os.close(descriptor)

    @staticmethod
    def moved_on() -> bool:
        """This icon runs from an installed runtime the store no longer points at (an update took over)."""
        store, own = RuntimeStore.default(), Runtime.own()
        try:
            return store.root in own.path.parents and store.active().path != own.path
        except (OSError, ValueError):
            return False

    @staticmethod
    def command() -> list[str]:
        """The icon (`python -m galaius.machine.tray`) from the active runtime, windowless on Windows."""
        python = RuntimeStore.default().active().python
        if sys.platform == "win32" and (windowless := python.with_name("pythonw.exe")).is_file():
            python = windowless
        return [str(python), "-I", "-m", "galaius.machine.tray"]

    @staticmethod
    def service(action: Literal["start", "stop"]) -> None:
        """`galaius machine service <action>` from the active runtime, in a process of its own (the icon never
        loads the connection's code), waited for; RuntimeError naming what it said when it failed."""
        command = RuntimeStore.default().active().command(("machine", "service", action))
        done = subprocess.run(command, capture_output=True, text=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if done.returncode != 0:
            raise RuntimeError((done.stderr or done.stdout).strip() or f"galaius machine service {action} failed")

    @staticmethod
    def session() -> bool:
        """Whether this computer has a desktop session to show an icon in: Linux needs its session bus."""
        if sys.platform != "linux":
            return True
        return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")) or Path(f"/run/user/{os.getuid()}/bus").exists()

    @classmethod
    def start(cls) -> None:
        """The icon process, on its own: never a child the service's stop or job takes down with it (systemd scope
        on Linux, a breakaway on Windows where its job allows it, a session of its own elsewhere)."""
        command = cls.command()
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
            except OSError:  # a job that does not allow it: the icon then ends with the task, which starts it again
                subprocess.Popen(command, creationflags=flags, close_fds=True)
            return
        if sys.platform == "linux" and shutil.which("systemd-run") is not None:
            started = subprocess.run(["systemd-run", "--user", "--collect", "--quiet", "--unit=galaius-tray", "--", *command],
                                     capture_output=True, text=True, timeout=30)
            if started.returncode == 0:
                return
            logger.info("systemd-run refused the tray (%s); started as a process of its own", started.stderr.strip())
        subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)

    @classmethod
    def ensure(cls) -> None:
        """The icon running in this session; a failure is logged, never the service's."""
        try:
            if cls.session() and not cls.running():
                cls.start()
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            logger.warning("tray icon not started: %s", error)
