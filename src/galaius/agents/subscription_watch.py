"""The PC runner's watch over its subscriptions: the usage lines each running pinned run appends are
read as they come (caps applied, `registry.subscription_heard`), and each subscription's usage goes
to the server when it moved enough (`SubscriptionUsageReport`).

Its own reader, not the registry's event fold: `registry.append_event` folds a CONVERSATION's events
only, and the incremental stream mirror runs in the MCP server's loop, not in the runner; and Codex
writes its usage only into its session record inside the subscription folder, never on the stream."""

from datetime import datetime
from pathlib import Path

from galaius_core import SubscriptionUsageReport
from pydantic import BaseModel, PrivateAttr

from galaius.agents import registry as reg
from galaius.agents.events import stream_lines
from galaius.agents.providers import PROVIDERS
from galaius.agents.subscriptions import Subscription


class SubscriptionWatch(BaseModel):
    """Byte offsets into each file it reads (a run's raw stream, a Codex session record): a line is read once."""

    _offsets: dict[Path, int] = PrivateAttr(default_factory=dict)
    _records: dict[str, Path] = PrivateAttr(default_factory=dict)

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
        return SubscriptionUsageReport(usage=due) if due else None

    @staticmethod
    def sent(report: SubscriptionUsageReport, now: datetime) -> None:
        for usage in report.usage:
            Subscription.of(usage.subscription_id).reported(usage, now)

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
