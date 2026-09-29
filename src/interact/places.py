"""Levels per folder of this PC (`interact_core.PlaceLevel`): what workflows, the Data screen and
agents may do in each folder, set by the PC's owner and held here, never on the server.

A folder's level is the one set on it or on its nearest parent; a folder nobody set is `hidden`.
Every path is walked part by part from the working directory without following a link, and each
part is checked against `NEVER_GRANTABLE` (credential and configuration stores, casefolded, per
OS) and the runner's own folders: no level opens them, whatever an ancestor's level says."""

import re
import stat
import sys
import time
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from interact_core import PLACE_LEVELS, MachinePlace, MachinePlaceEntry, PlaceLevel

from interact.pinned_directory import PinnedDirectory

LEVEL_RANK: dict[PlaceLevel, int] = {level: rank for rank, level in enumerate(PLACE_LEVELS)}
#: Levels whose folder a workflow step or an agent writes in place.
IN_PLACE_WRITES: frozenset[PlaceLevel] = frozenset({"sandbox", "write"})
#: The folder offered as the first sandbox ("a folder where it can do things").
SUGGESTED_SANDBOX = "interact-files"
#: Files an agent or an editor starting in a folder obeys: a workflow never writes them in place
#: (M8); a review shows them to the owner first.
INSTRUCTION_NAMES = frozenset({"claude.md", "agents.md", "gemini.md", "copilot-instructions.md"})
#: Characters no reachable name holds: Windows streams / separators, trailing dots or spaces.
FORBIDDEN = re.compile(r"[:\\]|[. ]$")


class NeverGrantable(BaseModel):
    """Credential and configuration stores no level opens, compared casefolded: `names` and
    `suffixes` wherever they appear on a path, `home` folders beneath the home folder per platform
    (`sys.platform` prefix). Every hidden (dot) name is refused too, which covers `.ssh`, `.gnupg`,
    `.aws`, `.kube`, `.docker`, `.netrc`, `.git-credentials`, `.config` and `.local` on every OS."""

    model_config = ConfigDict(frozen=True)
    home: dict[str, tuple[str, ...]]
    names: frozenset[str]
    suffixes: tuple[str, ...]

    def refusal(self, parts: tuple[str, ...], below_home: tuple[str, ...] | None, platform: str = sys.platform) -> str | None:
        """Why `parts` (walked from the working directory; `below_home`: the same folder relative to
        the home folder, None when outside it) may never be opened, or None."""
        for part in parts:
            folded = part.casefold()
            if folded.startswith("."):
                return f"{part}: hidden files and folders are never opened"
            if folded in self.names or folded.endswith(self.suffixes):
                return f"{part}: credential stores are never opened"
        if below_home:
            joined = "/".join(part.casefold() for part in below_home)
            for prefix in next((paths for key, paths in self.home.items() if platform.startswith(key)), ()):
                if joined == prefix or joined.startswith(prefix + "/"):
                    return f"{prefix}: this system's credential and startup stores are never opened"
        return None


NEVER_GRANTABLE = NeverGrantable(
    home={
        "darwin": ("library/keychains", "library/launchagents", "library/cookies", "library/mail", "library/messages", "library/safari",
                   "library/group containers", "library/containers", "library/application support/google/chrome",
                   "library/application support/firefox", "library/application support/1password", "library/application support/bitwarden",
                   "library/application support/microsoft edge", "library/application support/bravesoftware"),
        # AppData holds browser profiles, saved credentials and Start Menu\Programs\Startup.
        "win32": ("appdata", "ntuser.dat"),
        # Snap applications keep their profiles (browsers, password managers) here.
        "linux": ("snap",),
    },
    names=frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk", "authorized_keys", "known_hosts",
                     "credentials", "credentials.json", "keychain", "keychains", "keyrings", "password-store", "wallets"}),
    suffixes=(".kdbx", ".kdb", ".keychain", ".keychain-db", ".ppk", ".p12", ".pfx", ".pem", ".gpg", ".asc", ".ovpn"),
)


def split(path: str) -> tuple[str, ...]:
    """"a/b" -> ("a", "b"); "" -> (); refuses absolute, relative ('.', '..'), empty and Windows parts."""
    if not path:
        return ()
    if path.startswith("/") or "\\" in path:
        raise PermissionError("a place is a folder relative to the working directory")
    parts = tuple(path.split("/"))
    if any(not part or part in {".", ".."} or ":" in part for part in parts):
        raise PermissionError(f"{path}: empty, '.', '..' and ':' parts are never reachable")
    return parts


