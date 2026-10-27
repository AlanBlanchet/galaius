"""Stopping one of the owner's own editor sessions from the web. galaius never types into a session it
did not start (`messaging`), but the hook it installs in every Claude Code session of this user
(`galaius.session_stop_hook`) ends that session's turn at its next tool call. `SessionStops.ask`
leaves the stop where that hook reads it; `install` puts the hook in the user's Claude Code settings,
`uninstall` takes it out when this computer leaves its account.

Claude Code reads its hooks once, when a session starts: a session open before `install` cannot be
stopped this way until it is opened again (`installed_at` tells the web so before it promises a stop)."""

import contextlib
import json
import os
import shlex
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from galaius_core import StopActor
from pydantic import BaseModel, ConfigDict, ValidationError

from galaius import session_stop_hook
from galaius.agents.providers import PROVIDERS
from galaius.paths import UserPaths
from galaius.private_files import PRIVATE_FILES

#: In every command of ours (this one's and earlier ones'), and in no command of his.
HOOK_MARK = "session_stop_hook"

Settings = dict[str, Any]
#: What changing his settings did: `written`, `unchanged` (already as wanted), `not_json` (never overwritten), `busy`
#: (Claude Code kept writing them meanwhile), `unwritable` (a read-only folder, a file Windows holds).
Rewrite = Literal["written", "unchanged", "not_json", "busy", "unwritable"]
SETTINGS_OK: tuple[Rewrite, ...] = ("written", "unchanged")


class HookInstall(BaseModel):
    """When our hook went into which settings file: a session started before `at` runs without it, and leaving the
    account edits `settings` — the file the install edited, whatever `CLAUDE_CONFIG_DIR` the leaving shell has."""

    model_config = ConfigDict(frozen=True)
    at: float
    settings: Path


