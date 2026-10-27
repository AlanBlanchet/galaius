"""Stopping his own editor session from the web: the hook every Claude Code session runs ends a turn
it was asked to stop (`galaius.session_stop_hook`), and the PC installs that hook without touching
anything else in his Claude Code settings (`SessionStops`)."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from galaius_core import StopActor

from galaius import session_stop_hook
from galaius.agents.session_stops import HOOK_MARK, SessionStops

SESSION_ID = uuid4()
SESSION = str(SESSION_ID)
#: A tool call that lasts past the stop (Claude Code refuses a bare `sleep`).
WAIT = "python3 -c 'import time; time.sleep(30)'"


@pytest.fixture
def stops(tmp_path: Path) -> SessionStops:
    return SessionStops(folder=tmp_path / "stops", settings=tmp_path / ".claude" / "settings.json")


def _event(name: str, session: str = SESSION, **fields) -> dict:
    return {"hook_event_name": name, "session_id": session, "tool_name": "Bash", **fields}


@pytest.mark.parametrize(("event", "ends", "denies"), [
    ("PreToolUse", True, True),     # the next tool is not run, the turn ends
    ("PostToolUse", True, False),   # the tool just run was his before the stop: the turn ends after it
    ("Stop", False, False),         # a turn that ended by itself: nothing to end, the stop keeps holding
])
def test_a_stopped_session_ends_its_turn_at_its_next_tool_call(stops, event, ends, denies):
    stops.ask(SESSION_ID, StopActor(kind="person", name="Alan"))
    said = session_stop_hook.answer(_event(event), stops.folder, time.time())
    assert (said is not None and said["continue"] is False) == ends
    assert (said or {}).get("hookSpecificOutput", {}).get("permissionDecision") == ("deny" if denies else None)
    assert (stops.folder / f"{SESSION}.json").exists(), "it holds: a turn the session starts by itself is ended too"


@pytest.mark.parametrize(("event", "session", "age"), [
    ("UserPromptSubmit", SESSION, 0),                                   # he typed: his turn runs
    ("PreToolUse", SESSION, session_stop_hook.HOLD_SECONDS + 1),        # an old stop holds nothing
    ("PreToolUse", str(uuid4()), 0),                                    # another session of his: untouched
    ("PreToolUse", "../../etc/passwd", 0),                              # never a path out of the folder
])
def test_a_stop_never_ends_his_own_prompt_an_old_one_or_another_session(stops, event, session, age):
    stops.ask(SESSION_ID, None)
    assert session_stop_hook.answer(_event(event, session), stops.folder, time.time() + age) is None
    assert (stops.folder / f"{SESSION}.json").exists() == (session != SESSION)


def test_the_hook_goes_in_beside_his_own_settings_once(stops):
    """Every other hook and setting of his stays; a second install changes nothing; an older command
    of ours is replaced, never kept beside the new one."""
    stops.settings.parent.mkdir(parents=True)
    his = {"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/guard.py"}]}],
                                      "PostToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "old -m galaius.session_stop_hook /gone"}]}]}}
    stops.settings.write_text(json.dumps(his))
    assert stops.install() == "written" and stops.installed() and stops.installed_at() is not None
    written = stops.settings.read_text()
    assert stops.install() == "unchanged" and stops.settings.read_text() == written
    hooks = json.loads(written)["hooks"]
    assert json.loads(written)["model"] == "opus" and hooks["PreToolUse"][0] == his["hooks"]["PreToolUse"][0]
    assert all(sum(HOOK_MARK in json.dumps(group) for group in hooks[event]) == 1 for event in SessionStops.EVENTS)
    assert "/gone" not in written


def test_settings_that_are_not_json_are_never_overwritten(stops):
    stops.settings.parent.mkdir(parents=True)
    stops.settings.write_text("{ his half-typed settings")
    assert stops.install() == "not_json" and stops.settings.read_text() == "{ his half-typed settings"


@pytest.mark.skipif(sys.platform == "win32", reason="the shell pre-check is POSIX; Windows runs the interpreter directly")
def test_the_installed_command_starts_python_only_for_a_waiting_stop_and_clears_stale_ones(stops):
    """It runs at every tool call of every session: with no stop waiting it exits at once, printing nothing. A stop
    left for a session closed before its next tool call would start Python at every call of every other session:
    the first hook call past `HOLD_SECONDS` removes it."""
    stops.install()
    command = json.loads(stops.settings.read_text())["hooks"]["PreToolUse"][-1]["hooks"][0]["command"]
    run = lambda: subprocess.run(["sh", "-c", command], input=json.dumps(_event("PreToolUse")), capture_output=True, text=True, timeout=30, check=False)
    idle = run()
    assert idle.returncode == 0 and idle.stdout == ""
    closed = uuid4()
    stops.ask(closed, None)
    old = time.time() - session_stop_hook.HOLD_SECONDS - 1
    os.utime(stops.folder / f"{closed}.json", (old, old))
    assert run().stdout == "" and not (stops.folder / f"{closed}.json").exists()
    stops.ask(SESSION_ID, None)
    assert json.loads(run().stdout)["continue"] is False


def test_a_settings_change_while_installing_is_kept_and_uninstall_takes_out_only_ours(stops, monkeypatch):
    """Claude Code writes the same file (a `/permissions` change) with no lock to share: what it wrote between
    our read and our write survives. Leaving the account takes out our hook alone, every hook of his kept."""
    stops.settings.parent.mkdir(parents=True)
    guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/guard.py"}]}
    stops.settings.write_text(json.dumps({"hooks": {"PreToolUse": [guard]}}))
    read, raced = stops._read, []

    def his_write_lands_meanwhile():
        current = read()
        if not raced:
            raced.append(True)
            time.sleep(0.01)
            stops.settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"PreToolUse": [guard]}}))
        return current

    monkeypatch.setattr(stops, "_read", his_write_lands_meanwhile)
    assert stops.install() == "written" and stops.installed()
    assert json.loads(stops.settings.read_text())["permissions"] == {"allow": ["Bash(ls)"]}
    stops.ask(SESSION_ID, None)
    assert stops.uninstall() == "written" and not stops.installed() and list(stops.folder.iterdir()) == []
    assert json.loads(stops.settings.read_text()) == {"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"PreToolUse": [guard]}}


@pytest.mark.parametrize("gone", ["signed out", "folder deleted by hand"])
def test_a_hook_whose_script_is_gone_never_blocks_a_session(stops, gone):
    """A session open at sign-out keeps the hook it started with; a folder deleted without signing out leaves the
    hook in every session. Python finding no script exits 2, which Claude Code reads as « refuse this tool call,
    erase this prompt »: the hook must exit 0 saying nothing."""
    stops.install()
    stops.ask(SESSION_ID, None)
    if gone == "signed out":
        assert stops.uninstall() == "written"
    else:
        shutil.rmtree(stops.folder)
    for event in ("PreToolUse", "UserPromptSubmit"):
        done = subprocess.run(stops.argv(), input=json.dumps(_event(event)), capture_output=True, text=True, timeout=30, check=False)
        assert (done.returncode, done.stdout) == (0, ""), done.stderr


def test_the_guarded_hook_still_stops_the_session(stops):
    stops.install()
    stops.ask(SESSION_ID, None)
    done = subprocess.run(stops.argv(), input=json.dumps(_event("PreToolUse")), capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0 and json.loads(done.stdout)["continue"] is False, done.stderr


def test_settings_that_moved_lose_the_hook_the_new_ones_get(stops, tmp_path):
    """The service's CLAUDE_CONFIG_DIR changed between two connects: the old settings must not keep our hook forever."""
    old = stops.settings
    assert stops.install() == "written"
    moved = SessionStops(folder=stops.folder, settings=tmp_path / "elsewhere" / "settings.json", python=stops.python)
    assert moved.install() == "written" and moved.installed() and not stops.installed()
    assert HOOK_MARK not in old.read_text()
    assert moved.uninstall() == "written" and not moved.installed()


