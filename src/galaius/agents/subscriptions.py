"""Coding-agent subscriptions signed in on this PC, each in its own folder (`UserPaths.subscriptions()/<id>/`,
0700): the CLI's home for that login, named to it through `AgentProvider.auth_home_env`. The user's own
default logins (`~/.claude`, `~/.codex`) are never read or written here. A run is pinned to one
subscription (`RunSubscription`, on its record): every turn and every child runs on it, caps are checked
before each start and on each usage reading, and its usage is kept here, last value per window."""

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Mapping
from typing import ClassVar, Self
from uuid import UUID

from galaius_core import AgentProvider as ProviderName, SubscriptionCap, SubscriptionLimits, SubscriptionUsage, SubscriptionUsageReport, UsageWindow
from pydantic import BaseModel, ConfigDict

from galaius.agents.providers import PROVIDERS, AgentProvider
from galaius.paths import UserPaths


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
    #: The folder every CLI login is pointed at for a member's run on a lent PC: empty, never writable.
    LENT: ClassVar[str] = ".lent"
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
    def all(cls) -> tuple[Self, ...]:
        if not cls.root().is_dir():
            return ()
        found = []
        for folder in sorted(cls.root().iterdir()):
            try:
                found.append(cls.of(UUID(folder.name)))
            except (ValueError, SubscriptionRefused):
                continue
        return tuple(found)

    @classmethod
    def tokens(cls) -> tuple[str, ...]:
        """Every subscription's `credentials` here, longest first (a token inside another stays masked)."""
        return tuple(sorted({value for subscription in cls.all() for value in subscription.credentials()}, key=len, reverse=True))

    @classmethod
    def lent_overlay(cls) -> dict[str, str]:
        """Every CLI's login folder pointed at one empty folder nothing can write: no login answers there."""
        folder = cls.root() / cls.LENT
        if not folder.is_dir():
            cls._private(cls.root())
            folder.mkdir(mode=0o500)
        return {name: str(folder) for provider in PROVIDERS.values() for name in provider.auth_home_env}

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

    def check(self, limits: SubscriptionLimits, allowance: int | None = None) -> None:
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

    def credentials(self) -> tuple[str, ...]:
        """The token values its CLI keeps in this folder now, read again at each call (a refresh
        changes them): what a run's output never shows."""
        values: list[str] = []
        for name in self.CREDENTIALS:
            try:
                values += self._strings(json.loads((self.folder / name).read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return tuple(value for value in dict.fromkeys(values) if len(value) >= self.SECRET_LENGTH)

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
    def _private(folder: Path) -> None:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)

    def _write(self, state: SubscriptionState) -> None:
        temporary = self.state_path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(state.model_dump_json(), encoding="utf-8")
        temporary.replace(self.state_path)


class RunSubscription(BaseModel):
    """The subscription a run is pinned to, on its record: every turn and child runs on `id` (a
    member's run on a lent PC, `lent`, on no CLI login at all), under `limits`, raised for this run
    only by `allowance`; `capped` is the cap its subscription reached while it ran."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    lent: bool = False
    limits: SubscriptionLimits = SubscriptionLimits()
    allowance: int | None = None
    capped: SubscriptionCap | None = None

    @classmethod
    def resolve(cls, asked: Self | None, *, parent: Self | None, child: bool, environment: Mapping[str, str]) -> Self | None:
        """What a new run is pinned to: a child's is its parent run's (from the run records, never
        from what its environment names), anything else what was asked. A child asking another, or an
        environment naming another subscription's folder, is refused."""
        pinned = parent if child else asked
        if child and asked is not None and (asked.id, asked.lent) != ((parent.id, parent.lent) if parent is not None else (None, False)):
            raise SubscriptionRefused("a child runs on its parent run's subscription, never another")
        if child and pinned is not None:
            pinned = pinned.model_copy(update={"capped": None})
        expected = frozenset() if pinned is None else frozenset({pinned.folder().resolve()})
        if not Subscription.named_in(environment) <= expected:
            raise SubscriptionRefused("this run's environment names another subscription's folder; a run uses only the subscription it is pinned to")
        return pinned

    def folder(self) -> Path:
        return Subscription.root() / Subscription.LENT if self.id is None else Subscription.of(self.id).folder

    def pins(self, asked: UUID | None) -> None:
        """Refused unless `asked` (a turn or message's subscription) is this run's own."""
        if asked is not None and asked != self.id:
            raise SubscriptionRefused("this run is pinned to another subscription: its turns never move to a different one")

    def overlay(self) -> dict[str, str]:
        return Subscription.lent_overlay() if self.id is None else Subscription.of(self.id).overlay()

    def check(self) -> None:
        """Refused while its subscription is at a cap (this run's allowance counted)."""
        if self.id is not None:
            Subscription.of(self.id).check(self.limits, self.allowance)
