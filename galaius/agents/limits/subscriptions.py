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
import time
from datetime import datetime
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import ClassVar, Self
from uuid import UUID, uuid4

from galaius_core import AgentProvider as ProviderName, RunSubscription as WireRunSubscription, SubscriptionAllowance, SubscriptionCap, SubscriptionLimits
from galaius_core import CapReached, LoginIdentity, Refused, SubscriptionChoice, SubscriptionRefusals, SubscriptionUsage, SubscriptionUsageReport, UsageWindow
from pydantic import BaseModel, ConfigDict

from galaius.agents.limits import held, quota
from galaius.agents.providers import PROVIDERS, AgentProvider
from galaius.host.atomic_file import replace_held_open
from galaius.host.paths import UserPaths

logger = logging.getLogger(__name__)

#: Each subscription folder here whose state does not read, by name, with why (`Subscription.all`): said once per change,
#: never at each look (2026-11-03: one folder never signed in on this PC, 9,151 lines in 3 h).
_UNREADABLE: dict[str, str] = {}


class SubscriptionRefused(Refused[SubscriptionRefusals]):
    """A start, turn or child a subscription cannot take: its reason one of `SubscriptionRefusals`."""


class SubscriptionCapped(SubscriptionRefused):
    """A subscription at one of its caps: `cap` says which window, the reading and when it reopens."""

    def __init__(self, cap: SubscriptionCap) -> None:
        reopens = f"; it reopens {cap.resets_at.isoformat(timespec='minutes')}" if cap.resets_at is not None else ""
        super().__init__(CapReached(cap=cap),
                         f"this subscription is at {cap.used_percent:g}% of its {cap.window.replace('_', ' ')} window, its cap is {cap.cap_percent}%{reopens}")

    @property
    def cap(self) -> SubscriptionCap:
        """The cap it reached, read off its refusal."""
        assert isinstance(self.refusal, CapReached)  # built from one alone
        return self.refusal.cap


