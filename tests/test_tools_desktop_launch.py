"""launch_app tool-level command handling.

A shell-syntax command (`cd X && app`) runs via bash instead of failing exec with
`[Errno 2] No such file or directory: 'cd'`, `cwd=` starts the app in a project directory
directly, a `VAR=value app` prefix (#117) becomes the launch's environment instead of being
exec'd as a literal program named `VAR=value`, and the "Replaced N app(s)" note counts
kills that HAPPENED and names what survived (#118).
"""

import asyncio
from pathlib import Path

import pytest

import interact.server as srv
from interact.desktop.nested import KillReport

pytestmark = pytest.mark.usefixtures("desktop_gate_open")



class _ToolFakeBackend:
    display = ":88"

    def __init__(self):
        self.spawned: list[tuple[list[str], str | None, dict[str, str] | None]] = []
        self.spawn_error: OSError | None = None  # raised by spawn instead of launching
        self.kill_report = KillReport(killed=0)  # what a replace-kill reports back
        self.windows: list[tuple[int, str]] = [(7, "App")]

    def kill_apps(self):
        return self.kill_report

    def spawn(self, argv, cwd=None, env=None):
        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawned.append((list(argv), cwd, env))
        return type("P", (), {"poll": lambda self: None, "returncode": None})()

    def list_windows(self):
        return self.windows


@pytest.fixture
def fake_backend(monkeypatch):
    fb = _ToolFakeBackend()
    monkeypatch.setattr(srv.sandbox, "_get_sandbox", lambda size=None: fb)
    return fb


def test_cd_command_runs_via_shell(fake_backend):
    cmd = "cd /tmp/project/proj && exec uv run app"
    out = asyncio.run(srv.launch_app(cmd, wait=1))
    assert "ERROR" not in out and "No such file" not in out
    argv, _, _ = fake_backend.spawned[0]
    assert argv[:2] == ["bash", "-c"] and argv[2] == cmd


def test_cwd_param_spawns_in_that_directory(fake_backend, tmp_path):
    out = asyncio.run(srv.launch_app("uv run app", wait=1, cwd=str(tmp_path)))
    assert "ERROR" not in out
    assert fake_backend.spawned == [(["uv", "run", "app"], str(tmp_path), None)]


def test_missing_cwd_is_a_clear_error(fake_backend, tmp_path):
    out = asyncio.run(srv.launch_app("app", cwd=str(tmp_path / "nope")))
    assert out.startswith("ERROR") and "cwd" in out
    assert fake_backend.spawned == []  # never spawned into a wrong directory


# ── `VAR=value app` (#117) ────────────────────────────────────────────────────────────────
# Agents write `FOO=bar app` as naturally as `cd X && app`. No shell marker matches it, so
# the shlex argv was exec'd verbatim: Popen(["FOO=bar", "app"]) → FileNotFoundError:
# 'FOO=bar', reaching the agent as FastMCP's generic exception text, not one of this
# module's `ERROR:` strings.


def test_leading_assignments_become_the_launch_env(fake_backend):
    out = asyncio.run(srv.launch_app("FOO=bar BAZ=1 app --x", wait=1))
    assert "ERROR" not in out
    assert fake_backend.spawned == [(["app", "--x"], None, {"FOO": "bar", "BAZ": "1"})]


def test_assignments_keep_the_launch_rewrites(fake_backend, tmp_path, monkeypatch):
    """The prefix must NOT reroute the command through bash: staying on the exec path is what
    keeps the sandbox rewrites (an editor's isolated profile here, Flutter's software-GL
    flag) — a `bash -c` launch bypasses them."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    out = asyncio.run(srv.launch_app("FOO=bar code /tmp/p", wait=1))
    assert "ERROR" not in out
    argv, _, env = fake_backend.spawned[0]
    assert argv[:2] == ["code", "/tmp/p"] and "--user-data-dir" in " ".join(argv)
    assert env == {"FOO": "bar"}


def test_an_env_binary_prefix_is_left_to_env_itself(fake_backend):
    """`env LANG=C xterm` names a real program — the `env` binary applies the assignment, so
    the argv reaches exec whole and no per-launch environment is built."""
    asyncio.run(srv.launch_app("env LANG=C xterm", wait=1))
    assert fake_backend.spawned == [(["env", "LANG=C", "xterm"], None, None)]


def test_assignments_without_a_command_are_a_clear_error(fake_backend):
    out = asyncio.run(srv.launch_app("FOO=bar", wait=1))
    assert out.startswith("ERROR") and "no command" in out
    assert fake_backend.spawned == []


@pytest.mark.parametrize(
    ("error", "phrase"),
    [
        (FileNotFoundError(2, "No such file or directory", "app"), "sandbox PATH"),
        (PermissionError(13, "Permission denied", "app"), "chmod +x"),
    ],
)
def test_a_failed_exec_is_a_guided_error_naming_the_real_command(fake_backend, error, phrase):
    """A program exec cannot start returns a guided `ERROR:` naming what was actually tried —
    `app`, the real command, now that the `FOO=bar` prefix is consumed rather than exec'd."""
    fake_backend.spawn_error = error
    out = asyncio.run(srv.launch_app("FOO=bar app --x", wait=1))
    assert out.startswith("ERROR") and "'app'" in out and phrase in out


# ── "Replaced N app(s)" reports what the kill DID (#118) ────────────────────────────────
# `kill_apps` used to return the PRE-kill count, so an Electron app whose helpers `setsid`
# out of the group was reported "replaced" while its window stayed up holding the profile
# socket — and the next launch, handed to that zombie, opened nothing with no explanation
# anywhere.


@pytest.mark.parametrize(
    ("report", "present", "absent"),
    [
        (KillReport(killed=2), ["Replaced 2 app(s)"], ["SURVIVED"]),
        (KillReport(killed=0), [], ["Replaced", "SURVIVED"]),
        (
            KillReport(killed=1, survivors={4242: "code --user-data-dir=/x --type=renderer"}),
            ["ERROR:", "stopped 1", "successor was not launched", "SURVIVED", "4242", "code",
             "reset_sandbox"],
            ["Replaced 2", "user-data-dir"],
        ),
    ],
    ids=["all_died", "nothing_to_replace", "one_survived"],
)
def test_the_replaced_note_counts_kills_and_names_survivors(fake_backend, report, present, absent):
    fake_backend.kill_report = report
    out = asyncio.run(srv.launch_app("app", wait=1))
    assert all(text in out for text in present), out
    assert not any(text in out for text in absent), out


def test_a_survivor_is_named_and_blocks_the_successor(fake_backend):
    """The old process explains the failed replacement and prevents an ambiguous second window."""
    fake_backend.kill_report = KillReport(killed=0, survivors={4242: "code --type=renderer"})
    fake_backend.windows = []
    out = asyncio.run(srv.launch_app("app", wait=0.5))
    assert out.startswith("ERROR:") and "successor was not launched" in out
    assert "4242" in out and "code" in out and "type=renderer" not in out, out


def test_a_survivor_aborts_the_successor_launch(fake_backend):
    """`replace=True` is a gate: a leftover old process makes launching a second ambiguous
    window unsafe, so the successor must not be spawned."""
    fake_backend.kill_report = KillReport(killed=0, survivors={4242: "code"})
    out = asyncio.run(srv.launch_app("app", wait=1))
    assert out.startswith("ERROR:") and "successor was not launched" in out, out
    assert fake_backend.spawned == []
