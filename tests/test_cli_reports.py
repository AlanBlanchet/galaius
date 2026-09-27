"""The typed report/view shapes the CLI and dashboard render — usage aggregation and the
`interact` dashboard's own sections."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from interact.cli.tui import _provider_usage_rows
from interact.cli.usage import UsageReport
from interact.cli.view import View
from interact.config import Config
from interact.models import Model
from tests.support import catalog_json

# --- Usage-log aggregation — the data behind `interact usage` -------------------------------


def _write_log(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _row(
    model: str, provider: str, days_ago: float, cost: float, tin: int, tout: int, now: datetime
) -> dict:
    return {
        "timestamp": (now - timedelta(days=days_ago)).isoformat(),
        "model": model,
        "provider": provider,
        "backend": "api",
        "billing": "metered_api",
        "outcome": "succeeded",
        "input_tokens": tin,
        "output_tokens": tout,
        "cost": cost,
    }


def test_usage_aggregation_and_window(tmp_path: Path) -> None:
    now = datetime(2026, 6, 2, tzinfo=timezone.utc)
    log = tmp_path / "usage.jsonl"
    _write_log(log, [
        _row("openai/gpt-4o", "openai", 0.5, 0.10, 1000, 200, now),
        _row("openai/gpt-4o", "openai", 1.0, 0.20, 2000, 300, now),
        _row("gemini/gemini-3-pro", "gemini", 2.0, 0.05, 500, 100, now),
        _row("gemini/gemini-3-pro", "gemini", 40.0, 9.99, 9999, 9999, now),  # outside a 7d window
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


def test_session_usage_keeps_unknown_account_impact_distinct_from_zero(tmp_path: Path) -> None:
    now = datetime(2026, 6, 2, tzinfo=timezone.utc)
    log = tmp_path / "usage.jsonl"
    _write_log(log, [
        {
            "timestamp": now.isoformat(),
            "model": "claude default",
            "backend": "session",
            "provider": "claude",
            "billing": "session_usage",
            "outcome": "succeeded",
            "input_tokens": 120,
            "output_tokens": 8,
            "incremental_cost_usd": None,
            "api_equivalent_cost_usd": 0.02,
            "cost": None,
        },
        # Old rows without required transport identity are skipped, never inferred.
        {
            "timestamp": now.isoformat(), "model": "openai/example-model",
            "input_tokens": 20, "output_tokens": 4, "cost": 0.25,
        },
    ])

    report = UsageReport.build(path=log)

    assert report.entries == 1 and report.unknown_cost_calls == 1
    assert report.total_cost == 0.0
    providers = {group.name: group for group in report.by_provider}
    assert providers["claude"].unknown_cost_calls == 1
    assert "?" not in providers
    assert next(row for row in _provider_usage_rows(report) if row[0] == "claude") == (
        "claude", 1, 0.0, 1
    )


# --- The `interact` dashboard view ----------------------------------------------------------

_SAMPLE = catalog_json(
    ("gemini/g1", 1.0, 2.0),
    env_keys={"gemini": ["GEMINI_API_KEY"]},
    recommend={"component": ["gemini/g1"]},
    coord_formats={"gemini/": {"normalized": True, "box_order": "yxyx"}},
)


@pytest.fixture(autouse=True)
def _registry(reset_model_registry):
    pass


class TestDashboardView:
    def test_sections_reflect_state_and_serialize(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setenv("INTERACT_IMAGE_MODEL", "gemini/g1")
        Model.load_registry(_SAMPLE)

        view = View.dashboard(Config())

        assert [s.title for s in view.sections] == [
            "Providers",
            "Models",
            "Grounding models ready",
        ]
        assert "gemini" in view.sections[0].metrics[0].value
        image_row = next(r for r in view.sections[1].table.rows if r["role"] == "image")
        assert image_row["model"] == "gemini/g1"
        assert any(r["model"] == "gemini/g1" for r in view.sections[2].table.rows)

        # Round-trips as JSON — the contract an HTTP endpoint serves to the web renderer.
        assert json.loads(view.model_dump_json())["title"] == "interact"
