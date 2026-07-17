"""Agents naturally launch project apps with shell phrasing — `cd <repo> && uv run app` — but
launch_app exec'd the shlex argv verbatim, failing with the cryptic `[Errno 2] No such file or
directory: 'cd'` (hit as the FIRST attempt by two independent sessions driving a real app).
A command using shell syntax must run via a shell instead of raw exec."""

import pytest

from interact.launch import needs_shell


@pytest.mark.parametrize(
    "cmd",
    [
        "cd ~/proj && uv run app",  # the exact real-session failure
        "  cd /tmp && ./run.sh",  # leading whitespace
        "make build && ./out/app",
        "app --flag | tee log",
        "app; other",
        "app > /tmp/out.log",
        "app < /tmp/in.txt",
        "APP_DIR=$(pwd) app",
        "echo `date` && app",
        "a || b",
    ],
)
def test_shell_syntax_commands_need_a_shell(cmd):
    assert needs_shell(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "xterm",
        "flutter run -d linux",
        "/path/to/bin --flag value",
        "env LANG=C google-chrome",
        'app --title "plain quoted arg"',
    ],
)
def test_plain_exec_commands_do_not(cmd):
    assert needs_shell(cmd) is False
