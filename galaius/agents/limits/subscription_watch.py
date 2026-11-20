"""The PC runner's watch over its logins' usage: the usage lines each running run pinned to a subscription
appends are read as they come (caps applied, `registry.subscription_heard`); every login here, each
program's default one (the owner's own editor sessions') and each subscription's, is asked its usage now
by its own CLI (`ask`, `AgentProvider.current_usage`); what moved goes to the server (`SubscriptionUsageReport`).

Its own reader, not the registry's event fold: `registry.append_event` folds a CONVERSATION's events
only, and the incremental stream mirror runs in the MCP server's loop, not in the runner; and Codex
writes its usage only into its session record inside the subscription folder, never on the stream."""

import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from galaius_core import AgentProvider, LoginIdentity, LoginUsage, SubscriptionUsageReport, UsageWindow
from pydantic import BaseModel, PrivateAttr

from galaius.agents import registry as reg
from galaius.agents.limits import quota
from galaius.agents.transport.events import stream_lines
from galaius.agents.providers import PROVIDERS, AgentProvider as Program, held_environment
from galaius.agents.limits.subscriptions import Subscription, SubscriptionRefused


class LoginReading(BaseModel):
    """A program's default login here: its last usage reading and the login its CLI last named, and what was last
    reported of them (None: nothing since this runner started, so the server's, maybe older, is replaced by the first)."""

    usage: tuple[UsageWindow, ...] = ()
    #: The model-family pools its runs streamed (`quota.POOLS`), reported beside `usage` as windows of their own.
    pools: tuple[quota.ModelPool, ...] = ()
    identity: LoginIdentity = LoginIdentity()
    reported: tuple[UsageWindow, ...] | None = None
    reported_identity: LoginIdentity = LoginIdentity()
    reported_at: datetime | None = None

    def due(self, now: datetime) -> bool:
        """The server should hear it now: never yet, its usage moved (`SubscriptionUsageReport.due`), or its CLI names
        another login than the one last reported."""
        return (self.reported is None or SubscriptionUsageReport.due(self.windows(), self.reported, self.reported_at, now)
                or bool(self.identity.login) and self.identity != self.reported_identity)

    def windows(self) -> tuple[UsageWindow, ...]:
        """Its readings of windows not reopened since (a reading past its `resets_at` is the old window's), the family pools beside them."""
        now = datetime.now(UTC)
        return tuple(window for window in (*self.usage, *(pool.window for pool in self.pools)) if window.resets_at is None or window.resets_at > now)

    def report(self, provider: AgentProvider) -> LoginUsage:
        return LoginUsage(provider=provider, windows=self.windows(), **self.identity.model_dump())


