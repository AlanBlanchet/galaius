"""Outbound machine connection for owner-scoped workflow agent nodes."""

import asyncio
import base64
import getpass
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import urllib.error
import urllib.request
import platform
import time
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import ClassVar, Literal, Self
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
import websockets
from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, ValidationError, field_validator, model_validator

from galaius_core import MACHINE_AGENT_REQUESTS, MACHINE_MODELS, MachineProblem, MachineProblemCode, MachineAgentSettings, MachineAgentSettingsState, MachineAgentSettingsUpdate, PlacePath, AgentRevisionRef, AgentTouchScope, ArtifactRef, EgressAllowEntry, EgressPolicy, MachineAccelerator, MachineAgentAnswer, MachineAgentRequest, MachineCommand, MachineCommandResult, MachineDataAnswer, MachineDataRequest, MachineEvent, MachineFileEntry, MachineFileListing, MachineFileQuery, MachineFileQueryResult, MachineGitOrigin, MachineRuntime, MachineTokenSwap, PlaceLevel, ScriptFile, ScriptLanguage, UserModelOrigin
from galaius import USER_AGENT
from galaius import prompt_mirror
from galaius.agents.catalog import AgentCatalog
from galaius.agents.catalog_connection import CatalogConnection
from galaius.cli.prompts import PromptMode
from galaius.agents import providers as agent_providers
from galaius.file_lock import exclusive
from galaius.private_files import PRIVATE_FILES
from galaius.error_reports import MachineErrorReports
from galaius.machine_places import PlaceDesk
from galaius.machine_agents import AgentSpawns, LogRing, MachineAgents, MachineSessions, WebRuns, redact, secret_values
from galaius.machine_workspaces import MachineWorkspaces, WorkspaceJobs
from galaius_core.sealing import SecretsSeal
from galaius.agents.events import AgentEvent
from galaius.agents.run import load_policy, run_agent
from galaius.agents.followup import FollowUps
from galaius.agents.warm import WarmStart
from galaius.agents import registry as reg
from galaius.agents.profiles import ALLOWED_ENV
from galaius.functions import FunctionRegistry, PermissionLevel, invoke as invoke_function
from galaius.vision_env import Report, VisionWorker, ensure_vision_env
from galaius import gpu_scrub, user_models
from galaius.sandbox import run_pooled
from galaius.paths import UserPaths
from galaius.upgrade.quiet import QuietPoint, UpgradeReady
from galaius.upgrade.store import RuntimeStore
from galaius.pinned_directory import PinnedDirectory
from galaius.fence import EGRESS, FenceSpec, available
from galaius.places import BrowseBudget, IN_PLACE_WRITES, INSTRUCTION_NAMES, LEVEL_RANK, PlaceMap, split
from galaius.place_reviews import PlaceReviews, write_plain
from galaius.workspace_copy import WorkspaceRefused, WorkspaceTransfers
from galaius.windowless import console_python

if sys.platform == "win32":
    import win32api


class MachineConfig(MachineAgentSettings):
    model_config = ConfigDict(extra="forbid", frozen=True)
    server_url: str
    workspace_id: UUID
    machine_id: UUID
    token: SecretStr = Field(min_length=32, max_length=256)
    permission_ceiling: PermissionLevel
    working_directory: Path
    #: Levels per folder (relative to `working_directory`; `galaius.places`): what workflows, the
    #: Data screen and agents may do there. Set HERE by the owner (`galaius machine places`), or
    #: from the web, at once either way: the web never opens the home folder itself, anything
    #: outside it, a hidden name or a credential store (`PlaceMap.web_refusal`). A folder not named is hidden.
    places: dict[str, PlaceLevel] = Field(default_factory=dict, max_length=256)
    #: Whether the web may list file names, and folders outside the home folder, to pick levels
    #: from (names only), set here only (`galaius machine browse on`), off by default. Folder names
    #: inside the home folder are listed either way.
    browse: bool = False
    #: Whether agents started here run inside the OS fence built from the levels (`galaius.fence`),
    #: set here only (`galaius machine fence on`). On: an agent that cannot be fenced never starts.
    fence_agents: bool = False
    #: The only folders a Script step may run a file from (`ScriptFile`) — set here by the owner
    #: (`galaius machine script-roots`), none by default. Never inside or around a file root: no
    #: workflow file step can write beside a script it would then run.
    script_roots: tuple[str, ...] = Field(default=(), max_length=32)
    #: The agent settings (`MachineAgentSettings`, inherited: whether agents run here, where they
    #: start, what they may do, the two opt-ins, the repositories the web may clone) are its owner's,
    #: set here (`galaius login`, `galaius machine agents | agent-roots | agent-permission`) or on
    #: the PC's page on the web while `remote_settings` is on - never `full_access` from the web.
    #: An agent root is a file root's rules, never overlapping a file or script root (no workflow
    #: file step can write an instruction file where an agent then starts): a starting point, not a
    #: fence - an agent CLI reads whatever its user can. Read as typed loosely here: a root this PC
    #: cannot use is refused where used (`usable_agent_roots`), never a reason this file fails to load.
    agent_roots: tuple[str, ...] = Field(default=(), max_length=32)
    #: Whether the owner's web page may change the agent settings above (`MachineAgentSettings`):
    #: this PC's own kill switch (`galaius machine remote off`), which nothing on the server can
    #: turn back on.
    remote_settings: bool = True
    #: Counts every change of the agent settings, from the web or here: a web change is applied
    #: only when it was built from this revision, so it never undoes a change made here since.
    settings_revision: int = Field(default=0, ge=0)
    #: The last web version applied here (`MachineAgentSettingsUpdate.version`): older or equal
    #: ones (a replay, a late delivery) are never applied.
    web_settings_version: int = Field(default=0, ge=0)
    #: How long a vision model stays loaded after a step used it (0: loaded per step, GPU memory
    #: freed at once). Trades held GPU memory for ~6 s saved on each next step on that model.
    model_keep_warm_seconds: int = Field(default=300, ge=0, le=86400)
    seen_nonces: tuple[UUID, ...] = Field(default=(), max_length=10000)

    def place_map(self) -> PlaceMap:
        """The owner's levels as every reader here applies them (the runner's own folders never open)."""
        return PlaceMap(working_directory=self.working_directory, levels=self.places, internal=self._internal())

    def writable_folders(self) -> tuple[Path, ...]:
        """The folders a workflow step or an agent writes in (any level from `write_on_review` on)."""
        return tuple(folder for folder, _ in self.place_map().in_force("write_on_review").values())

    def usable_script_roots(self) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        """(the script roots safe to use, resolved; the names refused): a file root's rules, and
        never overlapping a folder anything writes in (inside it, around it, or the same folder)."""
        return self._apart(self._usable_roots(self.script_roots), self.writable_folders())

    def usable_agent_roots(self) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        """(the agent roots safe to use, resolved; the names refused): a file root's rules, never
        overlapping a sandbox or a usable script root (inside it, around it, or the same folder)."""
        sandboxes = tuple(folder for folder, level in self.place_map().in_force("sandbox").values() if level == "sandbox")
        return self._apart(self._usable_roots(self.agent_roots), (*sandboxes, *self.usable_script_roots()[0]))

    def with_place(self, path: str, level: PlaceLevel) -> Self:
        """This configuration with `level` on `path`.
        Refused: a folder no level opens; anything writable around a script root; a sandbox
        around an agent root (nothing a workflow writes lands where an agent starts)."""
        levels = self.place_map().with_level(path, level)
        folder = self.working_directory.resolve().joinpath(*split(path))
        def overlaps(roots: tuple[Path, ...]) -> bool:
            return any(folder == root or root in folder.parents or folder in root.parents for root in roots)
        if LEVEL_RANK[level] >= LEVEL_RANK["write_on_review"] and overlaps(self._usable_roots(self.script_roots)[0]):
            raise PermissionError(f"{path} holds or sits in a script folder: nothing may write there (`galaius machine script-roots`)")
        if level == "sandbox" and overlaps(self._usable_roots(self.agent_roots)[0]):
            raise PermissionError(f"{path} holds or sits in an agent folder: a sandbox stays apart from where agents start; use write instead")
        return self.model_copy(update={"places": levels})

    def _apart(self, found: tuple[tuple[Path, ...], tuple[str, ...]], taken: tuple[Path, ...]) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        """`found` (usable, refused) minus every usable root inside, around or equal to one of `taken`."""
        usable, refused = found
        base = self.working_directory.resolve()
        overlapping = [root for root in usable if any(root == other or other in root.parents or root in other.parents for other in taken)]
        return tuple(root for root in usable if root not in overlapping), (*refused, *(root.relative_to(base).as_posix() for root in overlapping))

    def agent_roots_by_name(self) -> dict[str, Path]:
        """The usable agent roots, by the name the web shows (relative to the working directory)."""
        base = self.working_directory.resolve()
        return {root.relative_to(base).as_posix(): root for root in self.usable_agent_roots()[0]}

    def _internal(self) -> tuple[Path, ...]:
        """The runner's own folders (its settings, its installed runtimes, its data): code every
        long-lived process runs, never opened to a workflow or an agent."""
        base = self.working_directory.resolve()
        return (base / ".galaius", MachineRunner.default_config_path().parent.resolve(), UserPaths.data().resolve(), RuntimeStore.default().root.resolve())

    def _usable_roots(self, names: tuple[str, ...]) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        """A usable root is strictly below the working directory, never the home folder or above
        it, never a symlink, never holding or inside the runner's own folders, never under a
        hidden name."""
        base, home = self.working_directory.resolve(), Path.home().resolve()
        internal = self._internal()
        usable, refused = [], []
        for name in names:
            declared = base / name
            root = declared.resolve()
            if declared.is_symlink() or (declared.exists() and PinnedDirectory.link_like(declared.lstat())) or base not in root.parents or root == home or root in home.parents or any(root == folder or root in folder.parents or folder in root.parents for folder in internal) \
                    or any(part.startswith(".") for part in root.relative_to(base).parts):  # hidden names are never reachable (`CommandFiles.inside`)
                refused.append(name)
            else:
                usable.append(root)
        return tuple(usable), tuple(refused)

    def agent_settings(self) -> tuple[MachineAgentSettings, tuple[str, ...]]:
        """(the agent settings as the web reads and edits them, each root they leave out and why)."""
        roots, refused = [], []
        for name in self.agent_roots:
            try:
                roots.append(PLACE_PATH.validate_python(name))
            except ValidationError:
                refused.append(f"{name}: not a plain folder path below the working directory")
        refused += [f"{name}: overlaps a file or script root, or is hidden, a link, the home folder or galaius's own" for name in self.usable_agent_roots()[1]]
        settings = MachineAgentSettings.model_validate({**{name: getattr(self, name) for name in MachineAgentSettings.model_fields}, "agent_roots": tuple(roots)})
        return settings, tuple(dict.fromkeys(refused))

    def agent_state(self, detail: str = "", refused_version: int = 0) -> MachineAgentSettingsState:
        settings, refused = self.agent_settings()
        return MachineAgentSettingsState(revision=self.settings_revision, version=self.web_settings_version, remote=self.remote_settings,
                                         settings=settings, refused=refused, detail=detail[:400], refused_version=refused_version)

    def create_agent_roots(self) -> tuple[str, ...]:
        """Creates each agent root that does not exist yet (the web named a new one), walking it part
        by part below the working directory and never through a link or a junction; the names created."""
        base, created = self.working_directory.resolve(), []
        for name in self.agent_roots:
            try:
                with PinnedDirectory.open(base, *PurePosixPath(name).parts, create=True) as folder:
                    if folder.created:
                        created.append(name)
            except (OSError, ValueError):
                continue  # a link, a junction or a file on the way: refused where used (`usable_agent_roots`)
        return tuple(created)

    def with_web_settings(self, update: MachineAgentSettingsUpdate) -> Self:
        """This config with a web version applied, or PermissionError naming why not (checked
        against what this PC holds NOW, never what the server believes)."""
        if not self.remote_settings:
            raise PermissionError("web control of agent settings is off on this computer (galaius machine remote on)")
        if update.version <= self.web_settings_version:
            raise PermissionError(f"version {update.version} is not newer than {self.web_settings_version}, applied already")
        # A change that only takes power away (agents off, a folder removed) applies whatever was
        # changed here since; one that grants anything must be built from what this PC holds now.
        if update.based_on != self.settings_revision and not update.settings.narrows(self.agent_settings()[0]):
            raise PermissionError("these settings changed on this computer since the page read them; reload it")
        values = update.settings.model_dump(include=set(MachineAgentSettings.model_fields))
        return self.model_copy(update={**values, "settings_revision": self.settings_revision + 1, "web_settings_version": update.version})

    def reported_file_roots(self) -> list[str]:
        """The folders workflows may name (level `read` or later), as the server shows them:
        paths relative to the working directory."""
        return list(self.place_map().in_force("read"))


    @staticmethod
    def endpoint_on(server_url: str, path: str, *, socket: bool = False) -> str:
        """`path` on the server at `server_url`: its routes live at the root, whatever path the address names."""
        parsed = urlsplit(server_url)
        scheme = ("wss" if parsed.scheme == "https" else "ws") if socket else parsed.scheme
        return urlunsplit((scheme, parsed.netloc, path, "", ""))

    def endpoint(self, path: str, *, socket: bool = False) -> str:
        return self.endpoint_on(self.server_url, path, socket=socket)

    @property
    def authorization(self) -> dict[str, str]:
        """What every HTTP request of this machine carries: its own token, and which galaius it is."""
        return {"Authorization": f"Bearer {self.token.get_secret_value()}", "User-Agent": USER_AGENT}
    @field_validator("server_url")
    @classmethod
    def secure_server(cls, value: str) -> str:
        parsed = urlsplit(value)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme not in ({"https", "http"} if local else {"https"}) or parsed.username or parsed.password or not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("machine server URL must be HTTPS (HTTP is allowed only on loopback)")
        return value.rstrip("/")

    @model_validator(mode="after")
    def fixed_working_directory(self):
        if not self.working_directory.is_absolute() or not self.working_directory.is_dir():
            raise ValueError("machine working directory must be an existing absolute directory")
        return self