def test_sign_out_edits_the_settings_the_install_edited_whatever_its_shell_says(stops, tmp_path, monkeypatch):
    """The service installs with its environment, sign-out runs in his shell: a CLAUDE_CONFIG_DIR set in only one of
    them must not leave the hook behind. His dotfiles' link stays a link."""
    real = tmp_path / "dotfiles" / "claude-settings.json"
    real.parent.mkdir()
    real.write_text("{}")
    stops.settings.parent.mkdir(parents=True)
    stops.settings.symlink_to(real)
    assert stops.install() == "written" and stops.settings.is_symlink() and HOOK_MARK in real.read_text()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert SessionStops(folder=stops.folder).uninstall() == "written" and json.loads(real.read_text()) == {}


def test_settings_it_cannot_write_never_stop_the_pc_connecting(stops):
    """An OSError writing them (here `.claude` is a file; a folder Windows holds alike) is an outcome, never a raise
    out of `connect`."""
    stops.settings.parent.write_text("not a folder")
    assert stops.install() == "unwritable" and not stops.installed()


@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "win32" or shutil.which("claude") is None, reason="needs the Claude Code CLI, signed in (POSIX: reads its home)")
@pytest.mark.parametrize(("prompt", "asked_after"), [
    ("Run `{wait}` with Bash in the foreground, then run 'touch {ran}' with Bash, then say done.", 8),
    (("Use the Task tool once: a general-purpose sub-agent that runs `{wait}` with Bash in the foreground, then runs 'touch {ran}' with Bash. "
      "When it returns, run 'touch {ran}.after' with Bash yourself, then say done."), 25),
], ids=["own-turn", "sub-agent"])
def test_a_real_claude_session_stops_at_its_next_tool_call(stops, tmp_path, prompt, asked_after):
    """The seam itself: Claude Code reads the installed hook and ends the turn at its next tool call — a sub-agent's
    call too, and the session's own turn with it (nothing after the stop runs, and the turn ends well before its
    sleeps would)."""
    import pwd  # POSIX-only module, this test's alone

    session = uuid4()
    stops.install()
    ran = tmp_path / "ran"
    turn = subprocess.Popen(["claude", "-p", prompt.format(ran=ran, wait=WAIT), "--session-id", str(session),
                             "--settings", str(stops.settings), "--setting-sources", "project", "--allowedTools", "Bash,Task", "--output-format", "json"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "HOME": (home := pwd.getpwuid(os.getuid()).pw_dir)})  # signed in as the user, not the test's HOME
    time.sleep(asked_after)  # its sleep is running: the web's stop arrives now
    stops.ask(session, None)
    asked = time.monotonic()
    _, err = turn.communicate(timeout=240)
    assert turn.returncode == 0, err[-400:]
    assert not ran.exists() and not ran.with_name("ran.after").exists(), "a tool ran after the stop"
    assert time.monotonic() - asked < 60, "the turn went on after the stop"
    said = [path for path in Path(home, ".claude", "projects").glob(f"*/{session}*/**/*.jsonl") if session_stop_hook.SAID in path.read_text()]
    said += [path for path in Path(home, ".claude", "projects").glob(f"*/{session}.jsonl") if session_stop_hook.SAID in path.read_text()]
    assert said, "no tool call met the stop: the turn ended by itself"


def test_on_windows_the_hook_runs_without_a_shell(stops, monkeypatch):
    """Claude Code runs a Windows hook command through Git Bash or PowerShell, which read a quoted or backslashed path
    each their own way: the exec form (`args`) hands the interpreter its arguments with no shell at all."""
    monkeypatch.setattr(sys, "platform", "win32")
    hook = stops.hook()
    assert hook["command"] == str(stops.python) and [hook["command"], *hook["args"]] == stops.argv()
    assert SessionStops._ours({"hooks": [hook]})
