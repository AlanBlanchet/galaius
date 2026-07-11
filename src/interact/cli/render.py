"""Surface adapters that render a :class:`interact.cli.view.View`.

A renderer is the only place UI lives. ``CliRenderer`` turns a View into terminal
output; a web renderer (phase 2) turns the same View JSON into HTML for the browser
and the VS Code webview. The View itself stays widget-free.
"""

from rich.console import Console
from rich.table import Table as RichTable

from interact.cli.view import View


class CliRenderer:
    """Render a :class:`View` to the terminal with compact rich tables."""

    @staticmethod
    def render(view: View, console: Console | None = None) -> None:
        out = console or Console()
        out.print(f"\n[bold]{view.title}[/bold]")
        for section in view.sections:
            out.print(f"\n[bold cyan]{section.title}[/bold cyan]")
            for metric in section.metrics:
                out.print(f"  {metric.label}: {metric.value}")
            if section.table is not None:
                table = RichTable(show_edge=False, pad_edge=False, box=None)
                for column in section.table.columns:
                    table.add_column(column.label)
                for row in section.table.rows:
                    table.add_row(*(row.get(column.key, "") for column in section.table.columns))
                out.print(table)
            if section.note:
                out.print(f"  [dim]{section.note}[/dim]")