logger = logging.getLogger(__name__)
PLACE_PATH: TypeAdapter[str] = TypeAdapter(PlacePath)


class MachineFiles(BaseModel):
    """One of the machine owner's folder sets as a workflow reaches it — `files`, the folders the
    owner set a level on (`MachineConfig.places`: `read` to read, `write_on_review` or later to
    write), or `scripts`, the script roots Script steps run files from (`MachineConfig.script_roots`):
    a path is resolved inside one of them or refused; a person picking a script sees a folder's
    entries or a file's sha256 (`listing`, answering a `MachineFileQuery`), never anything else."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    config: MachineConfig
    area: Literal["files", "scripts"] = "files"
    #: Characters no workflow path may hold: Windows streams / separators, drive prefixes.
    FORBIDDEN: ClassVar[re.Pattern] = re.compile(r"[:\\]|[. ]$")
    #: Most entries one folder listing carries.
    MAX_ENTRIES: ClassVar[int] = 500
    #: Largest file whose digest a listing computes (a script, not a dataset).
    MAX_DIGEST_BYTES: ClassVar[int] = 64 * 1024 * 1024

    def roots(self) -> tuple[Path, ...]:
        """The owner's folders of this area that are safe to use: script roots, or the folders
        whose level lets a workflow read them."""
        if self.area == "files":
            return tuple(folder for folder, _ in self.config.place_map().in_force("read").values())
        usable, refused = self.config.usable_script_roots()
        for name in refused:
            logger.warning("script root %s is refused: it must be a real folder inside the working directory, below the home folder, apart from every folder written in", name)
        return usable

    def inside(self, relative: object, need: Literal["read", "write"] = "read") -> Path:
        """`relative` inside one of the owner's folders, else refused: an absolute or drive path,
        `..`, a link on the way, a hidden (dot) or credential name, a stream or separator
        character; for `files`, a folder whose level does not give `need`."""
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise PermissionError("a file path is relative to the machine's working directory")
        parts = Path(relative).parts
        if any(part.startswith(".") or self.FORBIDDEN.search(part) for part in parts):
            raise PermissionError(f"{relative}: hidden names, ':' '\\' and trailing dots or spaces are never reachable from a workflow")
        base = self.config.working_directory.resolve()
        target = (base / relative).resolve()
        if self.area == "files":
            level = self.config.place_map().reach(parts)
            if LEVEL_RANK[level] < LEVEL_RANK["write_on_review" if need == "write" else "read"]:
                raise PermissionError(f"{relative} is {level.replace('_', ' ')} on this machine: a workflow {'writes' if need == 'write' else 'reads'} only where its owner "
                                      f"set {'write after review, sandbox or write' if need == 'write' else 'read or later'} (`galaius machine places`)")
            if target != base.joinpath(*parts):
                raise PermissionError(f"{relative}: links are never followed")
            return target
        if not any(target == root or root in target.parents for root in self.roots()):
            raise PermissionError(f"{relative} is outside this machine's script folders ({', '.join(self.config.script_roots) or 'none'}); its owner sets them with `galaius machine script-roots`")
        if any(part.startswith(".") for part in target.relative_to(base).parts):
            raise PermissionError(f"{relative}: hidden files and folders are never reachable from a workflow")
        return target

    def relative(self, path: Path) -> str:
        return path.relative_to(self.config.working_directory.resolve()).as_posix()

    def listing(self, relative: str) -> MachineFileListing:
        """"" lists the roots; a folder its visible entries (folders first, no links, no hidden
        names); a plain file its size, sha256 and the git checkout it sits in."""
        if not relative:
            return MachineFileListing(path="", kind="folder", entries=tuple(MachineFileEntry(name=self.relative(root), kind="folder") for root in self.roots()))
        target = self.inside(relative)
        facts = os.stat(target)
        if stat.S_ISDIR(facts.st_mode):
            entries = []
            for child in sorted(target.iterdir(), key=lambda item: item.name.lower()):
                if child.name.startswith(".") or self.FORBIDDEN.search(child.name) or PinnedDirectory.link_like(child.lstat()):
                    continue
                info = child.stat()
                if stat.S_ISDIR(info.st_mode):
                    entries.append(MachineFileEntry(name=child.name, kind="folder"))
                elif stat.S_ISREG(info.st_mode):
                    entries.append(MachineFileEntry(name=child.name, kind="file", size=info.st_size))
            entries.sort(key=lambda entry: entry.kind != "folder")
            return MachineFileListing(path=self.relative(target), kind="folder", entries=tuple(entries[:self.MAX_ENTRIES]), truncated=len(entries) > self.MAX_ENTRIES)
        content = self.script_bytes(target)
        return MachineFileListing(path=self.relative(target), kind="file", size=len(content), digest=hashlib.sha256(content).hexdigest(), git=self.git_origin(target))

    def script_bytes(self, target: Path) -> bytes:
        """The bytes of a plain file of the roots (no link, no pipe, no hard-linked twin)."""
        with PinnedDirectory.open(target.parent) as folder:
            descriptor = folder.file(target.name, os.O_RDONLY | PinnedDirectory.NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            facts = os.fstat(stream.fileno())
            if not stat.S_ISREG(facts.st_mode) or facts.st_nlink > 1:
                raise PermissionError(f"{target.name} is not a plain file of the machine's workflow folders")
            if facts.st_size > self.MAX_DIGEST_BYTES:
                raise PermissionError(f"{target.name} is larger than a script may be ({self.MAX_DIGEST_BYTES // (1024 * 1024)} MiB)")
            return stream.read()

    def git_origin(self, target: Path) -> MachineGitOrigin | None:
        """The git checkout `target` sits in (its remote without credentials, commit, whether the
        file differs from it), or None: not in one, or git is not installed."""
        base, git = self.config.working_directory.resolve(), shutil.which("git")
        top = next((folder for folder in target.parents if (folder / ".git").exists() and (folder == base or base in folder.parents)), None)
        if git is None or top is None:
            return None
        def ask(*arguments: str) -> str:
            return subprocess.run([git, "-C", str(top), *arguments], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        try:
            commit, remote = ask("rev-parse", "HEAD"), ask("config", "--default", "", "--get", "remote.origin.url")
            changed = ask("status", "--porcelain", "--", str(target.relative_to(top)))
        except (OSError, subprocess.SubprocessError):
            return None
        parsed = urlsplit(remote)
        if parsed.scheme and parsed.hostname:
            remote = urlunsplit((parsed.scheme, parsed.hostname + (f":{parsed.port}" if parsed.port else ""), parsed.path, "", ""))
        try:
            return MachineGitOrigin(repository=remote[:500] or None, commit=commit, path=target.relative_to(top).as_posix(), clean=not changed)
        except ValidationError:
            return None


class MachineDataFiles(BaseModel):
    """The owner's folders as the Data screen and the agents its owner allowed read them
    (MachineDataRequest): each folder set to `see` or later is a root; beneath ONE named root,
    walked part by part without following a link (`PinnedDirectory`) — a link anywhere on the way,
    a hidden or credential name, anything but a folder or a plain single-link file is refused, even
    when it would land inside another root. `see` lists names, sizes and dates; `read` or later
    also gives the bytes; a deeper folder set to `hidden` is left out. Read-only."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    config: MachineConfig
    MAX_ENTRIES: ClassVar[int] = 500

    def roots(self) -> dict[str, Path]:
        return {path: folder for path, (folder, _) in self.config.place_map().in_force("see").items()}

    @staticmethod
    def _parts(path: str) -> tuple[str, ...]:
        if not path:
            return ()
        if path.startswith("/") or "\\" in path:
            raise PermissionError("a path is relative to its file root")
        parts = tuple(path.split("/"))
        if any(not part or part in {".", ".."} or part.startswith(".") or MachineFiles.FORBIDDEN.search(part) for part in parts):
            raise PermissionError(f"{path}: hidden names, '.', '..', ':' '\\' and trailing dots or spaces are never reachable")
        return parts

    def _folder(self, root: str, parts: tuple[str, ...]):
        """The folder `parts` beneath `root`, reached without following a link."""
        try:
            top = self.roots()[root]
        except KeyError:
            raise PermissionError(f"{root} is not one of this machine's file roots; its owner sets levels with `galaius machine places`") from None
        return PinnedDirectory.open(top, *parts)

    def _level(self, root: str, parts: tuple[str, ...]) -> PlaceLevel:
        return self.config.place_map().reach((*split(root), *parts))

    @staticmethod
    def _identity(facts: os.stat_result) -> str:
        return f"{facts.st_dev}:{facts.st_ino}:{facts.st_mtime_ns}"

    def answer(self, request: MachineDataRequest) -> MachineDataAnswer:
        if request.op == "list" and not request.root:
            return MachineDataAnswer(request_id=request.id, kind="folder", entries=tuple(MachineFileEntry(name=name, kind="folder") for name in self.roots()))
        parts = self._parts(request.path)
        needed = "see" if request.op == "list" or (request.op == "stat" and not parts) else "read"
        if request.root in self.roots() and LEVEL_RANK[level := self._level(request.root, parts)] < LEVEL_RANK[needed]:
            said = "only names are listed there" if level == "see" else "nothing there is reachable"
            raise PermissionError(f"{'/'.join(filter(None, (request.root, request.path)))} is {level} on this machine: {said}")
        if request.op == "list":
            with self._folder(request.root, parts) as folder:
                entries, truncated = [], False
                for name in sorted(folder.names(), key=str.lower):
                    if name.startswith(".") or MachineFiles.FORBIDDEN.search(name) or self._level(request.root, (*parts, name)) == "hidden":
                        continue
                    facts = folder.stat(name)
                    if folder.link_like(facts):
                        continue
                    if stat.S_ISDIR(facts.st_mode) or (stat.S_ISREG(facts.st_mode) and facts.st_nlink == 1):
                        if len(entries) == self.MAX_ENTRIES:
                            truncated = True
                            break
                        entries.append(MachineFileEntry(name=name, kind="folder" if stat.S_ISDIR(facts.st_mode) else "file", size=None if stat.S_ISDIR(facts.st_mode) else facts.st_size))
            entries.sort(key=lambda entry: (entry.kind != "folder", entry.name.lower()))
            return MachineDataAnswer(request_id=request.id, kind="folder", entries=tuple(entries), truncated=truncated)
        with self._folder(request.root, parts[:-1]) as folder:
            facts = folder.stat(parts[-1]) if parts else folder.stat()
            if stat.S_ISDIR(facts.st_mode) and not folder.link_like(facts):
                if request.op != "stat":
                    raise PermissionError(f"{request.path}: only plain files are read")
                return MachineDataAnswer(request_id=request.id, kind="folder", modified_at=datetime.fromtimestamp(facts.st_mtime, UTC), identity=self._identity(facts))
            descriptor = folder.file(parts[-1], os.O_RDONLY | PinnedDirectory.NONBLOCK)
        try:
            facts = os.fstat(descriptor)
            if not stat.S_ISREG(facts.st_mode) or facts.st_nlink != 1:
                raise PermissionError(f"{request.path}: only plain files are read")
            facts_answer = {"request_id": request.id, "kind": "file", "size": facts.st_size, "modified_at": datetime.fromtimestamp(facts.st_mtime, UTC), "identity": self._identity(facts)}
            if request.op == "stat":
                return MachineDataAnswer(**facts_answer)
            if hasattr(os, "pread"):
                data = os.pread(descriptor, request.length, request.offset)
            else:
                os.lseek(descriptor, request.offset, os.SEEK_SET)
                data = os.read(descriptor, request.length)
            return MachineDataAnswer(**facts_answer, offset=request.offset, data=base64.b64encode(data).decode())
        finally:
            os.close(descriptor)


