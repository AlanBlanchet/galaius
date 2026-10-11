"""Coding-agent subscriptions signed in on this PC, each in its own folder (`UserPaths.subscriptions()/<id>/`,
0700): the CLI's home for that login, named to it through `AgentProvider.auth_home_env`. The user's own
default logins (`~/.claude`, `~/.codex`) are never read or written here. A run is pinned to one
subscription (`RunSubscription`, on its record): every turn and every child runs on it, caps are checked
before each start and on each usage reading, and its usage is kept here, last value per window."""

import json
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Mapping
from typing import ClassVar, Self
from uuid import UUID

from galaius_core import AgentProvider as ProviderName, RunSubscription as WireRunSubscription, SubscriptionAllowance, SubscriptionCap, SubscriptionLimits
from galaius_core import SubscriptionUsage, SubscriptionUsageReport, UsageWindow
from pydantic import BaseModel, ConfigDict

from galaius.agents.providers import PROVIDERS, AgentProvider
from galaius.paths import UserPaths

logger = logging.getLogger(__name__)


class SubscriptionRefused(PermissionError):
    """A start, turn or child a subscription cannot take; its message says why, for the owner."""


class SubscriptionCapped(SubscriptionRefused):
    """A subscription at one of its caps: `cap` says which window, the reading and when it reopens."""

    def __init__(self, cap: SubscriptionCap) -> None:
        reopens = f"; it reopens {cap.resets_at.isoformat(timespec='minutes')}" if cap.resets_at is not None else ""
        super().__init__(f"this subscription is at {cap.used_percent:g}% of its {cap.window.replace('_', ' ')} window, its cap is {cap.cap_percent}%{reopens}")
        self.cap = cap


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
    #: Per CLI, the folder its login is pointed at where none of its logins may answer (another CLI than
    #: a run's subscription's; every CLI for a member's run on a lent PC): empty, never writable.
    CLOSED: ClassVar[str] = ".closed"
    #: The files a CLI keeps its tokens in, inside its home: their values are redacted wherever a run's
    #: output goes (`credentials`).
    CREDENTIALS: ClassVar[tuple[str, ...]] = (".credentials.json", "auth.json")
    #: Written into a new folder, per provider: Codex keeps its login in a file there, never in the
    #: system keyring, which a second login would share.
    FIRST_FILES: ClassVar[dict[str, dict[str, str]]] = {"codex": {"config.toml": 'cli_auth_credentials_store = "file"\n'}}
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
            raise SubscriptionRefused(f"subscription {subscription_id} is a {made.state().provider} login on this PC, not {provider}")
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
            raise SubscriptionRefused(f"subscription {subscription_id} is not signed in on this PC; sign it in here from its page on the web") from None
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

    def overlay(self) -> dict[str, str]:
        """What a child of this subscription's CLI runs with on top of its environment."""
        return {name: str(self.folder) for name in self.program.auth_home_env}

    def state(self) -> SubscriptionState:
        return SubscriptionState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    def usage(self) -> tuple[UsageWindow, ...]:
        return self.state().usage

    def observe(self, windows: tuple[UsageWindow, ...]) -> tuple[UsageWindow, ...]:
        """`windows` (oldest first) replace the readings of the same windows; the usage now."""
        state = self.state()
        latest = {window.window: window for window in (*state.usage, *windows)}
        usage = tuple(latest.values())
        self._write(state.model_copy(update={"usage": usage}))
        return usage

    def check(self, limits: SubscriptionLimits, allowance: SubscriptionAllowance | None = None) -> None:
        """Refused (`SubscriptionCapped`) while a window of its usage is at its cap."""
        if (cap := limits.reached(self.usage(), allowance)) is not None:
            raise SubscriptionCapped(cap)

    def report_due(self, now: datetime) -> SubscriptionUsage | None:
        """Its usage when the server should hear it now: a window moved `REPORT_POINTS` or more since
        the last report, or anything changed and `REPORT_SECONDS` passed since it."""
        state = self.state()
        if not state.usage or state.usage == state.reported:
            return None
        last = {window.window: window.used_percent for window in state.reported}
        moved = any(abs(window.used_percent - last.get(window.window, -100.0)) >= SubscriptionUsageReport.REPORT_POINTS for window in state.usage)
        waited = state.reported_at is None or (now - state.reported_at).total_seconds() >= SubscriptionUsageReport.REPORT_SECONDS
        return SubscriptionUsage(subscription_id=self.id, windows=state.usage) if moved or waited else None

    def reported(self, usage: SubscriptionUsage, now: datetime) -> None:
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

    def _write(self, state: SubscriptionState) -> None:
        temporary = self.state_path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(state.model_dump_json(), encoding="utf-8")
        temporary.replace(self.state_path)


class RunSubscription(WireRunSubscription):
    """`galaius_core.RunSubscription` on a run's record, with what the PC does with it: the login
    folders its CLIs run with, its caps checked, a child or a message held to it."""

    @classmethod
    def resolve(cls, asked: Self | None, *, parent: Self | None, child: bool, environment: Mapping[str, str]) -> Self | None:
        """What a new run is pinned to: a child's is its parent run's (from the run records and the
        process tree, never from what its environment names), anything else what was asked. A child
        asking another, or an environment naming a folder of another subscription, is refused."""
        pinned = parent if child else asked
        if child and asked is not None and (asked.subscription_id, asked.lent) != ((parent.subscription_id, parent.lent) if parent is not None else (None, False)):
            raise SubscriptionRefused("a child runs on its parent run's subscription, never another")
        if child and pinned is not None:
            pinned = pinned.model_copy(update={"capped": None})
        expected = frozenset() if pinned is None else frozenset(Path(folder).resolve() for folder in pinned.overlay().values())
        if not Subscription.named_in(environment) <= expected:
            raise SubscriptionRefused("this run's environment names another subscription's folder; a run uses only the subscription it is pinned to")
        return pinned

    def pins(self, asked: UUID | None) -> None:
        """Refused unless `asked` (a turn or message's subscription) is this run's own."""
        if asked is not None and asked != self.subscription_id:
            raise SubscriptionRefused("this run is pinned to another subscription: its turns never move to a different one")

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
            Subscription.of(self.subscription_id).check(self.limits or SubscriptionLimits(), self.allowance)
