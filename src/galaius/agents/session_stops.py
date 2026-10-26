"""Stopping one of the owner's own editor sessions from the web. galaius never types into a session it
did not start (`messaging`), but the hook it installs in every Claude Code session of this user
(`galaius.session_stop_hook`) ends that session's turn at its next tool call. `SessionStops.ask`
leaves the stop where that hook reads it; `install` puts the hook in the user's Claude Code settings.

Claude Code reads its hooks once, when a session starts: a session open before `install` cannot be
stopped this way until it is opened again (`installed` tells the web so before it promises a stop)."""

import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

from galaius_core import StopActor

from galaius.paths import UserPaths
from galaius.private_files import PRIVATE_FILES
from galaius.upgrade.store import active_interpreter

HOOK_MODULE = "galaius.session_stop_hook"


class SessionStops:
    """The stops asked for this user's editor sessions (one file each, read by the hook) and the hook
    that honours them, as installed in `settings`."""

    #: The Claude Code events the hook answers: a tool call about to run, one that just ran (both end
    #: the turn), and his own prompt (which clears the stop: what he types is his).
    EVENTS: tuple[str, ...] = ("PreToolUse", "PostToolUse", "UserPromptSubmit")

    def __init__(self, folder: Path | None = None, settings: Path | None = None) -> None:
        self.folder = folder or UserPaths.data() / "session-stops"
        self.settings = settings or Path.home() / ".claude" / "settings.json"

    def ask(self, session: UUID, by: StopActor | None) -> None:
        """Stop `session` at its next tool call, and any turn it starts by itself after that, until he
        types in it (`session_stop_hook.HOLD_SECONDS` at most)."""
        PRIVATE_FILES.directory(self.folder)
        PRIVATE_FILES.replace_text(self.folder / f"{session}.json", json.dumps(
            {"at": time.time(), "by": None if by is None else by.model_dump(mode="json")}))

    def command(self) -> str:
        """The hook's command line. Where a POSIX shell runs it, the shell looks for a waiting stop and
        starts Python only when there is one — every tool call of every session runs it — and finds that
        Python through the `galaius` launcher, which each upgrade points at the new runtime: a command
        naming one runtime's interpreter would outlive it. Windows names the active runtime's."""
        folder = str(self.folder)
        if sys.platform == "win32":
            return subprocess.list2cmdline([active_interpreter(), "-P", "-m", HOOK_MODULE, folder])
        launcher = shutil.which("galaius")
        python = (f'"$(dirname "$(realpath {shlex.quote(launcher)})")/python"' if launcher and Path(launcher).resolve().with_name("python").exists()
                  else shlex.quote(active_interpreter()))
        return (f"for f in {shlex.quote(folder)}/*.json; do [ -e \"$f\" ] && exec {python} -P -m {HOOK_MODULE} {shlex.quote(folder)}; "
                "break; done; true")

    def _groups(self) -> dict[str, list[dict[str, object]]]:
        hook = {"type": "command", "command": self.command()}
        return {event: [{"matcher": "", "hooks": [hook]} if event != "UserPromptSubmit" else {"hooks": [hook]}] for event in self.EVENTS}

    @staticmethod
    def _ours(group: object) -> bool:
        return isinstance(group, dict) and any(isinstance(hook, dict) and HOOK_MODULE in str(hook.get("command", ""))
                                               for hook in group.get("hooks", ()) or ())

    def _read(self) -> dict[str, object] | None:
        """The user's settings, {} when there are none; None when they cannot be read as JSON (left untouched)."""
        try:
            current = json.loads(self.settings.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return None
        return current if isinstance(current, dict) else None

    def installed(self) -> bool:
        """Whether every event of the hook is in the settings, with the command this runtime writes."""
        current = self._read()
        hooks = current.get("hooks") if current else None
        return isinstance(hooks, dict) and all(any(self._ours(group) and group in wanted for group in hooks.get(event, ()) or ())
                                               for event, wanted in self._groups().items())

    @property
    def _since(self) -> Path:
        return self.folder / "installed-at"

    def installed_at(self) -> float | None:
        """When this hook went into the settings: a session started before then runs without it."""
        try:
            return float(self._since.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def install(self) -> bool:
        """The hook in the user's Claude Code settings, every other setting and hook kept as it is; an
        earlier command of ours is replaced. False when the settings are not JSON (never overwritten)."""
        current = self._read()
        if current is None:
            return False
        if self.installed():
            if self.installed_at() is None:  # installed before the time was kept: only later sessions are sure to hold it
                PRIVATE_FILES.directory(self.folder)
                PRIVATE_FILES.replace_text(self._since, str(time.time()))
            return True
        hooks = current.get("hooks")
        hooks = hooks if isinstance(hooks, dict) else {}
        for event, groups in self._groups().items():
            hooks[event] = [group for group in hooks.get(event, ()) or () if not self._ours(group)] + groups
        current["hooks"] = hooks
        PRIVATE_FILES.replace_text(self.settings, json.dumps(current, indent=2) + "\n")
        PRIVATE_FILES.directory(self.folder)
        PRIVATE_FILES.replace_text(self._since, str(time.time()))
        return True