class CommandFiles(MachineFiles):
    """The files one command moves: an input file (an `ArtifactRef` value) is downloaded from the
    server into `<working directory>/.galaius/transfers/<run>/` and handed to the step as a local
    path; a file op reads or writes one file INSIDE a folder whose level allows it
    (`MachineConfig.places`; a `write_on_review` folder receives the write in a staging copy the
    owner accepts on the machine, `galaius.place_reviews`). Every byte crosses the server over HTTP with this machine's own
    token, scoped to this command while it is in flight (`MachineCommand.input_file_path` /
    `upload_path`), its sha256 checked on arrival; every file op is logged locally."""

    command: MachineCommand
    #: Ports whose value was a file, now a local path.
    fetched: frozenset[str] = frozenset()
    UNSAFE: ClassVar[re.Pattern] = re.compile(r"[^A-Za-z0-9._-]+")
    #: Most files one command may receive.
    MAX_INPUT_FILES: ClassVar[int] = 16
    #: Largest file a read step uploads (the server refuses more anyway).
    MAX_READ_BYTES: ClassVar[int] = 1024 * 1024 * 1024

    def _request(self, path: str, **options) -> urllib.request.Request:
        return urllib.request.Request(self.config.endpoint(path), headers={**self.config.authorization, **options.pop("headers", {})}, **options)

    @property
    def inbox(self) -> Path:
        """Where this command's received files land: the runner's own folder, never a
        workflow-chosen path; removed when the command ends (`discard`)."""
        return self.config.working_directory.resolve() / ".galaius" / "transfers" / str(self.command.run_id) / str(self.command.id)

    def _inbox(self) -> Path:
        self.inbox.mkdir(parents=True, exist_ok=True)
        return self.inbox

    def discard(self) -> None:
        """The received copies are the step's inputs only: gone once it has run."""
        shutil.rmtree(self.inbox, ignore_errors=True)
        try:
            self.inbox.parent.rmdir()
        except OSError:
            pass

    def _audit(self, op: str, path: Path, digest: str, size: int) -> None:
        MachineRunner.audit("file-audit.log", {"op": op, "path": str(path), "digest": digest, "size": size, "run_id": str(self.command.run_id),
                                                "workflow_id": str(self.command.workflow.key.id), "node_id": str(self.command.node_id),
                                                "initiator_account": None if self.command.initiator_account is None else str(self.command.initiator_account)})

    def fetch_inputs(self) -> "CommandFiles":
        """Every file input downloaded and checked; the command's inputs now hold local paths. A
        step this machine will refuse anyway (a write on a read-only machine) downloads nothing."""
        if self.command.impl.kind == "builtin" and self.command.impl.op == "write_artifact" and self.config.permission_ceiling != "full_access":
            raise PermissionError("this machine is read-only: it cannot save files")
        inputs, fetched, count = dict(self.command.inputs), set(), 0
        for port, value in self.command.inputs.items():
            items = value if isinstance(value, list) else [value]
            refs = [self._artifact(item) for item in items]
            if not items or any(ref is None for ref in refs):
                continue
            count += len(refs)
            if count > self.MAX_INPUT_FILES:
                raise PermissionError(f"a step receives at most {self.MAX_INPUT_FILES} files")
            paths = [str(self._download(port, index, ref)) for index, ref in enumerate(refs)]
            inputs[port] = paths if isinstance(value, list) else paths[0]
            fetched.add(port)
        return self.model_copy(update={"command": self.command.model_copy(update={"inputs": inputs}), "fetched": frozenset(fetched)})

    @staticmethod
    def _artifact(value: object) -> ArtifactRef | None:
        if not isinstance(value, dict) or set(value) != set(ArtifactRef.model_fields):
            return None
        try:
            return ArtifactRef.model_validate(value)
        except ValidationError:
            return None

    def _download(self, port: str, index: int, ref: ArtifactRef) -> Path:
        target = self._inbox() / f"{ref.digest[:16]}-{self.UNSAFE.sub('_', ref.path.rsplit('/', 1)[-1]).lstrip('.') or 'file'}"
        partial = target.with_name(f".{target.name}.{os.getpid()}.part")
        digest, size = hashlib.sha256(), 0
        try:
            with urllib.request.urlopen(self._request(self.command.input_file_path(port, index)), timeout=300) as response, partial.open("wb") as stream:
                while chunk := response.read(1 << 20):
                    size += len(chunk)
                    if size > ref.size:
                        raise RuntimeError(f"{port}: the server sent more bytes than the file holds")
                    digest.update(chunk)
                    stream.write(chunk)
            if digest.hexdigest() != ref.digest:
                raise RuntimeError(f"{port}: the file arrived damaged (sha256 mismatch)")
            os.replace(partial, target)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"{port}: the server refused the file (HTTP {error.code})") from error
        finally:
            partial.unlink(missing_ok=True)
        logger.info("received %s (%d bytes, sha256 %s)", ref.path, size, ref.digest[:12])
        return target

    def _path(self, need: Literal["read", "write"] = "read") -> Path:
        """The file a file op names: a wired `path` input wins over the node's `artifact_path`."""
        return self.inside(self.command.inputs.get("path", self.command.config.get("artifact_path")), need)

    def _put(self, target: Path, content: bytes) -> UUID | None:
        """`content` saved at `target` as its folder's level says: in place (`sandbox`, `write`:
        never an instruction file an agent would obey, outside a sandbox), or held for the owner's
        review (`write_on_review`: the review's id)."""
        places = self.config.place_map()
        parts = target.relative_to(places.base).parts
        level = places.reach(parts)
        if level == "write_on_review":
            place, below = places.place_of(parts)
            return PlaceReviews.default().stage_file(place, places.base.joinpath(*split(place)), below, content, origin="workflow", run_id=self.command.run_id)
        if level not in IN_PLACE_WRITES:
            raise PermissionError(f"{target.name}: this folder is {level} on this machine; a workflow writes only where its owner allowed it")
        if level == "write" and target.name.casefold() in INSTRUCTION_NAMES:
            raise PermissionError(f"{target.name} steers the agents started there: a workflow writes it only in a sandbox, or after review")
        with PinnedDirectory.open(places.base, *parts[:-1], create=True) as folder:
            write_plain(folder, parts[-1], content)
        return None

    def write(self) -> dict[str, object]:
        """Saves the `value` input as a file (a received file copied as is, any other value as its
        text; several received files into `path` as a folder); answers what was saved, and the
        review holding it when its folder is write-after-review. Needs the machine's full-access
        ceiling."""
        if self.config.permission_ceiling != "full_access":
            raise PermissionError("this machine is read-only: it cannot save files")
        target, value = self._path("write"), self.command.inputs.get("value", "")
        base = self.config.working_directory.resolve()
        if "value" in self.fetched and isinstance(value, list):
            names, reviews = [], set()
            for received in value:
                name = Path(received).name
                reviews.add(self._put(self.inside(str((target / name).relative_to(base)), "write"), Path(received).read_bytes()))
                names.append(name)
            content, review = json.dumps(sorted(names)).encode(), next(iter(reviews - {None}), None)
        else:
            content = Path(value).read_bytes() if "value" in self.fetched else (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).encode()
            review = self._put(target, content)
        digest = hashlib.sha256(content).hexdigest()
        relative = target.relative_to(base).as_posix()
        self._audit("staged" if review else "write", target, digest, len(content))
        logger.info("%s %s (%d bytes)", "held for review" if review else "saved", relative, len(content))
        return {"machine": str(self.config.machine_id), "path": relative, "digest": digest, "size": len(content), **({"review": str(review)} if review else {})}

    def read(self) -> dict[str, object]:
        """Uploads one file of a file root, streamed; answers the `ArtifactRef` the server stored."""
        source = self._path()
        # Non-blocking: a named pipe planted in a root must not hang the step (refused just below).
        with PinnedDirectory.open(source.parent) as folder:
            descriptor = folder.file(source.name, os.O_RDONLY | PinnedDirectory.NONBLOCK)
        facts = os.fstat(descriptor)
        if not stat.S_ISREG(facts.st_mode) or facts.st_nlink > 1:
            os.close(descriptor)
            raise PermissionError(f"{source.name} is not a plain file of the machine's workflow folders")
        with os.fdopen(descriptor, "rb") as stream:
            if facts.st_size > self.MAX_READ_BYTES:
                raise PermissionError(f"{source.name} is larger than a step may send")
            digest = hashlib.sha256()
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
            stream.seek(0)
            query = urlencode({"name": source.name})
            request = self._request(f"{self.command.upload_path}?{query}", data=stream, method="POST",
                                    headers={"x-galaius-digest": digest.hexdigest(), "Content-Length": str(facts.st_size), "Content-Type": mimetypes.guess_type(source.name)[0] or "application/octet-stream"})
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    stored = json.loads(response.read())
            except urllib.error.HTTPError as error:
                raise RuntimeError(f"the server refused {source.name} (HTTP {error.code}): {error.read()[:200].decode(errors='replace')}") from error
        self._audit("read", source, digest.hexdigest(), facts.st_size)
        logger.info("sent %s (%d bytes, sha256 %s)", source.name, facts.st_size, digest.hexdigest()[:12])
        return ArtifactRef.model_validate(stored).model_dump(mode="json")


#: Runner log lines at or above this level travel to the server inside the command they belong to.
FORWARDED_LOG_LEVEL = logging.INFO


def level_word(record: logging.LogRecord) -> str:
    """The server's level vocabulary (debug / info / warn / error) for a stdlib record."""
    return {"WARNING": "warn", "CRITICAL": "error"}.get(record.levelname, record.levelname.lower())


class JsonLines(logging.Formatter):
    """One JSON object per line (journald, or the Windows service's log file, keeps it): the
    server's record fields, `source=machine`, plus whatever the call site bound in `extra`
    (machine, workspace, command, run, node); credential shapes masked (`redact`)."""

    _BOUND = ("machine_id", "workspace_id", "command_id", "run_id", "node_id", "action", "status", "duration_ms")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(), "level": level_word(record),
            "source": "machine", "name": record.name, "message": redact(record.getMessage()),
        }
        payload.update({key: str(getattr(record, key)) for key in self._BOUND if getattr(record, key, None) is not None})
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = redact(f"{type(record.exc_info[1]).__name__}: {record.exc_info[1]}")
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


class CommandLogs(logging.Handler):
    """Collects the runner's own log lines while one command runs, so they reach the server as
    `progress` events with `kind: log` — inside that command's span on the run's trace."""

    def __init__(self) -> None:
        super().__init__(FORWARDED_LOG_LEVEL)
        self.lines: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.lines) < 200:
            # `at`: when the line was written; the lines travel after the step, so the send time would lie.
            self.lines.append({"kind": "log", "level": level_word(record), "logger": record.name, "text": record.getMessage()[:2000], "at": datetime.fromtimestamp(record.created, UTC).isoformat()})

    def drain(self) -> list[dict[str, object]]:
        lines, self.lines = self.lines, []
        return lines


#: Agent events a machine forwards to the server while a step runs (the step's live transcript).
FORWARDED_EVENT_KINDS: frozenset[str] = frozenset({"text", "tool", "tool_result", "thinking", "error", "rate_limit", "done"})


class EnrollmentChanged(PermissionError):
    """The machine file on disk names another enrollment (machine, workspace, server or token)
    than the connection holds: nothing runs under the old one, the runner reconnects as the new."""


class TokenSwapped(Exception):
    """The server swapped this machine's bootstrap token and the new one is saved: the runner starts
    again on it, every task with it (none keeps the spent token)."""


class ScriptRuntime(BaseModel):
    """How one Script language (`ScriptLanguage`) runs: its name as the owner reads it, the file its
    inline code is written to, the program that runs it on each system (tried in order: a name on
    PATH, or an absolute path; `{system}` is Windows' own system folder) with its options, the
    program a pooled sandbox runs it with, and what is said where nothing runs it. `SCRIPT_RUNTIMES`
    holds one per language."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    title: str
    suffix: str
    programs: dict[Literal["posix", "windows"], tuple[str, ...]] = {}
    options: dict[Literal["posix", "windows"], tuple[str, ...]] = {}
    #: The program inside a pooled (Linux gVisor) sandbox; None: not offered there.
    pooled: str | None = None
    #: Said when no program here runs it.
    missing: str
    SYSTEM: ClassVar[Literal["posix", "windows"]] = "windows" if sys.platform == "win32" else "posix"

    @property
    def here(self) -> tuple[str, ...]:
        """This system's candidates, `{system}` filled in."""
        system = win32api.GetSystemDirectory() if self.SYSTEM == "windows" else ""
        return tuple(candidate.format(system=system) for candidate in self.programs.get(self.SYSTEM, ()))

    @property
    def label(self) -> str:
        """The program as the owner reads it before approving (`python3`, `/bin/sh`, `pwsh`, `cmd.exe`)."""
        first = self.programs.get(self.SYSTEM, ())
        return " ".join((Path(first[0]).name if "{system}" in first[0] else first[0], *self.options.get(self.SYSTEM, ()))) if first else self.title

    def program(self, path: str | None) -> str | None:
        """The first candidate this computer has; None when it has none."""
        for candidate in self.here:
            found = (candidate if Path(candidate).is_file() else None) if Path(candidate).is_absolute() else self.which(candidate, path)
            if found is not None:
                return found
        return None

    @staticmethod
    def which(name: str, path: str | None) -> str | None:
        """`name` on PATH only, never in the folder the runner stands in (Windows looks there
        first: a file step could plant `pwsh.cmd` beside it)."""
        found = shutil.which(name, path=path)
        folders = {Path(folder).resolve() for folder in (path or "").split(os.pathsep) if folder}
        return None if found is None or (Path(found).resolve().parent == Path.cwd().resolve() and Path.cwd().resolve() not in folders) else found

    @staticmethod
    def languages(path: str | None) -> tuple[ScriptLanguage, ...]:
        """The Script languages this computer runs, as the runner announces them (`script:<language>`):
        Python always (the runner's own), each other where its program is found."""
        return tuple(language for language, runtime in SCRIPT_RUNTIMES.items() if language == "python" or runtime.program(path) is not None)


SCRIPT_RUNTIMES: dict[ScriptLanguage, ScriptRuntime] = {
    # A machine's Python file runs on its own Python: python3, or on Windows the `py -3` launcher.
    "python": ScriptRuntime(title="Python", suffix=".py", programs={"posix": ("python3",), "windows": ("py",)}, options={"windows": ("-3",)}, pooled="python3",
                            missing="Python is not on this machine's PATH (python3, or py on Windows): name the interpreter in the step"),
    "shell": ScriptRuntime(title="Shell", suffix=".sh", programs={"posix": ("/bin/sh",)}, pooled="/bin/sh",
                           missing="a shell script runs with /bin/sh, which this computer does not have (Windows): write the step in PowerShell or cmd"),
    # The owner approved this exact code: the execution policy (not a security boundary) never refuses it.
    "powershell": ScriptRuntime(title="PowerShell", suffix=".ps1", programs={"posix": ("pwsh",), "windows": ("pwsh", "powershell")},
                                options={system: ("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File") for system in ("posix", "windows")},
                                missing="PowerShell is not installed on this machine (pwsh): install PowerShell 7 or place the step on a Windows machine"),
    # Windows' own cmd.exe, never %COMSPEC% (the environment could name another program).
    "cmd": ScriptRuntime(title="cmd", suffix=".cmd", programs={"windows": ("{system}\\cmd.exe",)}, options={"windows": ("/d", "/c")},
                         missing="cmd scripts run on Windows only: place the step on a Windows machine, or write it in shell or PowerShell"),
}


