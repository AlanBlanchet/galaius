"""What this computer's connection says about itself to its tray icon (`galaius.machine.tray`), through
three files beside the machine file, so the icon needs no token, no network and no shared process:

- `machine-presence.json` (`Presence`): written by the one connection holding `machine-presence.lock`,
  each time what it says changes (never on a timer: a beat-long keep-alive would fsync every 3 s);
- `machine-presence.lock`: held by that connection for as long as its process lives. The icon reads
  it as "Galaius runs here" (`PresenceFiles.alive`): a lock the system frees when the process dies,
  so neither a crash, a sleep nor a clock change can leave a stale « connected »;
- `machine-paused`: present while the owner paused this computer from its icon; the connection
  closes and stays away until it is gone.
"""

import os
import time
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from galaius.host.file_lock import FILE_LOCK
from galaius.host.paths import UserPaths
from galaius.host.private_files import PRIVATE_FILES

#: What the connection says of its link (`Presence.link`).
Link = Literal["connected", "reconnecting", "paused", "refused"]
#: What the icon shows (`Presence.shown`): the link, plus « working » (connected with agents running) and
#: « off » (no connection process at all).
Shown = Literal["connected", "working", "reconnecting", "paused", "refused", "off"]


class Presence(BaseModel):
    """One state of this computer's connection, as its icon shows it."""

    model_config = ConfigDict(frozen=True)
    link: Link = "reconnecting"
    #: Agent runs alive on this computer now (every galaius run, from the web or an editor).
    runs: int = Field(default=0, ge=0)
    #: A newer release is installed and waits for those runs to end before it takes over.
    update_waiting: bool = False
    #: This computer's name, its page on the web app, and the release the connection runs.
    pc: str = ""
    page: str = ""
    version: str = ""

    def shown(self, alive: bool) -> Shown:
        """What the icon shows when the connection process is `alive` (or not): a refused computer stays
        « refused » after its connection stopped, since only a new sign-in brings it back."""
        if not alive:
            return "refused" if self.link == "refused" else "off"
        return "working" if self.link == "connected" and self.runs else self.link


class PresenceFiles(BaseModel):
    """The three files, in `folder` (the machine file's)."""

    model_config = ConfigDict(frozen=True)
    folder: Path

    @classmethod
    def default(cls) -> "PresenceFiles":
        return cls(folder=UserPaths.config())

    @property
    def path(self) -> Path:
        return self.folder / "machine-presence.json"

    @property
    def lock_path(self) -> Path:
        return self.folder / "machine-presence.lock"

    @property
    def pause_path(self) -> Path:
        return self.folder / "machine-paused"

    def write(self, presence: Presence) -> None:
        PRIVATE_FILES.write_text(self.path, presence.model_dump_json())

    def read(self) -> Presence:
        """The last state written; an absent, half-written or foreign file reads as a fresh one (reconnecting)."""
        try:
            return Presence.model_validate_json(PRIVATE_FILES.read_text(self.path))
        except (OSError, ValueError, ValidationError):
            return Presence()

    def _open_lock(self) -> int:
        PRIVATE_FILES.directory(self.folder)
        return os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)

    def hold(self) -> int | None:
        """The writer's lock if nobody holds it (None: another connection, an older release leaving, still does), kept
        until this process ends: the descriptor is never closed, the system frees it with the process. Never waits: a
        blocked thread would hold its process's exit until it got the lock."""
        descriptor = self._open_lock()
        if FILE_LOCK.try_acquire(descriptor):
            return descriptor
        os.close(descriptor)
        return None

    def alive(self) -> bool:
        """Whether a connection process holds the lock now (a look that frees it at once)."""
        try:
            descriptor = self._open_lock()
        except OSError:
            return False
        try:
            if not FILE_LOCK.try_acquire(descriptor):
                return True
            FILE_LOCK.release(descriptor)
            return False
        finally:
            os.close(descriptor)

    @property
    def paused(self) -> bool:
        return self.pause_path.exists()

    def pause(self, paused: bool) -> None:
        if paused:
            PRIVATE_FILES.write_text(self.pause_path, "")
        else:
            self.pause_path.unlink(missing_ok=True)


class PresenceWatch(BaseModel):
    """The connection's side: what it last heard from its server, turned into the `Presence` it writes."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    #: The link reads « reconnecting » once the server answered nothing for this long. Three beats: a beat's payload can take seconds to read (a cold run registry), still under the server's 12 s.
    heard_within: ClassVar[float] = 10.0
    files: PresenceFiles
    link: Link = "reconnecting"
    #: Monotonic time of the server's last answer (a pong to a beat's ping), None before the first.
    heard: float | None = None

    def hear(self) -> None:
        self.heard = time.monotonic()

    def connected(self) -> None:
        self.link, self.heard = "connected", time.monotonic()

    def presence(self, *, runs: int, update_waiting: bool, pc: str, page: str, version: str, now: float | None = None) -> Presence:
        link = self.link
        if self.files.paused:
            link = "paused"
        elif link == "connected" and (self.heard is None or (time.monotonic() if now is None else now) - self.heard > self.heard_within):
            link = "reconnecting"
        return Presence(link=link, runs=runs, update_waiting=update_waiting, pc=pc, page=page, version=version)
