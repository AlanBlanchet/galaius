"""Deferred terminal-UI command boundary."""

from interact.cli.command_bootstrap import apply_command_environment
from interact.cli.tui import run as run_tui


def tui() -> None:
    apply_command_environment()
    run_tui()


__all__ = ["tui"]
