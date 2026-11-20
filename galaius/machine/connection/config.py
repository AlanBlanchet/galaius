"""The enrolled machine's settings file."""

import stat
from pathlib import Path, PurePosixPath
from typing import Self
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID
import httpx
from pydantic import ConfigDict, Field, SecretStr, TypeAdapter, ValidationError, field_validator, model_validator
from galaius_core import PROJECTS_ROOT, MachineAgentSettings, MachineAgentSettingsState, MachineAgentSettingsUpdate, PlaceLevel, PlacePath
from galaius import USER_AGENT
from galaius.workflows.functions import PermissionLevel
from galaius.host.paths import UserPaths
from galaius.upgrade.store import RuntimeStore
from galaius.host.pinned_directory import PinnedDirectory
from galaius.machine.places.levels import LEVEL_RANK, PlaceMap, split


PLACE_PATH: TypeAdapter[str] = TypeAdapter(PlacePath)


class MachineConfig(MachineAgentSettings):
    model_config = ConfigDict(extra="forbid", frozen=True)
    server_url: str
    workspace_id: UUID
    machine_id: UUID
    token: SecretStr = Field(min_length=32, max_length=256)
    permission_ceiling: PermissionLevel
    working_directory: Path
    #: Levels per folder (relative to `working_directory`; `galaius.machine.places.levels`): what workflows, the
    #: Data screen and agents may do there. Set HERE by the owner (`galaius machine places`), or
    #: from the web, at once either way: the web never opens the home folder itself, anything
    #: outside it, a hidden name or a credential store (`PlaceMap.web_refusal`). A folder not named is hidden.
    places: dict[str, PlaceLevel] = Field(default_factory=dict, max_length=256)
    #: Whether the web may list file names, and folders outside the home folder, to pick levels
    #: from (names only), set here only (`galaius machine browse on`), off by default. Folder names
    #: inside the home folder are listed either way.
    browse: bool = False
    #: Whether agents started here run inside the OS fence built from the levels (`galaius.isolation.fence`),
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
        return PlaceMap(working_directory=self.working_directory, levels=self.places, internal=self._internal(), projects=self.projects_root())

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
        """The usable agent roots, by the name the web shows (relative to the working directory), and
        the projects folder under `PROJECTS_ROOT` while it is safe (`projects_root`)."""
        base = self.working_directory.resolve()
        named = {root.relative_to(base).as_posix(): root for root in self.usable_agent_roots()[0]}
        return named | ({PROJECTS_ROOT: projects} if (projects := self.projects_root()) is not None else {})

    def projects_root(self) -> Path | None:
        """`UserPaths.projects`, holding the projects put here from the web (`PROJECTS_ROOT` on the wire; made
        by the first one), apart from the runner's own folders; None while anything but a plain folder sits
        there (a link, a junction or a file is never followed) or it is, holds or sits in one of the runner's
        own folders (the galaius folder it is made in excepted)."""
        declared = UserPaths.projects()
        if any(declared == folder or declared in folder.parents or (folder in declared.parents and folder != declared.parent) for folder in self._internal()):
            return None
        home = Path.home().resolve()
        for folder in (declared, *(parent for parent in declared.parents if home in parent.parents)):   # `~/galaius` too: a link or a file there is never followed
            try:
                facts = folder.lstat()
            except (FileNotFoundError, NotADirectoryError):   # not there yet, or a file above it (judged as that parent)
                continue
            if not stat.S_ISDIR(facts.st_mode) or PinnedDirectory.link_like(facts):
                return None
        return declared if not declared.exists() or declared.resolve() == declared else None

    def _internal(self) -> tuple[Path, ...]:
        """The runner's own folders (its settings, its installed runtimes, its data): code every
        long-lived process runs, never opened to a workflow or an agent."""
        base = self.working_directory.resolve()
        return (base / ".galaius", UserPaths.config().resolve(), UserPaths.data().resolve(), RuntimeStore.default().root.resolve(),
                UserPaths.agents().resolve(), UserPaths.subscriptions().resolve())

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

    def page(self, http: httpx.Client) -> str:
        """This computer's page in the web app, where its settings live: the app is where the
        server serves it (`/v1/install` origin, e.g. under /plateform), else at the server's root."""
        try:
            app = str(http.get(self.endpoint("/v1/install")).json()["origin"]).rstrip("/")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            app = self.server_url.rstrip("/")
        return f"{app}/#data?computer={self.machine_id.hex}"

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