class SessionStops:
    """The stops asked for this user's editor sessions (one file each, read by the hook), and the hook
    that honours them, as installed in `settings`."""

    #: The Claude Code events the hook answers: a tool call about to run, one that just ran (both end
    #: the turn), and his own prompt (which clears the stop: what he types is his).
    EVENTS: tuple[str, ...] = ("PreToolUse", "PostToolUse", "UserPromptSubmit")
    #: Rewrites of the settings tried while Claude Code keeps changing them under us.
    TRIES = 3

    def __init__(self, folder: Path | None = None, settings: Path | None = None, python: Path | None = None) -> None:
        self.folder = folder or UserPaths.data() / "session-stops"
        claude = PROVIDERS["claude"]
        self.settings = settings or claude.auth_home(os.environ) / str(claude.settings_file)
        #: The base interpreter, not a runtime's: an upgrade replaces runtimes, and a session keeps the command it
        #: started with. The hook is standard library only (`session_stop_hook`), so no runtime is needed.
        self.python = python or Path(getattr(sys, "_base_executable", "") or sys.executable)

    @property
    def script(self) -> Path:
        """The hook's own copy beside the stops: no runtime's file, which an upgrade removes."""
        return self.folder / f"{HOOK_MARK}.py"

    @property
    def _installed(self) -> Path:
        """Never `*.json`: every `*.json` beside it is a stop (the shell pre-check, `session_stop_hook.sweep`)."""
        return self.folder / "installed.record"

    def ask(self, session: UUID, by: StopActor | None) -> None:
        """Stop `session` at its next tool call, and any turn it starts by itself after that, until he
        types in it (`session_stop_hook.HOLD_SECONDS` at most). Stops past that, left by sessions closed
        before their next tool call, go."""
        PRIVATE_FILES.directory(self.folder)
        session_stop_hook.sweep(self.folder, time.time())
        PRIVATE_FILES.replace_text(self.folder / f"{session}.json", json.dumps(
            {"at": time.time(), "by": None if by is None else by.model_dump(mode="json")}))

    # The script run only when it is there: Python finding none exits 2, which Claude Code reads as « refuse this
    # tool call, erase this prompt », in every session, while no galaius is left to take the hook out (the folder
    # deleted by hand, a session still open after sign-out).
    GUARD = "import os, runpy, sys; sys.argv = sys.argv[1:]; os.path.isfile(sys.argv[0]) and runpy.run_path(sys.argv[0], run_name='__main__')"

    def argv(self) -> list[str]:
        """What the hook runs: a bare interpreter (`-I -S`) running the script copy when there is one."""
        return [str(self.python), "-I", "-S", "-c", self.GUARD, str(self.script), str(self.folder)]

    def hook(self) -> dict[str, object]:
        """The hook as his settings hold it. Where a POSIX shell runs it, the shell looks for a waiting stop and
        starts Python only when there is one: every tool call of every session runs it. On Windows Claude Code runs
        a command through Git Bash, else PowerShell, which quote a path each their own way: there the hook is its
        exec form (`args`, no shell), Python started at each tool call."""
        line = self.argv()
        if sys.platform == "win32":
            return {"type": "command", "command": line[0], "args": line[1:]}
        return {"type": "command", "command": f"for f in {shlex.quote(str(self.folder))}/*.json; do [ -e \"$f\" ] && exec {shlex.join(line)}; break; done; true"}

    def _groups(self) -> dict[str, list[dict[str, object]]]:
        hook = self.hook()
        return {event: [{"matcher": "", "hooks": [hook]} if event != "UserPromptSubmit" else {"hooks": [hook]}] for event in self.EVENTS}

    @staticmethod
    def _ours(group: object) -> bool:
        return isinstance(group, dict) and any(isinstance(hook, dict) and HOOK_MARK in f"{hook.get('command', '')} {hook.get('args', '')}"
                                               for hook in group.get("hooks", ()) or ())

    def _read(self) -> Settings | None:
        """The user's settings, {} when there are none; None when they are not JSON (left untouched). Raises an
        OSError when they cannot be read."""
        try:
            current = json.loads(self.settings.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError:
            return None
        return current if isinstance(current, dict) else None

    def _stamp(self) -> int | None:
        try:
            return self.settings.stat().st_mtime_ns
        except OSError:
            return None

    def _rewrite(self, change: Callable[[Settings], Settings | None]) -> Rewrite:
        """The settings rewritten by `change` (None: nothing to write). Claude Code writes the same file (a
        `/permissions` change) and takes no lock we can share: a file that moved while `change` ran is read
        again, so neither write is lost. A settings file that is a link (his dotfiles) is written where it points."""
        for _ in range(self.TRIES):
            stamp, current = self._stamp(), self._read()
            if current is None:
                return "not_json"
            if (changed := change(current)) is None:
                return "unchanged"
            if self._stamp() == stamp:
                PRIVATE_FILES.replace_text(self.settings.resolve(), json.dumps(changed, indent=2) + "\n")
                return "written"
        return "busy"

    def _held(self, hooks: object, exact: bool) -> bool:
        """Whether every event of `hooks` holds a group of ours — this computer's very one when `exact`."""
        wanted = self._groups()
        return isinstance(hooks, dict) and all(any(self._ours(group) and (not exact or group in wanted[event]) for group in hooks.get(event, ()) or ())
                                               for event in wanted)

    def installed(self) -> bool:
        """Whether every event of the hook is in the settings with this computer's command, its script in place,
        and his settings do not turn every hook off."""
        try:
            current = self._read()
        except OSError:
            return False
        return bool(current) and not current.get("disableAllHooks") and self.script.is_file() and self._held(current.get("hooks"), exact=True)

    def _record(self) -> HookInstall | None:
        try:
            return HookInstall.model_validate_json(self._installed.read_bytes())
        except (OSError, ValidationError):
            return None

    def installed_at(self) -> float | None:
        """When this hook went into the settings: a session started before then runs without it."""
        return None if (record := self._record()) is None else record.at

    def install(self) -> Rewrite:
        """The hook in the user's Claude Code settings, every other setting and hook kept as it is; an earlier
        command of ours is replaced. Its script copy is refreshed each time (sessions already open read the new
        one). Settings that are not JSON are never overwritten."""
        try:
            PRIVATE_FILES.directory(self.folder)
            PRIVATE_FILES.replace_text(self.script, Path(session_stop_hook.__file__).read_text(encoding="utf-8"))
            # A session started before a hook of ours was there has none; one started under an earlier command of ours keeps it.
            had = self._held((self._read() or {}).get("hooks"), exact=False)

            def add(current: Settings) -> Settings | None:
                hooks = current.get("hooks") if isinstance(current.get("hooks"), dict) else {}
                if self._held(hooks, exact=True):
                    return None
                current["hooks"] = {**hooks, **{event: [group for group in hooks.get(event, ()) or () if not self._ours(group)] + groups
                                                for event, groups in self._groups().items()}}
                return current

            if (done := self._rewrite(add)) not in SETTINGS_OK:
                return done
            if (record := self._record()) is not None and record.settings != self.settings:
                # Settings moved (CLAUDE_CONFIG_DIR changed): the hook leaves the ones it was in. Ones it cannot edit
                # keep a hook running this folder's script, so no stop is lost there.
                with contextlib.suppress(OSError):
                    self._at(record.settings)._rewrite(self._drop)
            if record is None or not had or record.settings != self.settings:
                PRIVATE_FILES.replace_text(self._installed, HookInstall(at=time.time(), settings=self.settings).model_dump_json())
            return done
        except OSError:
            return "unwritable"

    @classmethod
    def _drop(cls, current: Settings) -> Settings | None:
        """`current` without our hook, every other hook and setting kept; None when it holds none of ours."""
        hooks = current.get("hooks")
        if not isinstance(hooks, dict) or not any(cls._ours(group) for groups in hooks.values() if isinstance(groups, list) for group in groups):
            return None
        kept = {event: [group for group in groups if not cls._ours(group)] if isinstance(groups, list) else groups for event, groups in hooks.items()}
        kept = {event: groups for event, groups in kept.items() if groups != []}
        return {key: value for key, value in current.items() if key != "hooks"} | ({"hooks": kept} if kept else {})

    def _at(self, settings: Path) -> "SessionStops":
        return SessionStops(folder=self.folder, settings=settings, python=self.python)

    def uninstall(self) -> Rewrite:
        """Our hook out of the settings it went into (every other hook and setting kept), the stops and the script
        gone. A session open now keeps the hook it started with until he reopens it: finding no script, it exits 0."""
        record = self._record()
        try:
            done = (self if record is None else self._at(record.settings))._rewrite(self._drop)
            for path in [*self.folder.glob("*.json"), self._installed, self.script] if self.folder.is_dir() else ():
                path.unlink(missing_ok=True)
        except OSError:
            return "unwritable"
        return done
