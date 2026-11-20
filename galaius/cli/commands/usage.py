"""`galaius usage`: what the models cost, by day and by model."""

from typing import Annotated
from cyclopts import Parameter
from galaius.cli.usage import UsageReport, default_log_path


def usage(
    days: Annotated[int | None, Parameter(name=["--days", "-d"])] = None,
) -> None:
    """Summarise local VLM spend, tokens and calls by model and provider.

    Reads ~/.galaius/out/usage.jsonl (the same log the VS Code dashboard charts) —
    no network, no API calls. This is the CLI's usage analysis.

    Parameters
    ----------
    days
        Only count calls from the last N days (default: all time).
    """

    log_path = default_log_path()
    report = UsageReport.build(since_days=days)
    window = f"last {days}d" if days else "all time"
    if report.entries == 0:
        where = "no calls recorded yet" if not log_path.exists() else f"no calls in the {window}"
        print(f"galaius usage ({window}): {where}\n  log: {log_path}")
        return

    unknown = (
        f"; {report.session_usage_calls} session-usage call(s) with unknown account impact"
        if report.session_usage_calls
        else ""
    )
    print(
        f"galaius usage ({window}) — {report.entries} calls, observed metered API spend "
        f"${report.total_cost:.4f}{unknown}, {report.usage.prompt:,} in / "
        f"{report.usage.output or 0:,} out tokens\n"
    )
    print(f"  {'model':<40} {'calls':>6} {'in':>10} {'out':>10} {'cost':>10}")
    for group in report.by_model:
        cost = (
            f"${group.cost:.4f} + unknown"
            if group.unknown_cost_calls and group.cost
            else "unknown"
            if group.unknown_cost_calls
            else f"${group.cost:.4f}"
        )
        print(
            f"  {group.name:<40} {group.calls:>6} {group.usage.prompt:>10,} "
            f"{group.usage.output or 0:>10,} {cost:>10}"
        )
    if len(report.by_provider) > 1:
        print("\n  by provider:")
        for group in report.by_provider:
            cost = (
                f"${group.cost:.4f} + unknown"
                if group.unknown_cost_calls and group.cost
                else "unknown"
                if group.unknown_cost_calls
                else f"${group.cost:.4f}"
            )
            print(f"  {group.name:<40} {group.calls:>6} {' ':>10} {' ':>10} {cost:>10}")
