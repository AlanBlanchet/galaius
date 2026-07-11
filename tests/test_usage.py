"""Usage-log aggregation — the data behind `interact usage`."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from interact.cli.usage import UsageReport


def _write_log(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _row(model: str, days_ago: float, cost: float, tin: int, tout: int, now: datetime) -> dict:
    return {
        "timestamp": (now - timedelta(days=days_ago)).isoformat(),
        "model": model,
        "input_tokens": tin,
        "output_tokens": tout,
        "cost": cost,
    }


def test_usage_aggregation_and_window(tmp_path: Path) -> None:
    now = datetime(2026, 6, 2, tzinfo=timezone.utc)
    log = tmp_path / "usage.jsonl"
    _write_log(log, [
        _row("openai/gpt-4o", 0.5, 0.10, 1000, 200, now),
        _row("openai/gpt-4o", 1.0, 0.20, 2000, 300, now),
        _row("gemini/gemini-3-pro", 2.0, 0.05, 500, 100, now),
        _row("gemini/gemini-3-pro", 40.0, 9.99, 9999, 9999, now),  # outside a 7d window
        "not json at all",  # tolerated, skipped
    ])

    report = UsageReport.build(path=log)
    assert report.entries == 4
    assert round(report.total_cost, 2) == 10.34
    # by_model sorted by cost desc; the big old gemini row dominates all-time
    assert report.by_model[0].name == "gemini/gemini-3-pro"
    gpt = next(g for g in report.by_model if g.name == "openai/gpt-4o")
    assert gpt.calls == 2 and gpt.input_tokens == 3000 and round(gpt.cost, 2) == 0.30

    windowed = UsageReport.build(since_days=7, path=log, now=now)
    assert windowed.entries == 3, "the 40-day-old row is excluded"
    assert round(windowed.total_cost, 2) == 0.35
    providers = {g.name: g for g in windowed.by_provider}
    assert providers["openai"].calls == 2 and providers["gemini"].calls == 1


def test_usage_missing_log(tmp_path: Path) -> None:
    report = UsageReport.build(path=tmp_path / "nope.jsonl")
    assert report.entries == 0 and report.total_cost == 0.0 and report.by_model == []