class PlaceMap(BaseModel):
    """The owner's levels over one working directory, as every reader here applies them."""

    model_config = ConfigDict(frozen=True)
    working_directory: Path
    levels: dict[str, PlaceLevel] = Field(default_factory=dict)
    #: The runner's own folders (settings, installed runtimes, data): never opened.
    internal: tuple[Path, ...] = ()
    #: Names one browse page carries.
    PAGE: ClassVar[int] = 200
    #: Deepest folder a browse reaches (parts below the working directory).
    DEPTH: ClassVar[int] = 24

    @property
    def base(self) -> Path:
        return self.working_directory.resolve()

    def level(self, parts: tuple[str, ...]) -> PlaceLevel:
        """The level set on `parts` or on its nearest parent; `hidden` where none is."""
        for depth in range(len(parts), 0, -1):
            found = self.levels.get("/".join(parts[:depth]))
            if found is not None:
                return found
        return "hidden"

    def place_of(self, parts: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
        """The folder whose level `parts` takes (its path) and `parts` below it; ("", parts) when none."""
        for depth in range(len(parts), 0, -1):
            if "/".join(parts[:depth]) in self.levels:
                return "/".join(parts[:depth]), parts[depth:]
        return "", parts

    def inherited(self, parts: tuple[str, ...]) -> PlaceLevel:
        """The level `parts` would have with nothing set on it."""
        return self.level(parts[:-1]) if parts else "hidden"

    def refusal(self, parts: tuple[str, ...]) -> str | None:
        """Why no level opens `parts`: a never-grantable part (M7), a link on the way, a runner
        folder, the working directory itself. None: it may be opened."""
        if not parts:
            return "the whole working directory never takes a level; set one on a folder inside it"
        home = Path.home().resolve()
        target = self.base.joinpath(*parts)
        below_home = target.relative_to(home).parts if target.is_relative_to(home) else None
        if (said := NEVER_GRANTABLE.refusal(parts, below_home)) is not None:
            return said
        current = self.base
        for part in parts:
            current = current / part
            try:
                facts = current.lstat()
            except FileNotFoundError:
                break  # not made yet: nothing further on the way exists, so nothing is a link
            except OSError as error:
                return f"{'/'.join(parts)}: {error.strerror or type(error).__name__}"
            if PinnedDirectory.link_like(facts):
                return f"{current.relative_to(self.base).as_posix()} is a link: links are never followed"
        resolved = target.resolve()
        if resolved != target or any(resolved == folder or folder in resolved.parents or resolved in folder.parents for folder in self.internal):
            return "this runner's own folders (settings, runtimes, data) are never opened"
        return None

    def entries(self) -> tuple[MachinePlace, ...]:
        """Every level set here, with why it is not in force (when it is not)."""
        return tuple(MachinePlace(path=path, level=level, refused="" if level == "hidden" else self.refusal(split(path)) or "")
                     for path, level in sorted(self.levels.items()))

    def in_force(self, minimum: PlaceLevel = "see") -> dict[str, tuple[Path, PlaceLevel]]:
        """The folders set at `minimum` or later whose level is in force: path -> (folder, level)."""
        return {path: (self.base.joinpath(*split(path)), level) for path, level in sorted(self.levels.items())
                if LEVEL_RANK[level] >= LEVEL_RANK[minimum] and self.refusal(split(path)) is None}

    def reach(self, parts: tuple[str, ...]) -> PlaceLevel:
        """The level in force on `parts`: its level, or `hidden` when that is refused."""
        level = self.level(parts)
        return "hidden" if level == "hidden" or self.refusal(parts) is not None else level

    def with_level(self, path: str, level: PlaceLevel) -> dict[str, PlaceLevel]:
        """The levels after setting `path` to `level`: a level equal to the inherited one drops the
        entry; anything later than `hidden` on a folder no level may open is refused."""
        parts = split(path)
        if level != "hidden" and (said := self.refusal(parts)) is not None:
            raise PermissionError(said)
        levels = dict(self.levels)
        if level == self.inherited(parts):
            levels.pop(path, None)
        else:
            levels[path] = level
        if len(levels) > 256:
            raise PermissionError("a PC holds at most 256 folder levels")
        return levels

    def widens(self, path: str, level: PlaceLevel) -> bool:
        """Setting `level` on `path` opens it further than now."""
        return LEVEL_RANK[level] > LEVEL_RANK[self.level(split(path))]

    def browse(self, path: str, cursor: int) -> tuple[tuple[MachinePlaceEntry, ...], int | None]:
        """One page of names beneath `path` (names only: folders and plain files; links, hidden and
        never-grantable names left out), each with the level in force; the next cursor or None."""
        parts = split(path)
        if len(parts) > self.DEPTH:
            raise PermissionError(f"browsing stops {self.DEPTH} folders deep")
        if parts and (said := self.refusal(parts)) is not None:
            raise PermissionError(said)
        entries = []
        with PinnedDirectory.open(self.base, *parts) as folder:
            for name in folder.names():
                if NEVER_GRANTABLE.refusal((name,), None) is not None or FORBIDDEN.search(name):
                    continue
                try:
                    facts = folder.stat(name)
                except OSError:
                    continue
                if folder.link_like(facts) or not (stat.S_ISDIR(facts.st_mode) or stat.S_ISREG(facts.st_mode)):
                    continue
                entries.append((name, "folder" if stat.S_ISDIR(facts.st_mode) else "file"))
        entries.sort(key=lambda item: (item[1] != "folder", item[0].casefold()))
        page = tuple(MachinePlaceEntry(name=name, kind=kind, level=self.reach((*parts, name))) for name, kind in entries[cursor:cursor + self.PAGE])
        return page, cursor + self.PAGE if cursor + self.PAGE < len(entries) else None


class BrowseBudget:
    """How many browse pages the web may read in a window (M6): one session cannot mirror the
    whole tree quickly. Held by the runner for as long as it runs."""

    def __init__(self, pages: int = 240, window: float = 600.0) -> None:
        self.pages, self.window = pages, window
        self._taken: list[float] = []

    def take(self) -> None:
        now = time.monotonic()
        self._taken = [moment for moment in self._taken if now - moment < self.window]
        if len(self._taken) >= self.pages:
            raise PermissionError(f"browsing is limited to {self.pages} pages every {int(self.window // 60)} minutes on this PC; try again shortly")
        self._taken.append(now)
