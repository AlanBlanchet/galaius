"""Read and aggregate the local VLM usage log.

``vision.py`` appends one JSON line per VLM call to ``~/.interact/logs/usage.jsonl``
(timestamp, model, input/output tokens, cost). The VS Code dashboard charts that file;
this module gives the CLI parity — spend, tokens and call counts grouped by model and by
provider, optionally restricted to a recent window. No network, no VLM: pure local
analysis of calls already made.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ValidationError


def default_log_path() -> Path:
    """The active usage-log path (under config.debug_dir, honouring INTERACT_DEBUG_DIR)."""
    from interact.runtime import config

    return config.usage_log


class UsageEntry(BaseModel):
    timestamp: datetime
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    @property
    def provider(self) -> str:
        return self.model.split("/", 1)[0] if "/" in self.model else "?"


class Group(BaseModel):
    """Aggregated usage for one key (a model id or a provider)."""

    name: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    def add(self, entry: UsageEntry) -> None:
        self.calls += 1
        self.input_tokens += entry.input_tokens
        self.output_tokens += entry.output_tokens
        self.cost += entry.cost


class UsageReport(BaseModel):
    since_days: int | None
    entries: int
    total_cost: float
    total_input: int
    total_output: int
    by_model: list[Group]
    by_provider: list[Group]

    @staticmethod
    def read_entries(path: Path | None = None) -> list[UsageEntry]:
        path = path or default_log_path()
        if not path.exists():
            return []
        out: list[UsageEntry] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(UsageEntry.model_validate_json(line))
            except ValidationError:
                continue  # tolerate partial/legacy lines
        return out

    @classmethod
    def build(cls, since_days: int | None = None, path: Path | None = None,
              now: datetime | None = None) -> Self:
        entries = cls.read_entries(path)
        if since_days is not None:
            cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=since_days)
            entries = [e for e in entries if e.timestamp >= cutoff]

        models: dict[str, Group] = {}
        providers: dict[str, Group] = {}
        for entry in entries:
            models.setdefault(entry.model, Group(name=entry.model)).add(entry)
            providers.setdefault(entry.provider, Group(name=entry.provider)).add(entry)

        by_cost = lambda groups: sorted(groups.values(), key=lambda g: g.cost, reverse=True)  # noqa: E731
        return cls(
            since_days=since_days,
            entries=len(entries),
            total_cost=sum(e.cost for e in entries),
            total_input=sum(e.input_tokens for e in entries),
            total_output=sum(e.output_tokens for e in entries),
            by_model=by_cost(models),
            by_provider=by_cost(providers),
        )
