"""A command line the CLI cannot read is refused in one plain line wherever no terminal reads it."""

import subprocess
import sys


def test_an_unreadable_command_line_is_refused_in_one_plain_line_off_a_terminal() -> None:
    """2026-10-27 his PC relayed a refusal as « ╭─ Error ───╮ »: the framed panel's top edge was all a reader kept."""
    said = subprocess.run([sys.executable, "-m", "galaius", "agents", "send", "run", "- a message"], capture_output=True, text=True, timeout=60, check=False)
    assert said.returncode == 1
    assert "ERROR: Unknown option: - ." in said.stderr.splitlines() and "\u256d" not in said.stderr, said.stderr
