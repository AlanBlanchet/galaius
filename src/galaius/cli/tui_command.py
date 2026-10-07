"""Deferred terminal-UI command boundary."""

from galaius.cli.command_bootstrap import apply_command_environment
from galaius.cli.tui import run as run_tui


def tui() -> None:
    apply_command_environment()
    run_tui()


__all__ = ["tui"]
