"""The PC runner's watch over its subscriptions: the usage lines each running pinned run appends are
read as they come (caps applied, `registry.subscription_heard`), and each subscription's usage goes
to the server when it moved enough (`SubscriptionUsageReport`)."""

from datetime import datetime

from galaius_core import SubscriptionUsageReport
from pydantic import BaseModel, PrivateAttr

from galaius.agents import registry as reg
from galaius.agents.events import stream_lines
from galaius.agents.providers import PROVIDERS
from galaius.agents.subscriptions import Subscription


class SubscriptionWatch(BaseModel):
    """Byte offsets into each watched run's raw stream: a line is read once."""

    _offsets: dict[str, int] = PrivateAttr(default_factory=dict)

    def heard(self) -> None:
        """Every running run pinned to a subscription: the usage lines appended since the last call."""
        running = [run for run in reg.running_runs() if run.subscription_id is not None]
        for run in running:
            events = [event for line in self._appended(run.run_id) if (event := PROVIDERS[run.provider].parse(line)) is not None]
            reg.subscription_heard(run, events)
        self._offsets = {run.run_id: self._offsets[run.run_id] for run in running if run.run_id in self._offsets}

    def report(self, now: datetime) -> SubscriptionUsageReport | None:
        """What the server should hear now (`Subscription.report_due`), marked reported; None: nothing."""
        due = [(subscription, usage) for subscription in Subscription.all() if (usage := subscription.report_due(now)) is not None]
        for subscription, usage in due:
            subscription.reported(usage, now)
        return SubscriptionUsageReport(usage=tuple(usage for _, usage in due)) if due else None

    def _appended(self, run_id: str) -> list[str]:
        """The complete lines `run_id`'s raw stream gained since the last read."""
        try:
            with reg.raw_events_path(run_id).open("rb") as stream:
                stream.seek(self._offsets.get(run_id, 0))
                data = stream.read()
        except OSError:
            return []
        cut = data.rfind(b"\n") + 1
        self._offsets[run_id] = self._offsets.get(run_id, 0) + cut
        return stream_lines(data[:cut].decode(errors="replace"))
