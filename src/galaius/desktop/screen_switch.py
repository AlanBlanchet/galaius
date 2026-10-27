"""The owner's own switch on this computer: whether its screen may be watched from the web.

It lives on the machine, in this user's own file: nothing the server sends can turn it on, and
`galaius screen off` typed here ends a live session (`galaius.desktop.screen.ScreenSession`
re-reads it every second) and refuses the next one.
"""

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from galaius_core import ScreenAllowance, ScreenOffReason
from pydantic import ValidationError

from galaius.paths import UserPaths
from galaius.private_files import PRIVATE_FILES

logger = logging.getLogger("galaius")


class ScreenSwitch:
    """The owner's own switch on this computer: whether its screen may be watched at all.

    Nothing the server sends can turn this on — `galaius screen on` here does, `galaius screen off`
    here stops a live session and refuses the next one. The file is this user's alone
    (`PRIVATE_FILES`), and an allowance the owner forgot expires by itself
    (`ScreenAllowance.DEFAULT_HOURS`)."""

    #: Checked this often while a session runs, so an `off` typed on the machine lands within a frame or two.
    POLL_SECONDS: ClassVar[float] = 1.0
    #: Why a view cannot open or go on while sharing is off, and the fix, in one place for every refusal and ending.
    OFF: ClassVar[str] = "Screen sharing is off on this computer: its owner runs `galaius screen on` there."
    #: What the page is told when the owner turns sharing off here (`galaius screen off`).
    SWITCHED_OFF: ClassVar[str] = "Screen sharing was switched off on this computer."

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or UserPaths.config() / "screen.json"

    def read(self) -> ScreenAllowance:
        """What the owner last decided here; off when the file is absent or unreadable — never on
        by accident."""
        try:
            value = json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return ScreenAllowance(reason="sharing_off", why=self.OFF)
        except (OSError, ValueError):
            logger.warning("screen allowance unreadable; treated as off", exc_info=True)
            return ScreenAllowance(reason="sharing_off", why="This computer's screen setting could not be read, so it stays off.")
        try:
            return ScreenAllowance.model_validate(value)
        except ValidationError:
            return ScreenAllowance(reason="sharing_off", why="This computer's screen setting is malformed, so it stays off.")

    def write(self, allowance: ScreenAllowance) -> ScreenAllowance:
        """Save the owner's decision, owner-readable only, written whole (temp + rename)."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        PRIVATE_FILES.restrict_open(descriptor, temporary)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(allowance.model_dump(mode="json"), handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)
        PRIVATE_FILES.restrict(self.path)
        return allowance

    def turn_on(self, hours: int | None = None) -> ScreenAllowance:
        """Let the owner watch this computer, until `hours` from now (its default life when None;
        0: until `turn_off`, for a computer its owner leaves to reach from afar)."""
        span = ScreenAllowance.DEFAULT_HOURS if hours is None else hours
        return self.write(ScreenAllowance(allowed=True, until=datetime.now(UTC) + timedelta(hours=span) if span else None))

    def turn_off(self, why: str = SWITCHED_OFF,
                 reason: ScreenOffReason = "sharing_off") -> ScreenAllowance:
        return self.write(ScreenAllowance(allowed=False, reason=reason, why=why))

    def live(self) -> bool:
        return self.read().live()
