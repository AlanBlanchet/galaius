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
from galaius.agents.session_stops import HOOK_MODULE, SessionStops

SESSION_ID = uuid4()
SESSION = str(SESSION_ID)


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
                                      "PostToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": f"old -m {HOOK_MODULE} /gone"}]}]}}
    stops.settings.write_text(json.dumps(his))
    assert stops.install() and stops.installed() and stops.installed_at() is not None
    written = stops.settings.read_text()
    assert stops.install() and stops.settings.read_text() == written
    hooks = json.loads(written)["hooks"]
    assert json.loads(written)["model"] == "opus" and hooks["PreToolUse"][0] == his["hooks"]["PreToolUse"][0]
    assert all(sum(HOOK_MODULE in json.dumps(group) for group in hooks[event]) == 1 for event in SessionStops.EVENTS)
    assert "/gone" not in written


def test_settings_that_are_not_json_are_never_overwritten(stops):
    stops.settings.parent.mkdir(parents=True)
    stops.settings.write_text("{ his half-typed settings")
    assert not stops.install() and stops.settings.read_text() == "{ his half-typed settings"


@pytest.mark.skipif(sys.platform == "win32", reason="the shell pre-check is POSIX; Windows runs the interpreter directly")
def test_the_installed_command_starts_python_only_for_a_waiting_stop(stops, monkeypatch):
    """It runs at every tool call of every session: with no stop waiting it exits at once, printing nothing.
    (This checkout's interpreter, not an installed launcher's runtime, which may predate the hook.)"""
    monkeypatch.setattr("galaius.agents.session_stops.shutil.which", lambda name: None)
    monkeypatch.setattr("galaius.agents.session_stops.active_interpreter", lambda: sys.executable)
    stops.install()
    command = json.loads(stops.settings.read_text())["hooks"]["PreToolUse"][-1]["hooks"][0]["command"]
    run = lambda: subprocess.run(["sh", "-c", command], input=json.dumps(_event("PreToolUse")), capture_output=True, text=True, timeout=30, check=False)
    idle = run()
    assert idle.returncode == 0 and idle.stdout == ""
    stops.ask(SESSION_ID, None)
    assert json.loads(run().stdout)["continue"] is False


@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "win32" or shutil.which("claude") is None, reason="needs the Claude Code CLI, signed in (POSIX: reads its home)")
def test_a_real_claude_session_stops_at_its_next_tool_call(stops, tmp_path, monkeypatch):
    """The seam itself: Claude Code reads the installed hook and ends the turn before its first tool runs."""
    import pwd  # POSIX-only module, this test's alone

    monkeypatch.setattr("galaius.agents.session_stops.shutil.which", lambda name: None)
    monkeypatch.setattr("galaius.agents.session_stops.active_interpreter", lambda: sys.executable)
    session = uuid4()
    stops.install()
    marker = tmp_path / "ran"
    turn = subprocess.Popen(["claude", "-p", f"Run 'sleep 12' with Bash, then run 'touch {marker}' with Bash, then say done.", "--session-id", str(session),
                             "--settings", str(stops.settings), "--setting-sources", "project", "--allowedTools", "Bash", "--output-format", "json"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "HOME": pwd.getpwuid(os.getuid()).pw_dir})  # signed in as the user, not the test's HOME
    time.sleep(8)  # his prompt is in and its first tool is running: the web's stop arrives now
    stops.ask(session, None)
    _, err = turn.communicate(timeout=180)
    assert turn.returncode == 0, err[-400:]
    assert not marker.exists(), "the next tool ran: the stop did not reach the session"