class ScriptExecution(BaseModel):
    """The program a Script step runs through here, chosen from what the owner approves (language,
    code, a file's own `interpreter`): the approval preview shows it, the runner resolves it. A file
    naming its interpreter runs through that; Python declaring its packages (PEP 723) through
    `uv run --script`, which installs them into a cached throwaway environment; other inline Python
    on the runner's own interpreter, a machine's Python file on that machine's Python; every other
    language on its `SCRIPT_RUNTIMES` program."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    program: Literal["runner_python", "python3", "uv", "shell", "powershell", "cmd", "custom"]
    #: The program a file names (a command on PATH or an absolute path); only for "custom".
    interpreter: str | None = None
    #: PEP 723 inline script metadata: `# /// script` … `# ///` (the editor writes a script's packages there).
    METADATA: ClassVar[re.Pattern] = re.compile(r"(?m)^# /// script\r?\n(?:^#(?: .*)?\r?\n)*?^# ///\r?$")  # CRLF too: a file saved on Windows
    UV: ClassVar[tuple[str, ...]] = ("run", "--quiet", "--no-project", "--script")

    @model_validator(mode="after")
    def custom_interpreter(self) -> Self:
        if (self.program == "custom") != (self.interpreter is not None):
            raise ValueError("only a custom program names its interpreter")
        return self

    @classmethod
    def select(cls, language: ScriptLanguage, source: str, file: ScriptFile | None = None) -> Self:
        if file is not None and file.interpreter is not None:
            return cls(program="custom", interpreter=file.interpreter)
        if language != "python":
            return cls(program=language)
        return cls(program="uv" if cls.METADATA.search(source) else "python3" if file is not None else "runner_python")

    @property
    def runtime(self) -> ScriptRuntime | None:
        """The table entry whose program runs it (none for the runner's own Python, uv or a named interpreter)."""
        return SCRIPT_RUNTIMES["python" if self.program == "python3" else self.program] if self.program in {"python3", "shell", "powershell", "cmd"} else None

    @property
    def options(self) -> tuple[str, ...]:
        return self.UV if self.program == "uv" else () if self.runtime is None else self.runtime.options.get(ScriptRuntime.SYSTEM, ())

    @property
    def description(self) -> str:
        if self.runtime is not None:
            return self.runtime.label
        return " ".join((self.interpreter if self.program == "custom" else {"runner_python": "galaius's own Python", "uv": "uv"}[self.program], *self.options))


class MachineRunner:
    heartbeat_seconds = 3
    #: How often this PC checks whether the prompts it runs changed (a company granted or withdrew
    #: it, a prompt or agent was saved); a check is one 304 per workspace when nothing moved.
    prompt_sync_seconds = 300
    #: How often this PC renews the agent catalog and tool list a start reads (a 304 each while
    #: nothing changed): a start reads them from memory instead of waiting on the server.
    policy_renew_seconds = 15
    #: How often this PC's run records are settled against their processes (`registry.settle_gone`).
    settle_seconds = 60
    #: How often this PC looks whether its owner accepted an error report it prepared (only while one waits).
    deliver_seconds = 60
    reconnect_seconds = (1, 2, 5, 10, 20)
    #: Failed tries in a row (about 10 s with `reconnect_seconds`) before its page is told why.
    report_after_failures = 3

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or self.default_config_path()
        #: What the last failed channel try met, and the last crash its page was told of (each said once).
        self._last_failure = ""
        self._reported_crash = ""
        #: Next event sequence per command in flight: every event of one command, from any path, is ordered.
        self._sequences: dict[UUID, int] = {}
        #: The vision model runtime kept loaded between steps (`MachineConfig.model_keep_warm_seconds`).
        self._vision: VisionWorker | None = None
        #: File queries being answered (held so the loop does not drop them mid-answer).
        self._queries: set[asyncio.Task] = set()
        #: Agent actions already accepted (id -> expiry): each is taken once (`_answer_agent_request`).
        self._agent_requests: dict[UUID, datetime] = {}
        #: The sessions this runner hosts for the web (opened on first use) and its recent log lines.
        self._sessions: MachineSessions | None = None
        #: Commands being executed now (0 or 1: one worker runs them in order).
        self._executing = 0
        #: Set when supervised: leave for a new runtime once nothing runs (`_leave_when_quiet`).
        self._quiet = QuietPoint.current()
        #: The server closed the connection to restart (1012): ask for a release check once it is back.
        self._server_restarted = False
        self._log_ring = LogRing()
        logging.getLogger("galaius").addHandler(self._log_ring)
        #: Why the web runs could not be read at the last beat (`busy`), None when they could.
        self._runs_unreadable: str | None = None
        #: Tokens this runner held before a swap (`take_token_swap`): still masked in every kept line.
        self._spent_tokens: tuple[str, ...] = ()
        #: Why the last web version of the agent settings was not applied, and which one (shown on the PC's page).
        self._settings_detail, self._settings_refused = "", 0
        self._workspace_jobs = WorkspaceJobs(path=self.config_path.with_name("machine-workspaces.json"))
        #: Staged writes waiting for the owner's review, and the web's whole-PC browse budget.
        self.reviews = PlaceReviews.default()
        self.browse_budget = BrowseBudget()
        #: The agent child started ahead of the next web start (`WarmStart`), and of the next turn of
        #: the runs whose turn ended last (`FollowUps`).
        self.warm = WarmStart()
        self.followups = FollowUps()

    def _vision_worker(self, config: MachineConfig) -> VisionWorker:
        if self._vision is None or self._vision.keep_warm != config.model_keep_warm_seconds:
            if self._vision is not None:
                self._vision.close()
            self._vision = VisionWorker(config.model_keep_warm_seconds)
        return self._vision

    def _next_sequence(self, command_id: UUID) -> int:
        self._sequences[command_id] = self._sequences.get(command_id, 0) + 1
        return self._sequences[command_id]

    async def _event(self, socket, command_id: UUID, kind: str, payload: dict[str, object] | None = None) -> None:
        event = MachineEvent(command_id=command_id, sequence=self._next_sequence(command_id), kind=kind, timestamp=datetime.now(UTC), payload=payload or {})
        await socket.send(json.dumps({"type": "event", "event": event.model_dump(mode="json")}))

    @staticmethod
    def default_config_path() -> Path:
        return UserPaths.config() / "machine.json"

    def _config_lock(self):
        """Held by every writer of the machine file (`machine.lock` beside it, never replaced)."""
        PRIVATE_FILES.directory(self.config_path.parent)
        return exclusive(os.open(self.config_path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))

    def save(self, config: MachineConfig) -> None:
        with self._config_lock():
            self._save(config)

    def update(self, change: Callable[[MachineConfig], MachineConfig]) -> MachineConfig:
        """Read, merge and atomically replace under the same lock as enrollment and owner edits. A
        change of the agent settings made here counts one more `settings_revision`."""
        with self._config_lock():
            before = self.load()
            config = MachineConfig.model_validate(change(before).model_dump())
            if config.settings_revision == before.settings_revision and config.agent_settings()[0] != before.agent_settings()[0]:
                config = config.model_copy(update={"settings_revision": before.settings_revision + 1})
            self._save(config)
            return config

    def _save(self, config: MachineConfig) -> None:
        """The token sealed as `PRIVATE_FILES` stores secrets (as is on POSIX, DPAPI on Windows)."""
        values = config.model_dump(mode="json")
        values["token"] = PRIVATE_FILES.seal(config.token.get_secret_value())
        PRIVATE_FILES.write_text(self.config_path, json.dumps(values, separators=(",", ":")) + "\n")

    def load(self) -> MachineConfig:
        values = json.loads(PRIVATE_FILES.read_text(self.config_path))
        values["token"] = PRIVATE_FILES.unseal(values["token"])
        # Machine files written before levels: each file root was read + write apart from every
        # other root, i.e. a sandbox (kept as it was; the next save drops `file_roots`).
        for name in values.pop("file_roots", ()):
            values.setdefault("places", {}).setdefault(name, "sandbox")
        # Machine files written while a web widening waited for a confirm here: the web's level
        # now applies at once, so a widening still waiting is dropped (the web asks it again).
        values.pop("pending_places", None)
        return MachineConfig.model_validate(values)

    async def connect(self, config: MachineConfig) -> None:
        # Exact secrets masked in every kept log line from the first one on (`LogRing`).
        self._log_ring.secrets = self._secrets(config)
        self._workspace_jobs.settle_interrupted()
        # Local and independent of the server: a run killed while the PC is offline is settled too.
        settler = asyncio.create_task(self._settle_runs())
        reports = asyncio.create_task(self._deliver_reports(config))
        followed = asyncio.create_task(self._follow_up(config))
        swapped = False
        try:
            await self._connect(config)
        except TokenSwapped:
            swapped = True
        except (PermissionError, UpgradeReady):
            raise  # stopped on purpose, or leaving for a newer build: not a crash
        except Exception as error:
            # Whatever restarts it (systemd, launchd, the Windows task's loop), its page says why, once per cause.
            said = f"{type(error).__name__}: {error}"
            if said != self._reported_crash:
                self._reported_crash = said
                lines = (*self._log_ring.lines, *traceback.format_exception(error))
                await asyncio.to_thread(self.report_problem, config, "crashed", said, lines, self._log_ring.secrets)
            raise
        finally:
            settler.cancel()
            reports.cancel()
            followed.cancel()
            await asyncio.gather(settler, reports, followed, return_exceptions=True)
            for ahead in (self.warm, self.followups):  # a child started ahead never outlives its runner; ended on the loop it lives on
                AgentSpawns.loop().call_soon_threadsafe(ahead.close)
        if swapped:
            await self.connect(self.load())

    def _secrets(self, config: MachineConfig) -> tuple[str, ...]:
        """Exact values masked in every kept log line: the token, the ones it replaced, the environment's secrets."""
        return (config.token.get_secret_value(), *self._spent_tokens, *secret_values(self._safe_environment()))

    async def _follow_up(self, config: MachineConfig) -> None:
        """At start: the messages to web runs a former runner left queued go to their dispatchers,
        and the runs whose turn ended last get their next turn started ahead (`FollowUps`)."""
        try:
            agents = await asyncio.to_thread(self._agents, self._current_config(config))
            if agents.run_agents:
                await agents.follow_up()
        except Exception:  # its own failure never stops the runner: a follow-up then starts as before
            logger.exception("queued follow-ups not recovered")

    async def _settle_runs(self) -> None:
        """Every `settle_seconds`, mark ended the runs whose process is gone (`registry.settle_gone`)."""
        while True:
            try:
                for run in await asyncio.to_thread(reg.settle_gone):
                    cause = run.interruption.describe() if run.interruption else ""
                    logger.info("run %s settled %s %s", run.run_id[:8], run.status, cause)
            except Exception:  # one unreadable record never stops the sweep
                logger.exception("settling run records failed")
            await asyncio.sleep(self.settle_seconds)

    async def _connect(self, config: MachineConfig) -> None:
        delay_index = 0
        while True:
            if delay_index == self.report_after_failures:
                # Still not in after these tries: its page says why (the server forgets it once it is in).
                await asyncio.to_thread(self.report_problem, config, "channel_unreachable", self._last_failure, tuple(self._log_ring.lines), self._log_ring.secrets)
            endpoint = self._channel_url(config.server_url)
            try:
                async with websockets.connect(
                    endpoint,
                    additional_headers={"Authorization": f"Bearer {config.token.get_secret_value()}"},
                    user_agent_header=USER_AGENT,
                    open_timeout=10,
                    close_timeout=3,
                    ping_interval=15,
                    max_size=1024 * 1024,
                ) as socket:
                    delay_index = 0
                    self._reported_crash = ""  # in again: a later crash, even the same one, is news to its page
                    logger.info("connected to %s", endpoint, extra={"machine_id": config.machine_id, "workspace_id": config.workspace_id})
                    if self._server_restarted:
                        self._server_restarted = False
                        RuntimeStore.default().request_check()
                    await socket.send(json.dumps({"type": "hello", "features": self.features(), **await asyncio.to_thread(self._beat, config)}))
                    if await self._serve(socket, config):
                        return
            except EnrollmentChanged:
                config = self.load()
                logger.warning("machine file changed its enrollment; reconnecting with it", extra={"machine_id": config.machine_id})
                continue
            except PermissionError:
                raise
            except websockets.ConnectionClosed as error:
                if error.code in {4401, 4403}:
                    logger.error("machine token was revoked or rejected; connection stopped", extra={"machine_id": config.machine_id})
                    print("Machine token was revoked or rejected; connection stopped.", file=sys.stderr)
                    return
                self._last_failure = f"the connection closed (code {error.code})"
                logger.warning("connection closed (code %s)", error.code, extra={"machine_id": config.machine_id})
                # The server restarted: a deploy may have brought a release, checked once it answers again.
                self._server_restarted = self._server_restarted or error.code == 1012
            except websockets.InvalidStatus as error:
                # Refused at the handshake: a revoked or unknown token answers 401/403 before the
                # socket opens (the 4401/4403 close codes above never arrive). Stop, like a close.
                if error.response.status_code in {401, 403}:
                    logger.error("machine token was refused (HTTP %s); connection stopped", error.response.status_code, extra={"machine_id": config.machine_id})
                    print("Machine token was revoked or rejected; connection stopped.", file=sys.stderr)
                    return
                self._last_failure = f"the server refused the connection: HTTP {error.response.status_code}"
                logger.warning("server refused the connection: HTTP %s", error.response.status_code, extra={"machine_id": config.machine_id})
            except (OSError, TimeoutError, websockets.InvalidHandshake) as error:
                # A server mid-deploy answers the upgrade with no or a non-101 response: retry, never exit.
                self._last_failure = f"{endpoint} unreachable: {error or type(error).__name__}"
                logger.warning("server unreachable: %s", error or type(error).__name__, extra={"machine_id": config.machine_id})
            delay = self.reconnect_seconds[min(delay_index, len(self.reconnect_seconds) - 1)]
            logger.info("reconnecting in %s s", delay, extra={"machine_id": config.machine_id})
            await asyncio.sleep(delay)
            delay_index += 1

    async def _serve(self, socket, config: MachineConfig) -> bool:
        # Only the receiver consumes the websocket; only the worker executes commands.
        # Ending a connection drops queued work but joins the active command before reconnecting:
        # cancelling to_thread would leave its subprocess running beside the next command.
        commands: asyncio.Queue[MachineCommand | None] = asyncio.Queue(maxsize=32)
        receiver = asyncio.create_task(self._receive(socket, config, commands))
        worker = asyncio.create_task(self._command_worker(socket, config, commands))
        heartbeat = asyncio.create_task(self._heartbeat(socket, config))
        upgrade = asyncio.create_task(self._leave_when_quiet(commands))
        prompts = asyncio.create_task(self._sync_prompts())
        policy = asyncio.create_task(self._renew_policy())
        try:
            done, _ = await asyncio.wait((receiver, worker, heartbeat, upgrade), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            return receiver in done and receiver.result()
        finally:
            receiver.cancel()
            heartbeat.cancel()
            upgrade.cancel()
            prompts.cancel()
            policy.cancel()
            while not commands.empty():
                commands.get_nowait()
            commands.put_nowait(None)
            for query in self._queries:
                query.cancel()
            await asyncio.gather(receiver, heartbeat, upgrade, prompts, policy, *self._queries, return_exceptions=True)
            await asyncio.shield(worker)

    async def _command_worker(self, socket, config: MachineConfig, commands: asyncio.Queue[MachineCommand | None]) -> None:
        while (command := await commands.get()) is not None:
            self._executing += 1
            try:
                await self._execute(socket, config, command)
            finally:
                self._executing -= 1

    async def _leave_when_quiet(self, commands: asyncio.Queue[MachineCommand | None]) -> None:
        """Supervised: once another runtime is active and no command, query, agent request or web
        session turn is in flight, raise `UpgradeReady` (looked at on this loop, so nothing starts
        between the look and the connection closing)."""
        if self._quiet is None:
            await asyncio.Event().wait()
        await self._quiet.watch(lambda: not commands.empty() or bool(self._executing) or bool(self._queries) or (self._sessions is not None and self._sessions.busy) or MachineWorkspaces.busy(), every=1)

    async def _receive(self, socket, config: MachineConfig, commands: asyncio.Queue[MachineCommand | None]) -> bool:
        async for payload in socket:
            response = json.loads(payload)
            if response.get("type") == "command":
                # A full queue holds this read, as the one-at-a-time loop did: never an exit.
                await commands.put(MachineCommand.model_validate(response["command"]))
            elif response.get("type") in {"file_query", "data_request"}:
                answer = (self._answer_file_query(socket, config, response["query"]) if response["type"] == "file_query"
                          else self._answer_data_request(socket, config, response["request"]))
                task = asyncio.create_task(answer)
                self._queries.add(task)
                task.add_done_callback(self._query_finished)
            elif response.get("type") == "agent_settings":
                task = asyncio.create_task(self._apply_web_settings(socket, config, response.get("settings")))
                self._queries.add(task)
                task.add_done_callback(self._query_finished)
            elif response.get("type") == "agent_request":
                task = asyncio.create_task(self._answer_agent_request(socket, config, response["request"]))
                self._queries.add(task)
                task.add_done_callback(self._query_finished)
            elif response.get("type") == "token_swap":
                try:
                    swap = MachineTokenSwap.model_validate(response)
                except ValidationError as error:
                    raise PermissionError("the server's token swap is malformed; the token is unchanged") from error
                self.take_token_swap(config, swap)
            elif response.get("type") == "revoked":
                return True
        return False

    def take_token_swap(self, config: MachineConfig, swap: MachineTokenSwap) -> None:
        """A one-time bootstrap token (a cloud instance's: its user_data holds it) is swapped by the
        server on its first connection (`MachineTokenSwap`, signed with the current token's key).
        Saved under the machine file's lock while that file still holds the current token, then
        `TokenSwapped`. A swap for another machine or not signed so stops the runner
        (`PermissionError`), its token unchanged."""
        if swap.machine.id != config.machine_id or not hmac.compare_digest(self.signature(config.token.get_secret_value(), swap), swap.signature):
            raise PermissionError("the server's token swap is invalid; the token is unchanged")
        token = swap.token

        def swapped(current: MachineConfig) -> MachineConfig:
            self._same_enrollment(current, config)
            return current.model_copy(update={"token": SecretStr(token)})

        self.update(swapped)
        self._spent_tokens = (*self._spent_tokens, config.token.get_secret_value())
        self._log_ring.secrets = (*self._log_ring.secrets, token)
        logger.info("bootstrap token swapped; connecting with the new one", extra={"machine_id": config.machine_id})
        raise TokenSwapped

    def _query_finished(self, task: asyncio.Task) -> None:
        self._queries.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("machine query failed", exc_info=error)

    #: What this runner can do beyond the base protocol (the server's `MachineChannel.require_feature`):
    #: file queries browse the owner's script roots; a script file runs from them.
    #: `tool_gateway`: its `galaius mcp` serves the external tools the server connects its agents to.
    #: `token_swap`: it takes the server's swap of a one-time bootstrap token (`take_token_swap`).
    #: `workspace_copy`: it sends a folder to another PC and receives one, creates an empty project
    #: folder, names a folder's repository, and says why a project folder failed as a code.
    #: `places_direct`: a level from the web applies at once; folder names in the home folder are
    #: listed without the PC's own browse switch (`galaius_core.PlaceLevel`).
    FEATURES: ClassVar[tuple[str, ...]] = ("file_query", "script_file", "file_read", "agent_control", "agent_settings", "web_settings", "workspaces", "start_permission", "project_secrets", "places", "agent_media", "tool_gateway", "token_swap",
                                           "workspace_copy", "places_direct")

    @classmethod
    def features(cls) -> list[str]:
        """What this runner answers, and each Script language it runs here (`script:<language>`)."""
        return [*cls.FEATURES, *(f"script:{language}" for language in ScriptRuntime.languages(cls._safe_environment().get("PATH")))]

    @staticmethod
    def signature(token: str, message: MachineCommand | MachineFileQuery | MachineDataRequest | MachineAgentRequest | MachineAgentSettingsUpdate | MachineTokenSwap) -> str:
        """What the server signs `message` with for the machine holding `token`: HMAC-SHA256 of its
        canonical JSON (sorted keys, no spaces, `signature` left out) keyed by SHA-256(token)."""
        unsigned = message.model_dump(mode="json", exclude={"signature"})
        return hmac.new(hashlib.sha256(token.encode()).digest(), json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _verify_signed(config: MachineConfig, message: MachineCommand | MachineFileQuery | MachineDataRequest | MachineAgentRequest, what: str) -> None:
        """Not expired, and signed by the server with this machine's key: the server stores
        SHA-256(token), so both sides sign with the same derived key."""
        if message.expires_at.tzinfo is None or message.expires_at <= datetime.now(UTC):
            raise PermissionError(f"{what} expired")
        if not hmac.compare_digest(MachineRunner.signature(config.token.get_secret_value(), message), message.signature):
            raise PermissionError(f"{what} signature is invalid")

    def _current_config(self, connected: MachineConfig) -> MachineConfig:
        """Current owner permissions, or refusal. A stale connection can never restore access."""
        current = self.load()
        self._same_enrollment(current, connected)
        return current

    @staticmethod
    def _same_enrollment(current: MachineConfig, connected: MachineConfig) -> None:
        if (current.machine_id, current.workspace_id, current.server_url, current.token) != (connected.machine_id, connected.workspace_id, connected.server_url, connected.token):
            raise EnrollmentChanged("machine enrollment changed on this machine; reconnecting with it")

    async def _answer_file_query(self, socket, config: MachineConfig, payload: object) -> None:
        """What a person picking a script sees of this machine: checked like a command (this
        machine, this workspace, signed, not expired), answered from the owner's file roots only."""
        try:
            query = MachineFileQuery.model_validate(payload)
        except ValidationError:
            logger.warning("malformed file query dropped")
            return
        try:
            if query.machine.id != config.machine_id or query.workspace_id != config.workspace_id:
                raise PermissionError("file query targets another machine")
            self._verify_signed(config, query, "file query")
            listing = await asyncio.to_thread(MachineFiles(config=self._current_config(config), area="scripts").listing, query.path)
            logger.info("file query %s: %s", query.path or "(roots)", listing.kind)
            answer = MachineFileQueryResult(query_id=query.id, listing=listing)
        except (PermissionError, OSError, ValueError) as error:
            reason = str(error) if isinstance(error, (PermissionError, ValueError)) else f"{query.path}: {error.strerror or type(error).__name__}"
            answer = MachineFileQueryResult(query_id=query.id, error=reason[:400] or type(error).__name__)
        await socket.send(json.dumps({"type": "file_listing", "result": answer.model_dump(mode="json")}))

    async def _answer_data_request(self, socket, config: MachineConfig, payload: object) -> None:
        """The Data screen reading this machine's FILE ROOTS: checked like a command (this machine,
        this workspace, signed, not expired), answered beneath the named root only."""
        try:
            request = MachineDataRequest.model_validate(payload)
        except ValidationError:
            logger.warning("malformed data request dropped")
            return
        try:
            if request.machine.id != config.machine_id or request.workspace_id != config.workspace_id:
                raise PermissionError("data request targets another machine")
            self._verify_signed(config, request, "data request")
            answer = await asyncio.to_thread(MachineDataFiles(config=self._current_config(config)).answer, request)
            logger.info("data %s %s/%s", request.op, request.root or "(roots)", request.path)
        except (PermissionError, OSError, ValueError) as error:
            reason = str(error) if isinstance(error, (PermissionError, ValueError)) else f"{request.path}: {error.strerror or type(error).__name__}"
            answer = MachineDataAnswer(request_id=request.id, error=reason[:400] or type(error).__name__)
        await socket.send(json.dumps({"type": "data_answer", "result": answer.model_dump(mode="json")}))

    async def _apply_web_settings(self, socket, config: MachineConfig, payload: object) -> None:
        """The owner's web page changing this PC's agent settings (`MachineAgentSettingsUpdate`):
        signed with this machine's key, for this machine, newer than the last applied, built from
        this PC's current revision, and only while `remote_settings` is on - checked against what
        the machine file holds NOW. Applied or not, the PC answers its state and logs it here."""
        try:
            update = MachineAgentSettingsUpdate.model_validate(payload)
        except ValidationError:
            logger.warning("malformed agent settings dropped")
            return
        before = self._current_config(config)
        try:
            if update.machine.id != config.machine_id or update.workspace_id != config.workspace_id:
                raise PermissionError("agent settings target another machine")
            if not hmac.compare_digest(self.signature(config.token.get_secret_value(), update), update.signature):
                raise PermissionError("agent settings signature is invalid")
            def apply(current: MachineConfig) -> MachineConfig:
                self._same_enrollment(current, config)
                return current.with_web_settings(update)
            after = await asyncio.to_thread(self.update, apply)
            if created := await asyncio.to_thread(after.create_agent_roots):
                logger.info("agent folders created for the web: %s", ", ".join(created))
            self._settings_detail, self._settings_refused = "", 0
            was, now = before.agent_settings()[0].model_dump(mode="json"), after.agent_settings()[0].model_dump(mode="json")
            changed = {key: [was[key], now[key]] for key in was if was[key] != now[key]}
            # Agents switched off from the web stop the ones the web started here, as a kill switch must.
            stopped, failed = await asyncio.to_thread(self.web_runs.stop_live) if before.run_agents and not after.run_agents else (0, 0)
            logger.info("agent settings version %s from the web applied: %s%s", update.version, ", ".join(sorted(changed)) or "no change",
                        f"; running agents stopped: {stopped}" + (f", {failed} could not be" if failed else "") if stopped or failed else "")
            error = None
        except PermissionError as refusal:
            self._settings_detail, self._settings_refused = str(refusal), update.version
            changed, error = {}, str(refusal)
            logger.warning("agent settings version %s refused: %s", update.version, refusal)
        self.audit("agents.log", {"op": "settings", "version": update.version, "changed_by": str(update.changed_by), "changed_at": update.changed_at.isoformat(), "changed": changed, "error": error})
        state = self._current_config(config).agent_state(self._settings_detail, self._settings_refused)
        await socket.send(json.dumps({"type": "agent_settings_state", "state": state.model_dump(mode="json")}))

    async def _answer_agent_request(self, socket, config: MachineConfig, payload: object) -> None:
        """The owner driving agents here from the web (`MachineAgents`): checked like a command (this
        machine, this workspace, signed, not expired; an action's id accepted once), answered from
        the owner's CURRENT settings on this machine."""
        try:
            request: MachineAgentRequest = MACHINE_AGENT_REQUESTS.validate_python(payload)
        except ValidationError:
            logger.warning("malformed agent request dropped")
            return
        try:
            if request.machine.id != config.machine_id or request.workspace_id != config.workspace_id:
                raise PermissionError("agent request targets another machine")
            self._verify_signed(config, request, "agent request")
            if request.action:
                now = datetime.now(UTC)
                self._agent_requests = {key: until for key, until in self._agent_requests.items() if until > now}
                if request.id in self._agent_requests:
                    raise PermissionError("agent request was already used")
                self._agent_requests[request.id] = request.expires_at
            current = self._current_config(config)
            if request.feature == "places":
                answer = await asyncio.to_thread(PlaceDesk(self).answer, request)
                await socket.send(json.dumps({"type": "agent_answer", "result": answer.model_dump(mode="json")}))
                return
            self._log_ring.secrets = self._secrets(current)
            agents = self._agents(current)
            answer = await agents.answer(request)
            # Reading (the page polls every few seconds) stays out of the owner's log; actions go in.
            run_id = answer.run_id or getattr(request, "run_id", None)
            placed = next((item for item in agents.runs.read() if item.run_id == run_id), None) if run_id else None
            recorded = reg.get_run(str(run_id)) if run_id else None
            folder = "/".join(filter(None, (getattr(request, "root", "") or (placed.root if placed else ""), getattr(request, "path", "") or (placed.path if placed else ""))))
            said = " ".join(f"{key}={value}" for key, value in (("role", getattr(request, "role", None) or (recorded.agent if recorded else None)), ("folder", folder),
                                                                 ("run", str(run_id)[:8] if run_id else None), ("provider", getattr(request, "provider", None) if request.op == "provider" else None),
                                                                 ("answer", answer.detail if request.op in {"answer", "send", "provider"} else None)) if value)
            logger.log(logging.INFO if request.action else logging.DEBUG, "agent %s %s", request.op, said)
        except (PermissionError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            reason = str(error) if isinstance(error, (PermissionError, ValueError, RuntimeError)) else f"{type(error).__name__}: {error}"
            answer = MachineAgentAnswer(request_id=request.id, error=reason[:400] or type(error).__name__, code=error.code if isinstance(error, WorkspaceRefused) else None)
            logger.warning("agent %s refused: %s", request.op, answer.error)
        if request.action:
            # The whole brief / message stays HERE, in the owner's local log; the server keeps a digest.
            asked = request.model_dump(mode="json", exclude={"type", "id", "machine", "workspace_id", "expires_at", "signature"})
            self.audit("agents.log", {**asked, **({"started_run_id": str(answer.run_id)} if answer.run_id and "run_id" not in asked else {}), "error": answer.error})
        await socket.send(json.dumps({"type": "agent_answer", "result": answer.model_dump(mode="json")}))

    @property
    def web_runs(self) -> WebRuns:
        """The agent runs this computer started for the web (`machine-agent-runs.json` beside the machine file)."""
        return WebRuns(path=self.config_path.with_name("machine-agent-runs.json"))

    def busy(self) -> bool:
        """Work of its own is going on here: a step, a web session's turn, a workspace being
        prepared, or an agent run started for the web still working. Said in every beat (`busy`):
        a machine the server stops when idle (a cloud machine) is not stopped under it."""
        if self._executing or (self._sessions is not None and self._sessions.busy) or MachineWorkspaces.busy():
            return True
        try:
            working, unreadable = self.web_runs.working(), None
        except (OSError, ValueError) as error:
            working, unreadable = False, str(error)
        if unreadable != self._runs_unreadable:  # said once per change, never on every beat
            if unreadable is not None:
                logger.warning("web runs unreadable; reported not busy: %s", unreadable)
            self._runs_unreadable = unreadable
        return working

    def _agents(self, current: MachineConfig) -> MachineAgents:
        """What answers the web's agent requests, from the owner's current settings `current`."""
        self._sessions = self._sessions or MachineSessions(current.working_directory)
        return MachineAgents(roots=current.agent_roots_by_name(), permission=current.agent_permission, run_agents=current.run_agents,
                             continue_conversations=current.continue_conversations, answer_approvals=current.answer_approvals, session=f"web-{current.machine_id}",
                             runs=self.web_runs, environment=self._safe_environment(),
                             sessions=self._sessions, logs=self._log_ring, seal=SecretsSeal.for_token(current.token.get_secret_value()),
                             workspaces=MachineWorkspaces(roots=current.agent_roots_by_name(), origins=current.clone_origins, jobs=self._workspace_jobs,
                                                          working_directory=current.working_directory, register_root=self._register_agent_root,
                                                          transfers=WorkspaceTransfers(endpoint=current.endpoint, headers=current.authorization),
                                                          environment={**self._safe_environment(), **{key: os.environ[key] for key in ("SSH_AUTH_SOCK",) if key in os.environ}}),
                             places=current.place_map(), fence_agents=current.fence_agents, reviews=self.reviews,
                             levels_file=self.config_path, egress=(urlsplit(current.server_url).hostname or "",), warm=self.warm, followups=self.followups)

    def _register_agent_root(self, name: str) -> bool:
        """Adds `name` (relative to the working directory) to the agent roots - an existing checkout
        of a repository the owner allowed; whether it is usable there (never over a file or script
        root, never hidden, never galaius's own folders)."""
        after = self.update(lambda current: current if name in current.agent_roots else current.model_copy(update={"agent_roots": (*current.agent_roots, name)}))
        usable = name in after.agent_roots_by_name()
        if not usable:
            self.update(lambda current: current.model_copy(update={"agent_roots": tuple(root for root in current.agent_roots if root != name)}))
        logger.info("existing checkout %s %s as an agent folder", name, "registered" if usable else "refused")
        return usable

    def _accept_command(self, config: MachineConfig, connected: MachineConfig, command: MachineCommand) -> MachineConfig:
        self._same_enrollment(config, connected)
        if command.machine.id != config.machine_id:
            raise PermissionError("machine command targets another machine")
        # A pooled command legitimately carries a DIFFERENT workspace_id (the TENANT borrowing
        # this machine) than `config.workspace_id` (this machine's OWNER) — the HMAC signature
        # below, keyed on this machine's own token, is what proves the OWNER's server authorized
        # it; the equality check stays absolute for every non-pooled command (today's 1:1 shape,
        # unchanged: threat-model-shared-compute-2026-09-24.md threat #7's invariant).
        if command.tenancy == "owner" and command.workspace_id != config.workspace_id:
            raise PermissionError("machine command targets another workspace")
        if command.tenancy == "pooled" and command.impl.kind != "script":
            # Function/model pooled dispatch needs its own sandboxed fetch/invoke path
            # (galaius.model_safety's safetensors gate, a sandboxed function call) — not built
            # yet; refuse rather than silently run either unsandboxed or not at all.
            raise PermissionError(f"pooled dispatch of a {command.impl.kind!r} step is not supported yet; script only")
        self._verify_signed(config, command, "machine command")
        if command.nonce in config.seen_nonces:
            raise PermissionError("machine command nonce was already used")
        return config.model_copy(update={"seen_nonces": (*config.seen_nonces[-9998:], command.nonce)})

    async def _execute(self, socket, config: MachineConfig, command: MachineCommand) -> None:
        config = await asyncio.to_thread(self.update, lambda current: self._accept_command(current, config, command))
        bound = {"machine_id": config.machine_id, "command_id": command.id, "run_id": command.run_id, "node_id": command.node_id, "action": command.impl.kind}
        # Everything the runner logs from here to the result reaches the server inside this step.
        forwarded = CommandLogs()
        package = logging.getLogger("galaius")
        package.addHandler(forwarded)
        logger.info("running %s step", command.impl.kind, extra=bound)
        await self._event(socket, command.id, "started")
        started = time.perf_counter()
        try:
            try:
                if command.tenancy == "pooled":
                    output = json.dumps(await asyncio.to_thread(self._run_script_pooled, command, config), separators=(",", ":"))
                elif command.impl.kind == "agent":
                    if not config.run_agents:
                        raise PermissionError("agent steps are off on this computer; its owner turns them on there with `galaius machine agents on`")
                    output = await self._run_agent(command.impl.agent, str(command.inputs["task"]), config, socket, command.id)
                else:
                    # Files first: a step never sees a server reference, only a checked local copy.
                    files = CommandFiles(config=config, command=command)
                    try:
                        files = await asyncio.to_thread(files.fetch_inputs)
                        if command.impl.kind == "builtin":
                            answer = await asyncio.to_thread(files.write if command.impl.op == "write_artifact" else files.read)
                        elif command.impl.kind == "model":
                            answer = await asyncio.to_thread(self._run_model, files.command, config, self._reporter(socket, command.id))
                        else:
                            answer = await asyncio.to_thread({"function": self._run_function, "script": self._run_script}[command.impl.kind], files.command, config)
                    finally:
                        files.discard()
                    output = json.dumps(answer, separators=(",", ":"))
                result = MachineCommandResult(command_id=command.id, nonce=command.nonce, status="succeeded", result=output[:256 * 1024])
            except Exception as error:
                logger.error("%s step failed", command.impl.kind, exc_info=error, extra=bound)
                result = MachineCommandResult(command_id=command.id, nonce=command.nonce, status="failed", error=str(error)[:400] or type(error).__name__)
        finally:
            # Threat #1c: a pooled tenant's VRAM must be scrubbed whether its run succeeded,
            # failed, or crashed — never conditioned on the run's own outcome.
            if command.tenancy == "pooled":
                await self._scrub_gpu_if_present(socket, command.id)
        duration = round((time.perf_counter() - started) * 1000)
        logger.info("%s step %s in %d ms", command.impl.kind, result.status, duration, extra={**bound, "status": result.status, "duration_ms": duration})
        package.removeHandler(forwarded)
        try:
            for line in forwarded.drain():
                await self._event(socket, command.id, "progress", line)
            await socket.send(json.dumps({"type": "result", "result": result.model_dump(mode="json")}))
        finally:
            self._sequences.pop(command.id, None)

    def _reporter(self, socket, command_id: UUID) -> Report:
        """What a step running on a worker thread uses to tell the server its phase (a `phase`
        progress event, `galaius_core.MACHINE_PHASES`); never fails the step."""
        loop = asyncio.get_running_loop()

        def report(phase: str, detail: str) -> None:
            try:
                asyncio.run_coroutine_threadsafe(self._event(socket, command_id, "progress", {"kind": "phase", "phase": phase, "detail": detail[:240]}), loop).result(timeout=10)
            except Exception:
                logger.warning("step phase not sent", exc_info=True)
        return report

    def _run_model(self, command: MachineCommand, config: MachineConfig, report: Report = lambda phase, detail: None) -> dict[str, object]:
        """Run one registered model on paths inside the machine's approved workspace: the vendor
        catalog (`galaius_core.MACHINE_MODELS`, `provider == "huggingface"`) below, or — the
        whole point of "run inferences on any kind of model" — a workspace's OWN registered model
        (`provider == "workspace"`, `_run_user_model`), dispatched first since its `impl.model` is
        a `UserModel` UUID, never a `MACHINE_MODELS` key.

        Inference itself runs in `vision_infer.py`, inside the isolated venv `ensure_vision_env`
        provisions under `~/.galaius/models/env` — torch and transformers never load into this
        process, so a machine with no vision node configured never pays for them."""
        if command.impl.provider == "workspace":
            return self._run_user_model(command, config, report)
        model_id = command.impl.model
        images = command.inputs.get("images", command.config.get("images", ()))
        score_threshold = command.config.get("score_threshold", 0.5)
        # The one registry (galaius_core.MACHINE_MODELS); the runner script names tasks its own way.
        spec = MACHINE_MODELS[model_id]
        task_name, license_name = {"object-detection": "detection", "image-segmentation": "segmentation"}[spec.task], spec.license
        cache_root = Path.home() / ".galaius" / "models"
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache_root.chmod(0o700)

        run_dir = cache_root / "runs" / str(command.run_id) / str(command.node_id)
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir.chmod(0o700)
        image_paths = [str(self._model_input_path(raw_path, config.working_directory)) for raw_path in (images if isinstance(images, list) else [images])]

        logger.info("%s (%s) on %d image(s), score threshold %s", model_id, spec.task, len(image_paths), score_threshold)
        python = ensure_vision_env(cache_root, report=report)
        request_path = run_dir / "request.json"
        request_path.write_text(json.dumps({
            "model": model_id,
            "task": task_name,
            "license": license_name,
            "cache_root": str(cache_root),
            "run_dir": str(run_dir),
            "score_threshold": score_threshold,
            "image_paths": image_paths,
        }), encoding="utf-8")
        output = self._vision_worker(config).run(python, request_path, report)
        found = {key: len(value) for key, value in output.items() if isinstance(value, list)} if isinstance(output, dict) else {}
        logger.info("%s answered: %s", model_id, ", ".join(f"{count} {key}" for key, count in found.items()) or "no list output")
        return output

    def _run_user_model(self, command: MachineCommand, config: MachineConfig, report: Report = lambda phase, detail: None) -> dict[str, object]:
        """Run one workspace-registered model (`galaius_core.UserModel`, `impl.provider ==
        "workspace"`): the server resolved its CURRENT `origin` at dispatch time and signed it
        into `command.config["_user_model"]` (never stale — a later edit to the registration is read
        on the NEXT run, same as every other resource this app resolves at dispatch time). This machine
        independently re-validates that shape (`UserModelOrigin.model_validate`, never a bare dict
        trusted as-is), fetches the weights (`galaius.user_models`, byte-verified safetensors-only
        inside that call) and only then hands a LOCAL path to the same isolated `vision_env`
        subprocess the vendor catalog models use above — origin bytes never touch this process,
        torch/transformers still never import here.

        Two capability boundaries this machine refuses past, by name, rather than pretending to
        cross them: a `docker_image` origin is pulled (real, useful — proves the image is still
        reachable) but never RUN (no proven container I/O contract, no verified GPU passthrough);
        a `trusts_remote_code` origin is fetched and byte-verified but never LOADED (loading it
        would execute the repo's own Python, which this machine only sandboxes at the same gVisor
        tier a script node gets — unverified for GPU passthrough on this build, threat-model
        `pool.py` docstring). Both refusals happen AFTER their real, safe half of the work, never
        instead of it."""
        payload = command.config.get("_user_model")
        if not isinstance(payload, dict) or not isinstance(payload.get("origin"), dict):
            raise RuntimeError("server did not resolve this workspace model's origin; reconnect and re-run")
        origin = UserModelOrigin.model_validate(payload["origin"])
        licence = str(payload.get("licence") or "")
        task = command.impl.task
        if task not in user_models.SUPPORTED_TASKS:
            raise RuntimeError(f"this machine's runner does not support task {task!r} yet (supported: {', '.join(sorted(user_models.SUPPORTED_TASKS))})")
        cache_root = Path.home() / ".galaius" / "models"
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache_root.chmod(0o700)
        label = f"{origin.repo_id} @ {origin.revision or 'main'}" if origin.repo_id else command.impl.model
        if origin.kind == "docker_image":
            pulled = user_models.pull_docker_image(origin)
            raise RuntimeError(f"pulled and verified {pulled}; running a Docker-image model is not implemented on this machine yet")
        logger.info("fetching workspace model %s (%s) from %s", label, task, origin.kind)
        weights_dir = user_models.fetch_weights_dir(origin, command.impl.model, cache_root, config.server_url, config.token.get_secret_value())
        if origin.trusts_remote_code:
            raise RuntimeError(f"fetched and byte-verified {label}'s weights, but this machine has no sandboxed runtime for trust_remote_code models yet")

        images = command.inputs.get("images", command.config.get("images", ()))
        score_threshold = command.config.get("score_threshold", 0.5)
        task_name = {"object-detection": "detection", "image-segmentation": "segmentation"}[task]
        run_dir = cache_root / "runs" / str(command.run_id) / str(command.node_id)
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir.chmod(0o700)
        image_paths = [str(self._model_input_path(raw_path, config.working_directory)) for raw_path in (images if isinstance(images, list) else [images])]

        logger.info("%s (%s) on %d image(s), score threshold %s", label, task, len(image_paths), score_threshold)
        python = ensure_vision_env(cache_root, report=report)
        request_path = run_dir / "request.json"
        request_path.write_text(json.dumps({
            "model": label,
            "task": task_name,
            "license": licence,
            "cache_root": str(cache_root),
            "run_dir": str(run_dir),
            "score_threshold": score_threshold,
            "image_paths": image_paths,
            "weights_dir": str(weights_dir),
        }), encoding="utf-8")
        output = self._vision_worker(config).run(python, request_path, report)
        found = {key: len(value) for key, value in output.items() if isinstance(value, list)} if isinstance(output, dict) else {}
        logger.info("%s answered: %s", label, ", ".join(f"{count} {key}" for key, count in found.items()) or "no list output")
        return output

    def _run_function(self, command: MachineCommand, config: MachineConfig) -> object:
        """Runs one `@galaius.function`-decorated Python callable or registered shell command,
        the inbound half of PC integration (`_run_model`'s sibling: local compute reached FROM a
        workflow). `function_version` is re-checked against the entry's OWN current content hash
        — a workflow node stays pinned to the exact signature it was wired against, so a local
        edit that changes ports fails loudly here instead of silently coercing arguments — and
        the LOCAL permission ceiling still gates a `full_access` function exactly like every
        other machine command."""
        name = command.impl.name
        entry = FunctionRegistry().get(name)
        if entry.version != command.impl.version:
            raise RuntimeError(f"function {name!r} changed on this machine since the workflow was saved; re-select it")
        if entry.permission == "full_access" and config.permission_ceiling != "full_access":
            raise PermissionError(f"function {name!r} needs full-access permission; this machine is read-only")
        # A port's constant (the node's config) first, its wired value over it.
        arguments = {**command.config, **command.inputs}
        logger.info("function %s (%s) with %s", name, entry.version[:8], ", ".join(sorted(arguments)) or "no arguments")
        return invoke_function(entry, arguments, working_directory=config.working_directory)

    def _run_script(self, command: MachineCommand, config: MachineConfig, timeout: float = 120) -> str:
        """Runs a script the machine owner has already approved by its EXACT digest — a server-side
        gate (`MachineStore.script_approved`) checked BEFORE this ever dispatches; re-verified here
        too (defense in depth: two independent checks of the same bytes, never one trusted alone):
        inline source against its digest, a file of the machine (`ScriptFile`) against the sha256
        it was picked with, read NOW (commands run one at a time, so no other step rewrites it
        between this check and the start). Threat-modeler mitigations applied: #2 mandatory
        full_access (arbitrary code exec is de facto full_access regardless of any local setting);
        #4 one audit line per ATTEMPT, written before running, so a crash still leaves a record; #5
        a real subprocess (genuine timeout kill), the same stripped credential-free environment
        `_run_agent` uses, confined to the working directory (a file: its folder or its `cwd`)."""
        language, digest = command.impl.language, command.impl.source_digest
        if config.permission_ceiling != "full_access":
            raise PermissionError("script nodes always need full-access permission; this machine is read-only")
        if command.impl.origin == "machine_file":
            spec = command.impl.script_file(command.config)
            files = MachineFiles(config=self._current_config(config), area="scripts")
            path = files.inside(spec.path)
            content = files.script_bytes(path)
            found = hashlib.sha256(content).hexdigest()
            if found != spec.file_digest:
                raise RuntimeError(f"{spec.path} changed on this machine since it was picked (its sha256 is now {found[:12]}, it was {spec.file_digest[:12]}): check the file again in the step, then its owner approves the new digest")
            self._audit_script(command, spec.path)
            logger.info("%s script file %s (%s), timeout %s s", language, spec.path, found[:12], timeout)
            argv = [*self._interpreter(language, content.decode(errors="replace"), spec), str(path), *spec.args]
            if any(Path(argv[0]).resolve() == root or root in Path(argv[0]).resolve().parents for root in files.config.writable_folders()):
                raise PermissionError(f"{spec.interpreter}: the program that runs a script cannot live in a folder workflow file steps write to")
            return self._run_process(argv, files.inside(spec.cwd) if spec.cwd else path.parent, timeout)
        source = str(command.config["source"])
        try:
            command.impl.check(command.config)  # the same rule the server applied: source, language and pin agree
        except ValueError as error:
            raise RuntimeError(f"{error}; refusing to run") from error
        self._audit_script(command)
        logger.info("%s script %s (%d lines), timeout %s s", language, digest[:12], source.count("\n") + 1, timeout)
        with tempfile.TemporaryDirectory(prefix="galaius-script-") as scratch:
            script_path = Path(scratch) / f"script{SCRIPT_RUNTIMES[language].suffix}"
            script_path.write_text(source, encoding="utf-8")
            script_path.chmod(0o700)
            return self._run_process([*self._interpreter(language, source), str(script_path)], config.working_directory, timeout)

    def _run_process(self, argv: list[str], cwd: Path, timeout: float) -> str:
        """One script process: its standard output is the step's result, a non-zero exit its error."""
        try:
            completed = subprocess.run(argv, cwd=cwd, env=self._safe_environment(), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"script exceeded its {timeout}s timeout") from error
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or f"script exited {completed.returncode}").strip()[-2000:])
        if completed.stderr.strip():
            logger.warning("script wrote to stderr: %s", completed.stderr.strip()[-600:])
        logger.info("script exited 0, %d bytes of output", len(completed.stdout))
        return completed.stdout

    @classmethod
    def _interpreter(cls, language: ScriptLanguage, source: str, spec: ScriptFile | None = None) -> list[str]:
        """Resolve the same execution description the owner sees before approval; a language this
        computer cannot run is refused in plain words, never run by another program."""
        execution = ScriptExecution.select(language, source, spec)
        path = cls._safe_environment().get("PATH")
        if execution.program == "custom":
            interpreter = execution.interpreter
            if ("/" in interpreter or "\\" in interpreter) and not Path(interpreter).is_absolute():
                raise RuntimeError(f"{interpreter}: name the program by its full path, or by a name on the machine's PATH")
            found = interpreter if Path(interpreter).is_absolute() else ScriptRuntime.which(interpreter, path)
            if found is None or not os.access(found, os.X_OK):
                raise RuntimeError(f"{interpreter} is not a program this machine can run (not found on its PATH)")
            return [found]
        if execution.program == "runner_python":
            return [console_python()]
        if execution.program == "uv":
            found = ScriptRuntime.which("uv", path)
            if found is None:
                raise RuntimeError("this script declares the packages it needs (PEP 723): install uv on this machine to run it")
            return [found, *execution.options]
        found = execution.runtime.program(path)
        if found is None:
            raise RuntimeError(execution.runtime.missing)
        return [found, *execution.options]

    def _run_script_pooled(self, command: MachineCommand, config: MachineConfig, timeout: float = 120) -> str:
        """The POOLED sibling of `_run_script`: same digest re-check and audit log, but the
        source runs inside a fresh gVisor container (`galaius.sandbox.run_pooled`) instead of a
        bare subprocess on this host — the tenant did not enroll this machine and never gets its
        real filesystem, PID namespace, or unrestricted network. `_pool_egress_allow` (a plain
        list of `{host,port}` the server stamped into `command.config`) becomes a typed
        `EgressPolicy`; empty means no network at all, the sandbox's own default."""
        language, digest, source = command.impl.language, command.impl.source_digest, str(command.config["source"])
        try:
            command.impl.check(command.config)  # the same rule the server applied: source, language and pin agree
        except ValueError as error:
            raise RuntimeError(f"{error}; refusing to run") from error
        if config.permission_ceiling != "full_access":
            raise PermissionError("script nodes always need full-access permission; this machine is read-only")
        self._audit_script(command)
        raw_allow = command.config.get("_pool_egress_allow", [])
        policy = EgressPolicy(allow=tuple(EgressAllowEntry.model_validate(entry) for entry in raw_allow)) if isinstance(raw_allow, list) else EgressPolicy()
        runtime = SCRIPT_RUNTIMES[language]
        if runtime.pooled is None:
            raise RuntimeError(f"pooled machines do not run {runtime.title} scripts")
        interpreter, script_name = runtime.pooled, f"script{runtime.suffix}"
        logger.info("pooled %s script %s (%d lines), timeout %s s, egress allow=%d host(s)", language, digest[:12], source.count("\n") + 1, timeout, len(policy.allow))
        result = run_pooled(run_id=command.run_id, command=[interpreter, script_name], input_files={script_name: source.encode()}, egress=policy, timeout=timeout)
        if result.exit_code != 0:
            raise RuntimeError((result.stderr or f"pooled script exited {result.exit_code}").strip()[-2000:])
        if result.stderr.strip():
            logger.warning("pooled script wrote to stderr: %s", result.stderr.strip()[-600:])
        logger.info("pooled script exited 0, %d bytes of output", len(result.stdout))
        return result.stdout

    async def _scrub_gpu_if_present(self, socket, command_id: UUID) -> None:
        """Runs after EVERY pooled command, success or failure — threat #1c: a machine reporting a
        CUDA accelerator gets its free VRAM scrubbed (`galaius.gpu_scrub`) before the next tenant
        can be placed on it, and the server only trusts a fresh `GpuScrubRecord` from THIS event,
        never from the run having merely succeeded."""
        if not self._cuda_accelerators():
            return
        try:
            scrubbed_bytes = await asyncio.to_thread(gpu_scrub.scrub_all_free_memory)
            kind = "device_reset" if gpu_scrub.gpu_reset_supported() else "scrubbed"
            logger.info("pooled GPU scrub: %d bytes, kind=%s", scrubbed_bytes, kind)
            # `kind="gpu_scrub"` here is the PROGRESS PAYLOAD's own content-type tag (like
            # `CommandLogs`' `{"kind": "log", ...}`), never the outer `MachineEvent.kind` (that
            # stays "progress" — `_event`'s third argument, below); the server reads
            # `event.payload["kind"]`, not `event.kind`, to recognise this one.
            await self._event(socket, command_id, "progress", {"kind": "gpu_scrub", "accelerator_index": 0, "scrub_kind": kind})
        except gpu_scrub.CudaUnavailable as error:
            # A machine that REPORTED a CUDA accelerator but cannot actually reach the driver is a
            # machine this server must never trust with the next tenant's GPU placement — logged
            # loudly rather than silently letting `gpu_scrub_fresh` stay stale on the server side.
            logger.error("pooled GPU scrub failed, device may be left dirty for the next tenant: %s", error)

    @staticmethod
    def _audit_script(command: MachineCommand, path: str | None = None) -> None:
        """A durable local record of every script ATTEMPT (not just successes): source digest,
        the workflow/node/run that triggered it. Threat-modeler mitigation #4 — deliberately
        machine-side, next to the running process, so the record survives even a server outage;
        pairs with the server's OWN `machine_script_approvals.approved_by_account_id` for who
        allowlisted the digest in the first place."""
        MachineRunner.audit("script-audit.log", {
            "language": command.impl.language, "source_digest": command.impl.source_digest, "approval_digest": command.impl.approval_digest(command.config), "workflow_id": str(command.workflow.key.id),
            "workflow_revision": str(command.workflow.revision), "node_id": str(command.node_id), "run_id": str(command.run_id),
            **({"path": path} if path is not None else {}),
        })

    @staticmethod
    def audit(log: str, entry: dict[str, object]) -> None:
        """Appends one timestamped JSON line to a local, owner-only audit log beside the runner's
        configuration: what workflows made this machine do, surviving a server outage."""
        path = FunctionRegistry().config_path.parent / log
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": datetime.now(UTC).isoformat(), **entry}, separators=(",", ":")) + "\n")

    @staticmethod
    def _functions() -> list[dict[str, object]]:
        try:
            entries = FunctionRegistry().load()
        except PermissionError:
            return []
        return [entry.summary().model_dump(mode="json") for entry in entries]

    @staticmethod
    def _model_input_path(raw_path: str, working_directory: Path) -> Path:
        root = working_directory.resolve(strict=True)
        candidate = Path(raw_path)
        image_path = candidate.resolve(strict=True) if candidate.is_absolute() else (root / candidate).resolve(strict=True)
        if not image_path.is_relative_to(root) or not image_path.is_file():
            raise ValueError("model image paths must be files under the machine working directory")
        return image_path

    async def _run_agent(self, agent: AgentRevisionRef, task: str, config: MachineConfig, socket, command_id: UUID) -> str:
        safe_environment = self._safe_environment()
        original_environment = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(safe_environment)
            handle = await run_agent(
                None,
                task,
                agent_ref=AgentCatalog.reference(agent.id, agent.revision),
                cwd=str(config.working_directory),
                permission_mode=config.permission_ceiling,
                fence=await asyncio.to_thread(self._agent_fence, config),
            )
            logger.info("agent run %s started", handle.run_id, extra={"machine_id": config.machine_id, "command_id": command_id})
            await self._event(socket, command_id, "progress", {"kind": "log", "level": "info", "logger": __name__, "text": f"agent run {handle.run_id} started", "agent_run_id": handle.run_id})
            observed = 0

            async def forward(events: list[AgentEvent]) -> None:
                """Every event the server's step view reads, as it happens: what the agent said, each
                tool call WITH its arguments and — paired by `tool_id` — what the tool returned, and
                each event's token / cost figures, whose sum is the run's total."""
                nonlocal observed
                for event in events[observed:]:
                    observed += 1
                    if event.kind not in FORWARDED_EVENT_KINDS:
                        continue
                    payload = {"kind": event.kind, "text": event.text[:2000], "tool": event.tool, "tool_input": event.tool_input[:2000], "tool_id": event.tool_id,
                               "input_tokens": event.input_tokens, "output_tokens": event.output_tokens, "cost_usd": event.cost_usd}
                    await self._event(socket, command_id, "progress", {key: value for key, value in payload.items() if value not in (None, "")})

            while handle.process.returncode is None:
                await forward(reg.read_events(handle.run_id))
                await asyncio.sleep(0.3)
            exit_code = await handle.wait()
            events = reg.read_events(handle.run_id)
            # The loop exits before the final events land (the terminal "done" carries the totals).
            await forward(events)
            result = self._final_text(events)
            if exit_code != 0:
                raise RuntimeError(result or f"agent exited {exit_code}")
            if not result:
                raise RuntimeError("agent completed without a text result")
            return result
        finally:
            os.environ.clear()
            os.environ.update(original_environment)

    def _agent_fence(self, config: MachineConfig) -> FenceSpec | None:
        """What a workflow's agent step is fenced by (its levels, starting in the working directory,
        which shows only the folders they open), None when its owner left the fence off."""
        if not config.fence_agents:
            return None
        ready, reason = available()
        if not ready:
            raise PermissionError(f"the agent fence is on here but cannot be built: {reason}; its owner switches it off there with `galaius machine fence off`")
        places, providers = config.place_map(), ("claude", "codex")
        programs = tuple(Path(found) for found in (shutil.which(name) for name in providers) if found)
        spec = FenceSpec(working_directory=config.working_directory, levels_file=self.config_path, levels=places.levels, internal=places.internal,
                         start=config.working_directory, providers=providers, programs=programs, state=self.reviews.root.parent / "fence-state" / str(uuid4()), reviews=self.reviews.root,
                         egress=(*(host for provider in providers for host in EGRESS[provider]), urlsplit(config.server_url).hostname or ""))
        spec.build()
        return spec

    @staticmethod
    def _final_text(events: list[AgentEvent]) -> str:
        terminal = next((event for event in reversed(events) if event.kind == "done" and event.final_text and event.text.strip()), None)
        if terminal is not None:
            return terminal.text
        last = next((event for event in reversed(events) if event.kind == "text" and event.text.strip()), None)
        return "" if last is None else last.text

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        fixed = {"HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                 # Windows: what any program needs to start there (Python needs SYSTEMROOT, cmd COMSPEC), no secret among them.
                 "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "USERNAME", "USERDOMAIN", "APPDATA", "LOCALAPPDATA",
                 "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS", "PSMODULEPATH"}
        return {key: value for key, value in os.environ.items() if key in fixed or key in ALLOWED_ENV or key.startswith("LC_")}

    @staticmethod
    def _runtimes(config: MachineConfig) -> list[dict[str, str]]:
        """The agent CLIs the server may place agent steps on: none while agent steps are off here."""
        if not config.run_agents:
            return []
        return [MachineRuntime(provider=provider.name).model_dump(mode="json", exclude_none=True) for provider in agent_providers.PROVIDERS.values() if provider.name in {"claude", "codex"} and provider.available()]

    @staticmethod
    def _resources(working_directory: Path | None = None) -> dict[str, int]:
        """CPU count, total RAM and free disk on the machine's own working directory — the other
        half of a placement fit check next to `_accelerators`. POSIX `sysconf`, Windows
        `GlobalMemoryStatusEx` (no `psutil` dependency, no torch import); `cpu_count() or 1`
        matches the stdlib's own documented fallback."""
        ram_mb = 0
        try:
            total = win32api.GlobalMemoryStatusEx()["TotalPhys"] if sys.platform == "win32" else os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            ram_mb = total // (1024 * 1024)
        except (ValueError, OSError, AttributeError):
            pass
        try:
            disk_free_gb = shutil.disk_usage(working_directory or Path.home()).free // (1024 ** 3)
        except OSError:
            disk_free_gb = 0
        return {"cpu_count": os.cpu_count() or 1, "ram_mb": ram_mb, "disk_free_gb": disk_free_gb}

    @staticmethod
    def _accelerators() -> list[dict[str, object]]:
        """GPUs on this machine, so a `model` step (detection, segmentation) defaults here.

        Torch-free: `nvidia-smi` is queried directly and Apple's unified GPU is inferred from the
        platform, never by importing torch just to ask "is there a GPU" and dragging its multi-GB
        wheel onto a machine that runs no vision node. `none` is reported explicitly (not an empty
        list) so a runner that checked and found nothing is distinguishable from an older runner
        that never reported accelerators at all."""
        found = MachineRunner._cuda_accelerators() or MachineRunner._mps_accelerator()
        accelerators = found or (MachineAccelerator(kind="none", name="none", memory_mb=0),)
        return [item.model_dump(mode="json") for item in accelerators]

    @staticmethod
    def _cuda_accelerators() -> tuple[MachineAccelerator, ...]:
        found = shutil.which("nvidia-smi")
        if found is None:
            return ()
        try:
            completed = subprocess.run(
                [found, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        if completed.returncode != 0:
            return ()
        accelerators = []
        for line in completed.stdout.splitlines():
            name, _, memory = line.partition(",")
            name, memory = name.strip(), memory.strip()
            if name and memory.isdigit():
                accelerators.append(MachineAccelerator(kind="cuda", name=name, memory_mb=int(memory)))
        return tuple(accelerators)

    @staticmethod
    def _mps_accelerator() -> tuple[MachineAccelerator, ...]:
        if sys.platform != "darwin" or platform.machine() != "arm64":
            return ()
        return (MachineAccelerator(kind="mps", name="Apple GPU", memory_mb=0),)

    async def _sync_prompts(self) -> None:
        """Keep this PC's agents on the prompts of the workspace it runs, and its ~/galaius copies
        current, without anyone running `galaius prompts sync` (`prompt_mirror.sync`). A failed
        check is logged and retried next round; it never touches the machine channel."""
        seen: dict[UUID, str] = {}
        while True:
            try:
                connection = CatalogConnection.load()
                if connection is not None and connection.auth_mode == "machine" and await asyncio.to_thread(prompt_mirror.changed, connection, seen):
                    report = await asyncio.to_thread(prompt_mirror.sync, connection, prompt_mirror.mirror_root(), PromptMode.installed)
                    logger.info("prompts synced: %s", "; ".join(report.notes))
            except (OSError, ValueError, httpx.HTTPError) as error:
                seen.clear()
                logger.warning("prompt sync failed: %s", error)
            await asyncio.sleep(self.prompt_sync_seconds)

    async def _renew_policy(self) -> None:
        """Keep what a start reads first (`load_policy`: the agent catalog, the tool list) renewed in
        the background, every `policy_renew_seconds`. A failed renewal is logged; starts keep the last
        renewed read for up to `CatalogConnection.RENEWALS_REUSED` periods, then read the server themselves."""
        CatalogConnection.renew_every(self.policy_renew_seconds)
        while True:
            try:
                await asyncio.to_thread(self._renewed_policy)
            except Exception:  # one failed renewal never stops the next; starts then read the server themselves
                logger.exception("agent catalog not renewed")
            await asyncio.sleep(self.policy_renew_seconds)

    @staticmethod
    def _renewed_policy() -> None:
        with CatalogConnection.renewing():
            load_policy()

    async def _heartbeat(self, socket, config: MachineConfig) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            await socket.send(json.dumps({"type": "heartbeat", **await asyncio.to_thread(self._beat, config)}))

    def _beat(self, connected: MachineConfig) -> dict[str, object]:
        """What every hello and heartbeat says: this PC's runtimes, resources, its file roots and
        agent settings as its machine file says NOW (a change made here reaches the server with the
        next beat), and whether work of its own is going on (`busy`)."""
        current = self._current_config(connected)
        return {"runtimes": self._runtimes(current), "accelerators": self._accelerators(), "functions": self._functions(), "resources": self._resources(current.working_directory),
                "file_roots": current.reported_file_roots(), "agent_settings": current.agent_state(self._settings_detail, self._settings_refused).model_dump(mode="json"),
                "busy": self.busy()}

    @staticmethod
    def _channel_url(server_url: str) -> str:
        return MachineConfig.endpoint_on(server_url, "/v1/machine-channel", socket=True)

    @classmethod
    def report_problem(cls, config: MachineConfig, code: MachineProblemCode, detail: str = "", lines: tuple[str, ...] = (), secrets: tuple[str, ...] = ()) -> bool:
        """Tells its server why this computer is not connected (`MachineProblem`, shown on its page):
        over HTTPS with its machine token, which still answers when the channel does not. Best
        effort, credentials masked: False when the server did not take it. The same problem also
        prepares an error report from `lines` (what led to it) and asks its owner on that page
        whether to send it (`error_reports`): nothing of the lines leaves before his yes."""
        problem = MachineProblem(code=code, detail=redact(detail, (config.token.get_secret_value(),)).strip()[-500:])
        # The question's line is galaius's own last line, never `detail`: on an install it is the service
        # log's raw end, which a program the PC ran may have written (threat model S1). Never raises.
        cls.error_reports().prepare(config, code, lines[-1] if lines else code, lines, secrets)
        try:
            response = httpx.post(config.endpoint("/v1/machine/problem"), json=problem.model_dump(mode="json", exclude_none=True), headers=config.authorization, timeout=10)
        except httpx.HTTPError:
            return False
        return response.status_code == 204

    @classmethod
    def error_reports(cls) -> MachineErrorReports:
        """This computer's error report drafts, beside its machine file."""
        return MachineErrorReports(folder=cls.default_config_path().parent / "error-reports")

    async def _deliver_reports(self, config: MachineConfig) -> None:
        """Every `deliver_seconds`, the report its owner accepted on the PC's page goes up (`deliver`)."""
        while True:
            try:
                if await asyncio.to_thread(self.error_reports().deliver, config):
                    logger.info("error report sent: its owner accepted it on this computer's page")
            except Exception:  # one failed look never stops the next
                logger.exception("delivering an error report failed")
            await asyncio.sleep(self.deliver_seconds)


def shell_path(current: str, shell: str | None = None, timeout: float = 10) -> str:
    """The owner's shell PATH, ahead of `current`, deduplicated.

    A user service starts with the service manager's PATH, not the shell's, so a CLI placed on
    PATH by a shell rc file (nvm's node bin, an npm prefix) is invisible to `shutil.which`: an
    installed codex went unreported. The shell is asked once, interactive + login (rc files hold
    those additions), detached from any terminal and bounded by `timeout`; rc-file noise around
    the markers is ignored, and any failure keeps `current` unchanged."""
    marker = "__galaius_path__"
    try:
        completed = subprocess.run(
            [shell or os.environ.get("SHELL") or "/bin/sh", "-ilc", f'printf "{marker}%s{marker}" "$PATH"'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return current
    parts = completed.stdout.split(marker)
    found = parts[1] if len(parts) >= 3 else ""
    return ":".join(dict.fromkeys(entry for entry in (*found.split(":"), *current.split(":")) if entry))


async def connect_command(server_url: str | None, workspace_id: UUID | None, machine_id: UUID | None, token: str | None, permission_ceiling: PermissionLevel, working_directory: Path | None, serve: bool) -> None:
    runner = MachineRunner()
    try:
        if server_url is None and workspace_id is None and machine_id is None and token is None:
            config = runner.load()
        else:
            if server_url is None or workspace_id is None or machine_id is None:
                raise ValueError("setup needs server URL, workspace ID, and machine ID")
            secret = token or getpass.getpass("Machine token: ")
            config = MachineConfig(server_url=server_url, workspace_id=workspace_id, machine_id=machine_id, token=secret, permission_ceiling=permission_ceiling, working_directory=working_directory or Path.cwd())
            runner.save(config)
        if not serve:
            print(f"Saved machine configuration to {runner.config_path}; enable the galaius-machine user service to keep it connected.")
            return
        os.environ["PATH"] = shell_path(os.environ.get("PATH", ""))
        # The service's journal gets one JSON object per line, the same fields the server stores.
        printer = logging.StreamHandler(sys.stderr)
        printer.setFormatter(JsonLines())
        package = logging.getLogger("galaius")
        package.addHandler(printer)
        package.setLevel(logging.INFO)
        await runner.connect(config)
    except (OSError, PermissionError, ValidationError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
