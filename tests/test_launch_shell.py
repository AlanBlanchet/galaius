"""Agents naturally launch project apps with shell phrasing — `cd <repo> && uv run app` — but
launch_app exec'd the shlex argv verbatim, failing with the cryptic `[Errno 2] No such file or
directory: 'cd'` (hit as the FIRST attempt by two independent sessions driving a real app).
A command using shell syntax must run via a shell instead of raw exec."""

import os
import sys
from pathlib import Path

import pytest

from interact.launch import _browser_isolate, _clear_stale_locks, _editor_isolate, needs_shell
from interact.launch import apply_launch_rewrites


@pytest.mark.parametrize(
    "cmd",
    [
        "cd /tmp/project/proj && uv run app",  # the exact real-session failure
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


# ── VS Code (and other Electron editors) in the sandbox ──────────────────────────────────────
# An editor has the same singleton escape a browser does: launching `code` while an instance is
# running hands the request to that instance, which opens a window on the USER'S desktop. The
# sandbox then sits empty with no error — indistinguishable from a slow start.




def test_vscode_gets_its_own_user_data_dir():
    argv, note = _editor_isolate(["code", "/tmp/project/dev/interact"], ":99")
    joined = " ".join(argv)
    assert "--user-data-dir" in joined, "without this it joins the running instance"
    assert "99" in joined, "the profile is per-display so two sandboxes never share a lock"
    assert note


def test_electron_needs_software_gl_and_no_sandbox_on_a_nested_display():
    argv, _ = _editor_isolate(["code", "."], ":99")
    joined = " ".join(argv)
    # A nested X display has no usable hardware GL, and Electron's own sandbox fails without
    # user namespaces — both render a black window or refuse to start.
    assert "--disable-gpu" in joined
    assert "--no-sandbox" in joined


def test_a_non_editor_command_is_untouched():
    argv, note = _editor_isolate(["xterm"], ":99")
    assert argv == ["xterm"] and note == ""


def test_it_is_idempotent():
    once, _ = _editor_isolate(["code", "."], ":99")
    twice, _ = _editor_isolate(once, ":99")
    assert once == twice, "re-running the rewrite must not duplicate flags"


def test_the_rewrite_is_actually_applied_by_the_launcher():
    argv, note = apply_launch_rewrites(["code", "/tmp/proj"], ":99")
    assert "--user-data-dir" in " ".join(argv) and note


def test_first_run_modals_are_suppressed():
    """A fresh profile opens the welcome walkthrough and the workspace-trust modal, and each
    swallows the keystrokes an agent sends next — the editor then looks unresponsive for reasons
    unrelated to the task."""
    argv, _ = _editor_isolate(["code", "/tmp/p"], ":99")
    joined = " ".join(argv)
    for flag in ("--skip-welcome", "--disable-workspace-trust", "--skip-release-notes"):
        assert flag in joined, f"{flag} missing — a modal will eat the first keystrokes"


# ── Stale singleton locks in a sandbox profile ──────────────────────────────────────────────
# The profile is keyed per DISPLAY and outlives it: when the sandbox is torn down the editor's
# processes go, but its singleton lock file stays behind naming a pid that no longer exists. The
# next launch finds a lock it cannot join and exits WITHOUT EVER SHOWING A WINDOW — the sandbox
# just looks empty, with nothing in any log to say why. Observed live: `code.lock` held pid
# 2498082, long dead, and every launch into that profile silently produced nothing.




def _dead_pid() -> int:
    """A pid that is provably not running: fork a child and reap it.

    A large literal like 999999 is NOT safe — `/proc/sys/kernel/pid_max` is 4194304 on this
    machine, so that number is legal and reachable, and the test would silently be asserting
    against whatever process happened to hold it.
    """
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()  # reaped, so the pid is free and provably not running
    return child.pid


def test_a_lock_owned_by_a_dead_process_is_cleared(tmp_path):
    lock = tmp_path / "code.lock"
    lock.write_text(str(_dead_pid()))
    _clear_stale_locks(tmp_path)
    assert not lock.exists(), "a stale lock makes the next launch exit with an empty sandbox"


def test_a_lock_owned_by_a_LIVE_process_is_left_alone(tmp_path):
    lock = tmp_path / "code.lock"
    lock.write_text(str(os.getpid()))
    _clear_stale_locks(tmp_path)
    assert lock.exists(), "clearing a live lock would break a running editor's own profile"


def test_an_unparseable_lock_is_left_alone(tmp_path):
    """Only remove what we can PROVE is dead — never destroy a file we do not understand."""
    lock = tmp_path / "code.lock"
    lock.write_text("not-a-pid")
    _clear_stale_locks(tmp_path)
    assert lock.exists()


def test_a_chromium_singleton_symlink_is_handled_too(tmp_path):
    """Chromium's own lock is a symlink named <host>-<pid>, not a file with a pid inside."""
    lock = tmp_path / "SingletonLock"
    lock.symlink_to(f"somehost-{_dead_pid()}")
    _clear_stale_locks(tmp_path)
    assert not lock.is_symlink()


def test_a_missing_profile_is_not_an_error(tmp_path):
    _clear_stale_locks(tmp_path / "nope")  # must not raise — this runs before every launch


def test_the_isolation_rewrite_clears_the_lock_it_will_trip_over(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    profile = tmp_path / ".interact" / "out" / "sandbox-profiles" / "editor-99"
    profile.mkdir(parents=True)
    (profile / "code.lock").write_text(str(_dead_pid()))
    _editor_isolate(["code", "/tmp/p"], ":99")
    assert not (profile / "code.lock").exists()


def test_the_browser_profile_gets_its_stale_lock_cleared_too(tmp_path, monkeypatch):
    """Chromium's own `SingletonLock` is what `_PROFILE_LOCKS` names, and `_browser_isolate`
    creates the profiles that carry it — clearing it only for editors fixed the sibling that
    happened to be reported and left the one the lock is actually named after."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    profile = tmp_path / ".interact" / "out" / "sandbox-profiles" / "99-google-chrome"
    profile.mkdir(parents=True)
    (profile / "SingletonLock").symlink_to(f"host-{_dead_pid()}")
    _browser_isolate(["google-chrome", "https://example.com"], ":99")
    assert not (profile / "SingletonLock").is_symlink()