class SubscriptionState(BaseModel):
    """What galaius keeps in a subscription's folder (`Subscription.STATE`): its provider, its last
    usage reading per window and the login its CLI last named, and what was last reported of each to the server."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderName
    usage: tuple[UsageWindow, ...] = ()
    reported: tuple[UsageWindow, ...] = ()
    reported_at: datetime | None = None
    identity: LoginIdentity = LoginIdentity()
    reported_identity: LoginIdentity = LoginIdentity()


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
    #: Where a signed-out login's sessions are kept (`remove`), one folder per sign-out shaped as a login folder: a
    #: transcript is never deleted with its login, a run pinned there resumes from it (`session_homes`).
    KEPT: ClassVar[str] = ".signed-out"
    #: The files a CLI keeps its tokens in, inside its home: their values are redacted wherever a run's
    #: output goes (`credentials`).
    CREDENTIALS: ClassVar[tuple[str, ...]] = (".credentials.json", "auth.json")
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
        if made.state_path.exists():
            cls.of(subscription_id).runs(provider)
        cls._private(cls.root())
        cls._private(made.folder)
        for name, text in made.program.first_files.items():
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
        """Every subscription here whose state file reads; one that does not is logged once per change of why
        (`_UNREADABLE`), never silently skipped."""
        found, unreadable = [], {}
        for folder in cls.folders():
            try:
                found.append(cls.of(UUID(folder.name)))
            except (ValueError, SubscriptionRefused) as error:
                unreadable[folder.name] = str(error)
                if _UNREADABLE.get(folder.name) != unreadable[folder.name]:
                    logger.warning("subscription folder %s has no readable state: %s", folder.name, error)
        _UNREADABLE.clear()
        _UNREADABLE.update(unreadable)
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

    def runs(self, provider: ProviderName) -> Self:
        """This login, refused when it is another CLI's: the ONE check every start, move and folder of a subscription
        makes. A login holds one CLI's sessions, in that CLI's own format: a conversation never moves between CLIs."""
        if provider != self.provider:
            raise SubscriptionRefused("pinned_elsewhere", f"subscription {self.id} is a {self.provider} login, never {provider}: "
                                      "a conversation stays with its CLI (each keeps its sessions in its own format)")
        return self

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
        return program.auth_home(cls.unpinned(environment))

    @classmethod
    def unpinned(cls, environment: Mapping[str, str]) -> dict[str, str]:
        """`environment` without any CLI login variable naming a folder under `root` (a subscription's, a closed one): what a
        turn on this PC's own login runs with, whoever's process asked for it (a pinned run messaging an unpinned one)."""
        root = cls.root().resolve()
        logins = {name for provider in PROVIDERS.values() for name in provider.auth_home_env}
        return {key: value for key, value in environment.items() if not (key in logins and Path(value).resolve().is_relative_to(root))}

    @classmethod
    def session_homes(cls, program: AgentProvider, environment: Mapping[str, str]) -> tuple[Path, ...]:
        """Every folder of this PC that may hold a session of `program`, each shaped as its login folder: its default
        login's, each subscription's, each signed-out login's kept sessions (`KEPT`)."""
        kept = cls.root() / cls.KEPT
        return (cls.default_home(program, environment), *cls.folders(), *(sorted(path for path in kept.iterdir() if path.is_dir()) if kept.is_dir() else ()))

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
        inside, with their times, for it to continue there (contract A9): each a subscription's folder, this PC's own
        login's or a kept one's (`session_homes`), one CLI's (the caller checked). Never a credential, never another file,
        never over a newer file; refused when none is in `source`. How many were copied."""
        files = [path for path in program.session_files(source, cwd, session) if path.is_file() and not path.is_symlink() and path.name not in cls.CREDENTIALS]
        if not files:
            raise SubscriptionRefused("session_missing", f"session {session} is not in login folder {source}: it cannot continue elsewhere")
        copied = 0
        for path in files:
            copy, held = target / path.relative_to(source), path.stat()
            if copy.is_file() and copy.stat().st_mtime > held.st_mtime:
                continue
            copy.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            cls._replace(copy, path.read_bytes())
            os.utime(copy, ns=(held.st_atime_ns, held.st_mtime_ns))
            copied += 1
        return copied

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

    def identify(self, identity: LoginIdentity) -> None:
        """Its CLI named the login its folder holds now (`AgentProvider.login_status`); one naming no account keeps the
        account last named for the same login (a held login's status names none, its sign-in's screen may have)."""
        with self.WRITING:
            state = self.state()
            if not identity.account and identity.login == state.identity.login:
                identity = identity.model_copy(update={"account": state.identity.account})
            if state.identity != identity:
                self._write(state.model_copy(update={"identity": identity}))

    def report_due(self, now: datetime) -> SubscriptionUsage | None:
        """Its usage and login when the server should hear them now: its usage moved (`SubscriptionUsageReport.due`), or
        its CLI names another login than the one last reported."""
        state = self.state()
        due = state.usage and SubscriptionUsageReport.due(state.usage, state.reported, state.reported_at, now)
        named = state.identity.login and state.identity != state.reported_identity
        return SubscriptionUsage(subscription_id=self.id, windows=state.usage, **state.identity.model_dump()) if due or named else None

    def reported(self, usage: SubscriptionUsage, now: datetime) -> None:
        with self.WRITING:
            self._write(self.state().model_copy(update={"reported": usage.windows, "reported_at": now,
                                                        "reported_identity": LoginIdentity(account=usage.account, login=usage.login)}))

    @property
    def held(self) -> str | None:
        """Its held login (`held.credential_in`); None: a program holding none, or none here."""
        form = self.program.held
        return held.credential_in(self.folder, form) if form is not None else None

    def hold(self, credential: str) -> bool:
        """`credential` kept as its held login; whether it changed (`held.hold`). Its program must hold one."""
        form = self.program.held
        assert form is not None, f"{self.provider} holds no login"
        return held.hold(self.folder, form, credential)

    def forget_held(self) -> None:
        """Its held login removed (`held.forget`), the rest of its folder kept."""
        if (form := self.program.held) is not None:
            held.forget(self.folder, form)

    @classmethod
    def credentials_in(cls, folder: Path) -> tuple[str, ...]:
        """The token values a CLI keeps in `folder` now, read again at each call (a refresh changes
        them), and its held login: what a run's output never shows."""
        values = [token for provider in PROVIDERS.values() if (form := provider.held) is not None and form.variable is not None
                  and (token := held.credential_in(folder, form)) is not None]
        for name in cls.CREDENTIALS:
            try:
                values += cls._strings(json.loads((folder / name).read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return tuple(value for value in dict.fromkeys(values) if len(value) >= cls.SECRET_LENGTH)

    def remove(self) -> None:
        """Its folder removed, its program's session folders (`FencedCli.sessions`) first moved to a kept folder of their
        own (`KEPT`): a run pinned here resumes from them once moved or signed in again, never « No conversation found »
        (2026-11-09: his web sign-out and sign-in again of a login removed his main conversation's transcript)."""
        sessions = [self.folder / name for name in (self.program.fenced.sessions if self.program.fenced is not None else ()) if (self.folder / name).is_dir()]
        if sessions:
            kept = self.root() / self.KEPT / f"{self.id}.{time.time_ns()}"
            self._private(kept.parent)
            self._private(kept)
            for folder in sessions:
                folder.rename(kept / folder.name)
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
        replace_held_open(temporary, path)

    def _write(self, state: SubscriptionState) -> None:
        """Through a temporary file of its own (two writers never rename each other's half-written one)."""
        temporary = self.state_path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(state.model_dump_json(), encoding="utf-8")
        temporary.replace(self.state_path)


class PassedOver(BaseModel):
    """Why a launch passed over subscriptions of one provider: the caps they were at, the English of each refusal, the
    logins at their usage limit (`held`, waited on should nothing else start)."""

    model_config = ConfigDict(frozen=True)

    caps: tuple[SubscriptionCap, ...] = ()
    said: str = ""
    held: tuple[UUID | None, ...] = ()


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
        (`bound`); none: why each was passed over (`PassedOver`). A login at its usage limit waits for its reopening; one
        whose sign-in was lost is asked again after the others, never ahead of a spare that works (2026-10-31: his second
        Claude account sat unused while the first waited for its weekly reset)."""
        held, lost, ready = [], [], []
        for option in offered:
            pause = quota.login_pause(provider, subscription=option.subscription_id)
            (ready if pause is None else held if pause.reason == "usage_limit" else lost).append(option)
        caps, said, waiting = [], [], tuple(option.subscription_id for option in held)
        for option in ready + lost:
            try:
                return option, option.bound(environment, provider), PassedOver(held=waiting)
            except SubscriptionCapped as capped:
                caps.append(capped.cap)
                said.append(str(capped))
            except SubscriptionRefused as refused:
                said.append(str(refused))
        return None, dict(environment), PassedOver(caps=tuple(caps), said="; ".join(said)[:400], held=waiting)

    @classmethod
    def closed(cls, providers: Iterable[str]) -> dict[str, Path]:
        """`providers`' closed login folders (`homes` of a lent run): a fenced default-login run of another CLI never
        reaches the subscription folders a start named for them."""
        lent = cls(lent=True).homes()
        return {name: lent[name] for name in providers}

    @classmethod
    def resolve(cls, asked: Self | None, *, parent: Self | None, child: bool, environment: Mapping[str, str]) -> Self | None:
        """What a new run is pinned to: what was asked; a child asking none its parent run's (from the run records and
        the process tree, never from what its environment names). A child of a lent run asking any login, or an
        environment naming a folder of another subscription, is refused."""
        if child and parent is not None and parent.lent and asked is not None and (asked.subscription_id, asked.lent) != (parent.subscription_id, parent.lent):
            raise SubscriptionRefused("lent", "a child of a member's run on a lent PC runs on none of its CLI logins, as its parent")
        pinned = asked if asked is not None or not child else parent
        if child and pinned is not None:
            pinned = pinned.model_copy(update={"capped": None})
        # A child's environment is its parent's: the folder it names is the parent's login, replaced by `bound`.
        named = (pinned, parent if child else None)
        expected = frozenset(Path(folder).resolve() for one in named if one is not None for folder in one.overlay().values())
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
        if self.subscription_id is not None and provider is not None:
            Subscription.of(self.subscription_id).runs(provider)
        self.check()
        if self.subscription_id is not None:
            Subscription.of(self.subscription_id).adopt(environment)
        return {**environment, **self.overlay()}
