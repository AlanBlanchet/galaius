"""The PC runner's watch over its logins' usage: the usage lines each running run pinned to a subscription
appends are read as they come (caps applied, `registry.subscription_heard`); every login here, each
program's default one (the owner's own editor sessions') and each subscription's, is asked its usage now
by its own CLI (`ask`, `AgentProvider.current_usage`); what moved goes to the server (`SubscriptionUsageReport`).

Its own reader, not the registry's event fold: `registry.append_event` folds a CONVERSATION's events
only, and the incremental stream mirror runs in the MCP server's loop, not in the runner; and Codex
writes its usage only into its session record inside the subscription folder, never on the stream."""

import asyncio
import threading
from datetime import datetime
from pathlib import Path
from typing import get_args

from galaius_core import AgentProvider, LoginUsage, SubscriptionUsageReport, UsageWindow
from pydantic import BaseModel, PrivateAttr

from galaius.agents import registry as reg
from galaius.agents.events import stream_lines
from galaius.agents.providers import PROVIDERS
from galaius.agents.subscriptions import Subscription


class LoginReading(BaseModel):
    """A program's default login here: its last usage reading, and the one last reported (None: none since this runner
    started, so the server's, maybe older, is replaced by the first)."""

    usage: tuple[UsageWindow, ...] = ()
    reported: tuple[UsageWindow, ...] | None = None
    reported_at: datetime | None = None


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
        its usage now as its CLI says, one CLI at a time. Not installed: no reading; a CLI that could not answer leaves
        the last one. A subscription's reading goes to its folder (`Subscription.observe`, as a run's would)."""
        for name in get_args(AgentProvider):
            provider = PROVIDERS[name]
            usage = await provider.current_usage(environment) if provider.available() else ()
            if usage is not None:
                with self._lock:
                    self._logins[name] = self._logins.get(name, LoginReading()).model_copy(update={"usage": usage})
        for subscription in await asyncio.to_thread(Subscription.all):
            if usage := await subscription.program.current_usage({**environment, **subscription.overlay()}):
                await asyncio.to_thread(subscription.observe, usage)

    def login_usage(self, provider: AgentProvider) -> tuple[UsageWindow, ...]:
        """`provider`'s default login's last reading here (`ask`)."""
        with self._lock:
            reading = self._logins.get(provider)
        return () if reading is None else reading.usage

    def heard(self) -> None:
        """Every running run pinned to a subscription: the usage lines appended since the last call."""
        running = [run for run in reg.running_runs() if run.subscription_id is not None]
        for run in running:
            provider = PROVIDERS[run.provider]
            files = (reg.raw_events_path(run.run_id), *filter(None, (self._session_record(run),)))
            events = [event for path in files for line in self._appended(path, run.usage_from.get(str(path), 0)) if (event := provider.parse(line)) is not None]
            reg.subscription_heard(run, events)
        watched = {reg.raw_events_path(run.run_id) for run in running} | {self._records[run.run_id] for run in running if run.run_id in self._records}
        self._offsets = {path: offset for path, offset in self._offsets.items() if path in watched}
        self._records = {run.run_id: self._records[run.run_id] for run in running if run.run_id in self._records}

    def report(self, now: datetime) -> SubscriptionUsageReport | None:
        """What the server should hear now (`Subscription.report_due`); None: nothing. Marked reported
        only once sent (`sent`)."""
        due = tuple(usage for subscription in Subscription.all() if (usage := subscription.report_due(now)) is not None)
        with self._lock:
            logins = tuple(LoginUsage(provider=name, windows=reading.usage) for name, reading in self._logins.items()
                           if reading.reported is None or SubscriptionUsageReport.due(reading.usage, reading.reported, reading.reported_at, now))
        return SubscriptionUsageReport(usage=due, logins=logins) if due or logins else None

    def sent(self, report: SubscriptionUsageReport, now: datetime) -> None:
        for usage in report.usage:
            Subscription.of(usage.subscription_id).reported(usage, now)
        with self._lock:
            for login in report.logins:
                self._logins[login.provider] = self._logins[login.provider].model_copy(update={"reported": login.windows, "reported_at": now})

    def _session_record(self, run: reg.AgentRun) -> Path | None:
        """A Codex run's session record in its subscription's folder (`sessions/**/rollout-*-<thread>.jsonl`),
        where Codex writes its `rate_limits`; None for another CLI or before Codex wrote it."""
        if run.provider != "codex" or not run.provider_session_id:
            return None
        if run.run_id not in self._records:
            folder = run.fence.sessions("codex")[0] if run.fence is not None else Subscription.of(run.subscription_id).folder / "sessions"
            found = next(iter(sorted(folder.rglob(f"rollout-*-{run.provider_session_id}.jsonl"))), None) if folder.is_dir() else None
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
