"""Where galaius keeps its files on this computer, as each operating system expects them: the one
owner of the XDG / Windows / macOS folder rules every other module asks."""

import os
import sys
from pathlib import Path
from typing import ClassVar


class UserPaths:
    """This user's galaius folders."""

    @staticmethod
    def config() -> Path:
        """Settings and credentials (`machine.json`, `login-server`, `functions.json`)."""
        return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "galaius"

    @staticmethod
    def data() -> Path:
        """Installed runtimes and the installer's own uv."""
        if sys.platform == "win32":
            return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "galaius"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "galaius"
        return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "galaius"

    #: Where the agent registry lives when set (`agents`).
    AGENTS_OVERRIDE: ClassVar[str] = "GALAIUS_AGENTS_DIR"

    @staticmethod
    def agents() -> Path:
        """The agent registry (runs, their streams, queues, quota cooldowns): `AGENTS_OVERRIDE` when
        set (a probe or test keeps its runs out of the owner's), else `~/.galaius/out/agents`."""
        override = os.environ.get(UserPaths.AGENTS_OVERRIDE)
        return Path(override).expanduser() if override else Path.home() / ".galaius" / "out" / "agents"

    @staticmethod
    def launcher() -> Path | None:
        """Where new launches start (`~/.local/bin/galaius`); None on Windows (uv's own entry)."""
        return None if sys.platform == "win32" else Path(os.environ.get("XDG_BIN_HOME") or Path.home() / ".local" / "bin") / "galaius"
