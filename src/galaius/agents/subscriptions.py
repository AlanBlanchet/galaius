"""Coding-agent subscriptions signed in on this PC, each in its own folder (`UserPaths.subscriptions()/<id>/`,
0700): the CLI's home for that login, named to it through `AgentProvider.auth_home_env`. The user's own
default logins (`~/.claude`, `~/.codex`) are never read or written here. A run is pinned to one
subscription (`RunSubscription`, on its record): every turn and every child runs on it, caps are checked
before each start and on each usage reading, and its usage is kept here, last value per window."""

import json
import logging
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import ClassVar, Self
from uuid import UUID, uuid4

from galaius_core import AgentProvider as ProviderName, RunSubscription as WireRunSubscription, SubscriptionAllowance, SubscriptionCap, SubscriptionLimits
from galaius_core import Refused, SubscriptionChoice, SubscriptionRefusal, SubscriptionUsage, SubscriptionUsageReport, UsageWindow
from pydantic import BaseModel, ConfigDict

from galaius.agents.providers import PROVIDERS, AgentProvider
from galaius.paths import UserPaths

logger = logging.getLogger(__name__)


class SubscriptionRefused(Refused[SubscriptionRefusal]):
    """A start, turn or child a subscription cannot take: its reason a `SubscriptionRefusalReason`."""


class SubscriptionCapped(SubscriptionRefused):
    """A subscription at one of its caps: `cap` says which window, the reading and when it reopens."""

    def __init__(self, cap: SubscriptionCap) -> None:
        reopens = f"; it reopens {cap.resets_at.isoformat(timespec='minutes')}" if cap.resets_at is not None else ""
        super().__init__(SubscriptionRefusal(reason="capped", cap=cap),
                         f"this subscription is at {cap.used_percent:g}% of its {cap.window.replace('_', ' ')} window, its cap is {cap.cap_percent}%{reopens}")