class SubscriptionWatch(BaseModel):
    """Byte offsets into each file it reads (a run's raw stream, a Codex session record): a line is read once; each
    program's default login's reading (`ask`)."""

    _offsets: dict[Path, int] = PrivateAttr(default_factory=dict)
    _records: dict[str, Path] = PrivateAttr(default_factory=dict)
    _logins: dict[AgentProvider, LoginReading] = PrivateAttr(default_factory=dict)
    #: `ask` writes `_logins` on the runner's loop; `report`, `sent` and the program states read it from threads.
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    async def ask(self, environment: dict[str, str]) -> None:
        """Each program's default login (`environment`: the system variables), then each subscription in its own folder:
        its usage and which login it is now as its CLI says (`identity`), one CLI at a time. Not installed: no reading;
        a CLI that could not answer leaves the last one. A subscription's go to its folder (`Subscription.observe`, as
        a run's would, `Subscription.identify`)."""
        environment = held_environment(environment)
        for name, provider in PROVIDERS.items():
            installed = provider.available()
            asked = time.time()
            usage = await provider.current_usage(environment) if installed else ()
            identity = await self.identity(provider, environment) if installed else LoginIdentity()
            if usage is not None:
                await asyncio.to_thread(quota.usage_heard, name, usage, asked=asked)
            with self._lock:
                reading = self._logins.get(name, LoginReading())
                self._logins[name] = reading.model_copy(update={"usage": reading.usage if self.keeps(usage, identity) else usage,
                                                                "identity": reading.identity if identity is None else identity})
        for subscription in await asyncio.to_thread(Subscription.all):
            overlaid = held_environment({**environment, **subscription.overlay()})
            asked = time.time()
            usage = await subscription.program.current_usage(overlaid)
            identity = await self.identity(subscription.program, overlaid)
            if usage is not None and not self.keeps(usage, identity):
                await asyncio.to_thread(subscription.observe, usage, whole=True)
                await asyncio.to_thread(quota.usage_heard, subscription.program.name, usage, asked=asked, subscription=subscription.id)
            if identity is not None:
                await asyncio.to_thread(subscription.identify, identity)

    @staticmethod
    def keeps(usage: tuple[UsageWindow, ...] | None, identity: LoginIdentity | None) -> bool:
        """Its CLI's answer leaves the last reading: it could not be asked (None), or it names no window for a login that IS
        signed in (a held token, an API key: `rate_limits_available` false). That login's readings are the vendor's own
        lines its runs stream (`heard`); an empty answer wiping them every `usage_ask_seconds` left every held login at
        « no reading » (2026-11-20). Only a signed-out login (no login named) is read as none."""
        return usage is None or (not usage and identity is not None and bool(identity.login))

    @staticmethod
    async def identity(provider: Program, environment: dict[str, str]) -> LoginIdentity | None:
        """Which login `environment` names for `provider` now, as its status command says (`login_status`; never its
        expiry check: an expired login is the same account); none when signed out; None: it could not answer."""
        status = await provider.login_status(environment, expiry=False)
        return None if status.signed_in is None else LoginIdentity(account=status.account, login=status.login)

    def login_usage(self, provider: AgentProvider) -> tuple[UsageWindow, ...]:
        """`provider`'s default login's last reading here (`ask`)."""
        with self._lock:
            reading = self._logins.get(provider)
        return () if reading is None else reading.usage

    def heard(self) -> None:
        """Every running run's usage lines appended since the last call: a run pinned to a subscription feeds that subscription's
        windows and pools, one on this PC's own login that login's (`login_heard`); a pool at his stop holds its family there."""
        running = [run for run in reg.running_runs() if run.subscription_id is not None]
        for run in running:
            provider = PROVIDERS[run.provider]
            files = (reg.raw_events_path(run.run_id), *filter(None, (self._session_record(run),)))
            lines = [line for path in files for line in self._appended(path, run.usage_from.get(str(path), 0))]
            reg.subscription_heard(run, [event for line in lines if (event := provider.parse(line)) is not None])
            if (pools := self._pools_in(provider, lines)):
                try:
                    Subscription.of(run.subscription_id).observe_pools(pools)
                except SubscriptionRefused:
                    pass  # signed out while it ran: nothing to keep
        own = [run for run in reg.running_runs() if run.subscription_id is None and run.provider in PROVIDERS]
        for run in own:  # a run on this PC's own login: the same lines say that login's windows, with no subscription to keep them in
            provider = PROVIDERS[run.provider]
            path = reg.raw_events_path(run.run_id)
            lines = [line for line in self._appended(path, 0) if "rate_limit" in line]
            events = [event for line in lines if (event := provider.parse(line)) is not None]
            if windows := tuple(window for event in events for window in event.windows):
                self.login_heard(run.provider, windows)
            if pools := self._pools_in(provider, lines):
                Subscription.stop_pools(pools, provider=run.provider, subscription=None)
                self.login_pools(run.provider, pools)
        watched = {reg.raw_events_path(run.run_id) for run in (*running, *own)} | {self._records[run.run_id] for run in running if run.run_id in self._records}
        self._offsets = {path: offset for path, offset in self._offsets.items() if path in watched}
        self._records = {run.run_id: self._records[run.run_id] for run in running if run.run_id in self._records}

    def login_heard(self, provider: AgentProvider, windows: tuple[UsageWindow, ...]) -> None:
        """`windows` a run on `provider`'s own login streamed (oldest first) replace that login's readings of the same windows."""
        with self._lock:
            reading = self._logins.get(provider, LoginReading())
            latest = {window.window: window for window in (*reading.usage, *windows)}
            self._logins[provider] = reading.model_copy(update={"usage": tuple(latest.values())})

    def login_pools(self, provider: AgentProvider, pools: tuple[quota.ModelPool, ...]) -> None:
        """`pools` a run on `provider`'s own login streamed (oldest first) replace that login's readings of the same families."""
        with self._lock:
            reading = self._logins.get(provider, LoginReading())
            latest = {pool.pool: pool for pool in (*reading.pools, *pools)}
            self._logins[provider] = reading.model_copy(update={"pools": tuple(latest.values())})

    @staticmethod
    def _pools_in(provider: Program, lines: list[str]) -> tuple[quota.ModelPool, ...]:
        """The model-family pools (`quota.POOLS`) `lines` say, oldest first: those lines that mention one only."""
        found = []
        for line in lines:
            if "unifiedWindows" not in line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            found.extend(provider.pools(parsed) if isinstance(parsed, dict) else ())
        return tuple(found)

    def report(self, now: datetime) -> SubscriptionUsageReport | None:
        """What the server should hear now (`Subscription.report_due`); None: nothing. Marked reported
        only once sent (`sent`)."""
        due = tuple(usage for subscription in Subscription.all() if (usage := subscription.report_due(now)) is not None)
        with self._lock:
            logins = tuple(reading.report(name) for name, reading in self._logins.items() if reading.due(now))
        return SubscriptionUsageReport(usage=due, logins=logins) if due or logins else None

    def sent(self, report: SubscriptionUsageReport, now: datetime) -> None:
        for usage in report.usage:
            Subscription.of(usage.subscription_id).reported(usage, now)
        with self._lock:
            for login in report.logins:
                self._logins[login.provider] = self._logins[login.provider].model_copy(update={
                    "reported": login.windows, "reported_identity": LoginIdentity(account=login.account, login=login.login), "reported_at": now})

    def _session_record(self, run: reg.AgentRun) -> Path | None:
        """The record in its subscription's folder where run `run`'s CLI writes its usage windows (`usage_record`); None
        for a CLI that writes none there, or before it wrote it."""
        if (provider := PROVIDERS.get(run.provider)) is None or provider.usage_records is None or not run.provider_session_id:
            return None
        if run.run_id not in self._records:
            folder = run.fence.sessions(run.provider)[0] if run.fence is not None else Subscription.of(run.subscription_id).folder / "sessions"
            found = provider.usage_record(folder, run.provider_session_id)
            if found is None:
                return None
            self._records[run.run_id] = found
        return self._records[run.run_id]

    def _appended(self, path: Path, start: int) -> list[str]:
        """The complete lines `path` gained since the last read (first read: from byte `start`, where the run's current
        subscription's readings begin, `AgentRun.usage_from`)."""
        offset = self._offsets.get(path, start)
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read()
        except OSError:
            return []
        cut = data.rfind(b"\n") + 1
        self._offsets[path] = offset + cut
        return stream_lines(data[:cut].decode(errors="replace"))
