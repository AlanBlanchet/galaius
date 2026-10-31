"""A command line the CLI cannot read is refused in one plain line wherever no terminal reads it."""

import subprocess
import sys

import pytest

LONG = "--no-such-option-" + "x" * 120


@pytest.mark.parametrize("argv, said", [(["run", "- a message"], "ERROR: Unknown option: - ."),
                                        (["--message-id", "m", LONG, "run", "hi"], f"ERROR: Unknown option: {LONG}.")], ids=["dash", "past-80-columns"])
def test_an_unreadable_command_line_is_refused_in_one_plain_line_off_a_terminal(argv: list[str], said: str) -> None:
    """2026-10-27 his PC relayed a refusal as « ╭─ Error ───╮ »: the framed panel's top edge was all a reader kept, as it
    keeps the first line of any refusal."""
    done = subprocess.run([sys.executable, "-m", "galaius", "agents", "send", *argv], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 1
    assert said in done.stderr.splitlines() and "\u256d" not in done.stderr, done.stderr