class SubscriptionState(BaseModel):
    """What galaius keeps in a subscription's folder (`Subscription.STATE`): its provider, its last
    usage reading per window, and the reading last reported to the server."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderName
    usage: tuple[UsageWindow, ...] = ()
    reported: tuple[UsageWindow, ...] = ()
    reported_at: datetime | None = None


class Subscription(BaseModel):
    """One subscription's folder on this PC. Its path never changes once created: Claude Code on macOS
    keys its keychain entry by it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    provider: ProviderName

    STATE: ClassVar[str] = "galaius-subscription.json"
    #: Held around every read-then-write of a state file: the runner's threads (its usage ask, its stream reader, its report) each write it.
    WRITING: ClassVar[threading.Lock] = threading.Lock()
    #: Per CLI, the folder its login is pointed at where none of its logins may answer (another CLI than
    #: a run's subscription's; every CLI for a member's run on a lent PC): empty, never writable.
    CLOSED: ClassVar[str] = ".closed"
    #: The files a CLI keeps its tokens in, inside its home: their values are redacted wherever a run's
    #: output goes (`credentials`).
    CREDENTIALS: ClassVar[tuple[str, ...]] = (".credentials.json", "auth.json")
    #: Written into a new folder, per provider: Codex keeps its login in a file there, never in the
    #: system keyring, which a second login would share.
    FIRST_FILES: ClassVar[dict[str, dict[str, str]]] = {"codex": {"config.toml": 'cli_auth_credentials_store = "file"\n'}}
    #: The user settings keys a subscription folder takes from the default login's at each launch
    #: (`adopt`): what its runs may do and the hooks that run, never `env`, `apiKeyHelper` or another key.
    ADOPTED_SETTINGS: ClassVar[tuple[str, ...]] = ("hooks", "permissions")
    #: What of the default folder a hook's script is never taken from: the login's own state, its
    #: history and its sessions.
    NEVER_ADOPTED: ClassVar[frozenset[str]] = frozenset({".claude.json", "history.jsonl", "projects", "sessions"})
    #: Shorter strings of a credential file are not secrets (a type, a plan name).
    SECRET_LENGTH: ClassVar[int] = 16

    @staticmethod
    def root() -> Path:
        return UserPaths.subscriptions()

    @classmethod
    def create(cls, subscription_id: UUID, provider: ProviderName) -> Self:
        """The folder of `subscription_id` for `provider`, made (0700) when new; a folder of another provider is refused."""
        made = cls(id=subscription_id, provider=provider)
        if made.state_path.exists() and made.state().provider != provider:
            raise SubscriptionRefused("pinned_elsewhere", f"subscription {subscription_id} is a {made.state().provider} login on this PC, not {provider}")
        cls._private(cls.root())
        cls._private(made.folder)
        for name, text in cls.FIRST_FILES.get(provider, {}).items():
            if not (made.folder / name).exists():
                (made.folder / name).write_text(text, encoding="utf-8")
        if not made.state_path.exists():
            made._write(SubscriptionState(provider=provider))
        return made

    @classmethod
    def of(cls, subscription_id: UUID) -> Self:
        path = cls.root() / str(subscription_id) / cls.STATE
        try:
            state = SubscriptionState.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SubscriptionRefused("not_signed_in", f"subscription {subscription_id} is not signed in on this PC; sign it in here from its page on the web") from None
        return cls(id=subscription_id, provider=state.provider)

    @classmethod
    def folders(cls) -> tuple[Path, ...]:
        """Every subscription folder here (named by a UUID), whatever its state file says."""
        if not cls.root().is_dir():
            return ()
        return tuple(folder for folder in sorted(cls.root().iterdir()) if folder.is_dir() and cls._uuid(folder.name) is not None)

    @classmethod
    def all(cls) -> tuple[Self, ...]:
        """Every subscription here whose state file reads; one that does not is logged, never silently skipped."""
        found = []
        for folder in cls.folders():
            try:
                found.append(cls.of(UUID(folder.name)))
            except (ValueError, SubscriptionRefused) as error:
                logger.warning("subscription folder %s has no readable state: %s", folder.name, error)
        return tuple(found)

    @classmethod
    def tokens(cls) -> tuple[str, ...]:
        """The tokens kept in every subscription folder (`credentials`), whatever its state file says,
        longest first (a token inside another stays masked)."""
        return tuple(sorted({value for folder in cls.folders() for value in cls.credentials_in(folder)}, key=len, reverse=True))

    @classmethod
    def closed_overlay(cls) -> dict[str, str]:
        """Every CLI's login variable at its own closed folder (`CLOSED`), made empty and read-only."""
        overlay = {}
        for provider in PROVIDERS.values():
            folder = cls.root() / cls.CLOSED / provider.name
            if not folder.is_dir():
                cls._private(folder.parent)
                folder.mkdir(mode=0o500)
            overlay |= {name: str(folder) for name in provider.auth_home_env}
        return overlay

    @classmethod
    def named_in(cls, environment: Mapping[str, str]) -> frozenset[Path]:
        """The folders under `root` the CLI login variables of `environment` point at."""
        root = cls.root().resolve()
        named = (Path(environment[name]).resolve() for provider in PROVIDERS.values() for name in provider.auth_home_env if environment.get(name))
        return frozenset(path for path in named if path.is_relative_to(root))

    @property
    def folder(self) -> Path:
        return self.root() / str(self.id)

    @property
    def state_path(self) -> Path:
        return self.folder / self.STATE

    @property
    def program(self) -> AgentProvider:
        return PROVIDERS[self.provider]

    def adopt(self, environment: Mapping[str, str]) -> None:
        """The owner's own config, as his default login of this CLI has it now, written into this
        folder before a launch: the user-level instructions file, and of the settings file only
        `ADOPTED_SETTINGS` (the folder's other keys kept), every path into a CLI's default folder
        rehomed here with the scripts there it names (`_rehome`). Nothing else of the default homes is
        read (no credential, `.claude.json`, session or history) and nothing is ever written there."""
        default = self.default_home(self.program, environment)
        login = {name: str(default) for name in self.program.auth_home_env}
        if (instructions := self.program.user_instructions({**environment, **login})) is not None and instructions.is_file():
            self._replace(self.folder / instructions.name, instructions.read_bytes())
        if self.program.settings_file is not None and (settings := default / self.program.settings_file).is_file():
            owner = json.loads(settings.read_text(encoding="utf-8"))
            mine = self.folder / self.program.settings_file
            kept = json.loads(mine.read_text(encoding="utf-8")) if mine.is_file() else {}
            adopted = self._rehome({key: owner[key] for key in self.ADOPTED_SETTINGS if key in owner}, environment)
            kept = {key: value for key, value in kept.items() if key not in self.ADOPTED_SETTINGS}
            self._replace(mine, (json.dumps({**kept, **adopted}, indent=2) + "\n").encode())

    @classmethod
    def default_home(cls, program: AgentProvider, environment: Mapping[str, str]) -> Path:
        """`program`'s default login folder: its variable's, unless that names a subscription folder
        (a launch inside a pinned run), then home's own."""
        root = cls.root().resolve()
        unpinned = {key: value for key, value in environment.items() if not (key in program.auth_home_env and Path(value).resolve().is_relative_to(root))}
        return program.auth_home(unpinned)

    def _rehome(self, value: dict, environment: Mapping[str, str]) -> dict:
        """`value` with every path into any CLI's default folder (`~/<folder>/`, `$HOME/…`,
        `${HOME}/…`) naming the same path in this folder, and each folder holding a file it names
        copied here from that CLI's default folder (`_adopt_folder`): a fenced run reaches no other."""
        text = json.dumps(value)
        for program in PROVIDERS.values():
            if not program.home_folder:
                continue
            spellings = tuple(f"{home}/{program.home_folder}/" for home in ("~", "$HOME", "${HOME}"))
            for relative in dict.fromkeys(re.findall("(?:" + "|".join(map(re.escape, spellings)) + r")([\w./-]+)", text)):
                self._adopt_folder(self.default_home(program, environment), Path(relative))
            for spelling in spellings:
                text = text.replace(spelling, f"{self.folder.as_posix()}/")
        return json.loads(text)

    def _adopt_folder(self, default: Path, relative: Path) -> None:
        """The folder holding the file `relative` names in `default`, copied here whole (a script reads
        files beside it), or that file alone when it sits at the folder's top; never a credential, the
        login's own state, its history or its sessions, never anything outside `default`."""
        if ".." in relative.parts or relative.parts[0] in self.NEVER_ADOPTED:
            return
        source = default / relative
        files = [source] if len(relative.parts) == 1 else [path for path in source.parent.rglob("*") if path.is_file()] if source.parent.is_dir() else []
        for path in files:
            inside = path.relative_to(default)
            if (path.is_symlink() or not path.is_file() or path.name in self.CREDENTIALS or inside.parts[0] in self.NEVER_ADOPTED
                    or not path.resolve().is_relative_to(default.resolve())):
                continue
            target = self.folder / inside
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._replace(target, path.read_bytes())
            target.chmod(path.stat().st_mode & 0o700)

    @classmethod
    def carry_session(cls, program: AgentProvider, source: Path, target: Path, cwd: Path, session: str) -> int:
        """Session `session`'s files (`AgentProvider.session_files`) copied from login folder `source` to `target`, same place
        inside, for it to continue there (contract A9): each a subscription's folder or this PC's own login's, one CLI's
        (the caller checked). Never a credential, never another file; refused when none is in `source`. How many were copied."""
        files = [path for path in program.session_files(source, cwd, session) if path.is_file() and not path.is_symlink() and path.name not in cls.CREDENTIALS]
        if not files:
            raise SubscriptionRefused("session_missing", f"session {session} is not in login folder {source}: it cannot continue elsewhere")
        for path in files:
            copy = target / path.relative_to(source)
            copy.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            cls._replace(copy, path.read_bytes())
        return len(files)

    def overlay(self) -> dict[str, str]:
        """What a child of this subscription's CLI runs with on top of its environment."""
        return {name: str(self.folder) for name in self.program.auth_home_env}

    def state(self) -> SubscriptionState:
        return SubscriptionState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    def usage(self) -> tuple[UsageWindow, ...]:
        return self.state().usage

    def observe(self, windows: tuple[UsageWindow, ...], *, whole: bool = False) -> tuple[UsageWindow, ...]:
        """`windows` (oldest first) replace the readings of the same windows, or, `whole` (its CLI's full answer now, none
        when signed out), every reading; the usage now."""
        with self.WRITING:
            state = self.state()
            latest = {window.window: window for window in (*(() if whole else state.usage), *windows)}
            usage = tuple(latest.values())
            self._write(state.model_copy(update={"usage": usage}))
        return usage

    def check(self, limits: SubscriptionLimits, allowance: SubscriptionAllowance | None = None) -> None:
        """Refused (`SubscriptionCapped`) while a window of its usage is at its cap."""
        if (cap := limits.reached(self.usage(), allowance)) is not None:
            raise SubscriptionCapped(cap)

    def report_due(self, now: datetime) -> SubscriptionUsage | None:
        """Its usage when the server should hear it now (`SubscriptionUsageReport.due`)."""
        state = self.state()
        due = state.usage and SubscriptionUsageReport.due(state.usage, state.reported, state.reported_at, now)
        return SubscriptionUsage(subscription_id=self.id, windows=state.usage) if due else None

    def reported(self, usage: SubscriptionUsage, now: datetime) -> None:
        with self.WRITING:
            self._write(self.state().model_copy(update={"reported": usage.windows, "reported_at": now}))

    @classmethod
    def credentials_in(cls, folder: Path) -> tuple[str, ...]:
        """The token values a CLI keeps in `folder` now, read again at each call (a refresh changes
        them): what a run's output never shows."""
        values: list[str] = []
        for name in cls.CREDENTIALS:
            try:
                values += cls._strings(json.loads((folder / name).read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return tuple(value for value in dict.fromkeys(values) if len(value) >= cls.SECRET_LENGTH)

    def remove(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)

    @classmethod
    def _strings(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [found for item in value.values() for found in cls._strings(item)]
        if isinstance(value, list):
            return [found for item in value for found in cls._strings(item)]
        return []

    @staticmethod
    def _uuid(name: str) -> UUID | None:
        try:
            return UUID(name)
        except ValueError:
            return None

    @staticmethod
    def _private(folder: Path) -> None:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)

    @staticmethod
    def _replace(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(content)
        temporary.chmod(0o600)
        temporary.replace(path)

    def _write(self, state: SubscriptionState) -> None:
        """Through a temporary file of its own (two writers never rename each other's half-written one)."""
        temporary = self.state_path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(state.model_dump_json(), encoding="utf-8")
        temporary.replace(self.state_path)


class PassedOver(BaseModel):
    """Why a launch passed over every subscription of one provider: the caps they were at, the English of each refusal."""

    model_config = ConfigDict(frozen=True)

    caps: tuple[SubscriptionCap, ...] = ()
    said: str = ""


class RunSubscription(WireRunSubscription):
    """`galaius_core.RunSubscription` on a run's record, with what the PC does with it: the login
    folders its CLIs run with, its caps checked, a child or a message held to it."""

    @classmethod
    def of(cls, choice: SubscriptionChoice) -> Self:
        """What a web start's candidate of `choice.provider` runs pinned to."""
        return cls(subscription_id=choice.subscription_id, limits=choice.limits)

    @classmethod
    def first_under_cap(cls, offered: list[Self], environment: Mapping[str, str], provider: str) -> tuple[Self | None, dict[str, str], PassedOver]:
        """The first of `offered` (one provider's, his order) a launch of `provider` can run on now, with its environment
        (`bound`); none: why each was passed over (`PassedOver`: the caps reached, the English of the first refusal)."""
        caps, said = [], []
        for option in offered:
            try:
                return option, option.bound(environment, provider), PassedOver()
            except SubscriptionCapped as capped:
                caps.append(capped.refusal.cap)
                said.append(str(capped))
            except SubscriptionRefused as refused:
                said.append(str(refused))
        return None, dict(environment), PassedOver(caps=tuple(caps), said="; ".join(said)[:400])

    @classmethod
    def closed(cls, providers: Iterable[str]) -> dict[str, Path]:
        """`providers`' closed login folders (`homes` of a lent run): a fenced default-login run of another CLI never
        reaches the subscription folders a start named for them."""
        lent = cls(lent=True).homes()
        return {name: lent[name] for name in providers}

    @classmethod
    def resolve(cls, asked: Self | None, *, parent: Self | None, child: bool, environment: Mapping[str, str]) -> Self | None:
        """What a new run is pinned to: a child's is its parent run's (from the run records and the
        process tree, never from what its environment names), anything else what was asked. A child
        asking another, or an environment naming a folder of another subscription, is refused."""
        pinned = parent if child else asked
        if child and asked is not None and (asked.subscription_id, asked.lent) != ((parent.subscription_id, parent.lent) if parent is not None else (None, False)):
            raise SubscriptionRefused("pinned_elsewhere", "a child runs on its parent run's subscription, never another")
        if child and pinned is not None:
            pinned = pinned.model_copy(update={"capped": None})
        expected = frozenset() if pinned is None else frozenset(Path(folder).resolve() for folder in pinned.overlay().values())
        if not Subscription.named_in(environment) <= expected:
            raise SubscriptionRefused("pinned_elsewhere", "this run's environment names another subscription's folder; a run uses only the subscription it is pinned to")
        return pinned

    def overlay(self) -> dict[str, str]:
        """Every CLI's login variable for this run: its subscription's CLI at its folder, every other
        CLI (every CLI, lent) at its closed folder, where no login answers."""
        own = Subscription.of(self.subscription_id).overlay() if self.subscription_id is not None else {}
        return {**Subscription.closed_overlay(), **own}

    def homes(self) -> dict[str, Path]:
        """Per CLI, the login folder a fenced run of it binds (`FenceSpec.homes`): never a default one."""
        overlay = self.overlay()
        return {provider.name: Path(overlay[provider.auth_home_env[0]]) for provider in PROVIDERS.values() if provider.auth_home_env}

    def check(self) -> None:
        """Refused while its subscription is at a cap (this run's allowance counted)."""
        if self.subscription_id is not None:
            Subscription.of(self.subscription_id).check(self.limits, self.allowance)

    def bound(self, environment: Mapping[str, str], provider: str | None = None) -> dict[str, str]:
        """`environment` for one launch or turn on it (of `provider`'s CLI, refused when its subscription is another's
        login): its caps checked (refused at one), the owner's config adopted into its folder (`Subscription.adopt`),
        every CLI's login variable at its folder or a closed one (`overlay`)."""
        if self.subscription_id is not None and provider is not None and (held := Subscription.of(self.subscription_id)).provider != provider:
            raise SubscriptionRefused("pinned_elsewhere", f"subscription {self.subscription_id} is a {held.provider} login here, never {provider}")
        self.check()
        if self.subscription_id is not None:
            Subscription.of(self.subscription_id).adopt(environment)
        return {**environment, **self.overlay()}
