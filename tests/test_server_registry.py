"""A long-lived `interact mcp` server serves the code it imported at startup, so after the package
advances it silently runs OLD code until reconnected — the trap behind "I shipped the fix but the
bug persists". Servers register pid+version; the CLI flags any LIVE one behind the latest and prunes
dead pids. (This is exactly why the user's aino sandbox bug persisted: a v0.2.5 server never reconnected.)

Also carries the STALENESS-BY-CODE tests (formerly `test_server_code_staleness`): the version
comparison alone is not enough — the project deliberately does not bump on every change, so every
fix between releases leaves running servers frozen on old code while the version string still
matches. A server records the source it loaded at startup and staleness becomes a comparison
between two facts about ONE tree.
"""

import json
import os
import signal
import time
from pathlib import Path

import pytest

from interact import server_registry as sr
from interact import server_registry as reg  # kept for the staleness block's readability
from interact.cli import app_commands

# HOME isolation comes from tests/conftest.py's `_isolate_unit_configuration`; the runtime dir
# below is derived from HOME so nothing here has to redirect it.


def test_a_live_server_behind_the_latest_is_flagged(monkeypatch):
    monkeypatch.setattr(sr, "latest_version", lambda: "9.9.9")  # newer than whatever register writes
    path = sr.register_server()  # records THIS (live) pid + the installed version
    assert path and path.exists()
    stale = sr.stale_servers()
    assert any(s["pid"] == os.getpid() for s in stale)  # our pid runs an older version → flagged


def test_a_current_server_is_not_flagged(monkeypatch):
    monkeypatch.setattr(sr, "latest_version", sr.installed_version)  # matches what register writes
    sr.register_server()
    assert sr.stale_servers() == []  # version == latest → not stale


def test_dead_pid_registry_file_is_pruned():
    d = sr._runtime_dir()
    d.mkdir(parents=True, exist_ok=True)
    dead = d / "999999.json"
    dead.write_text(json.dumps({"pid": 999999, "version": "0.0.1"}))  # a pid that does not exist
    sr.stale_servers()
    assert not dead.exists()  # pruned, so a crashed server never lingers as a false positive


def test_alive_never_signals_the_process_on_windows(monkeypatch):
    """os.kill(pid, 0) is the POSIX liveness probe, but on Windows signal 0 == CTRL_C_EVENT — it
    sends Ctrl-C to the pid's console group and interrupts the caller. That KeyboardInterrupt broke
    Windows CI via test_dead_pid_registry_file_is_pruned (all tests passed, yet exit 1). On Windows
    _alive must use a non-signaling liveness check and must NEVER call os.kill (#73)."""
    monkeypatch.setattr(sr.sys, "platform", "win32")
    monkeypatch.setattr(sr, "_alive_windows", lambda pid: pid != 999999)  # stub the OpenProcess path
    signalled: list = []
    monkeypatch.setattr(sr.os, "kill", lambda *a, **k: signalled.append(a))
    assert sr._alive(999999) is False          # dead → its registry file is pruned
    assert sr._alive(4321) is True             # alive
    assert signalled == []                     # os.kill(pid, 0) would fire CTRL_C_EVENT — never call it


def test_unregister_removes_the_file():
    path = sr.register_server()
    assert path and path.exists()
    sr.unregister_server(path)
    assert not path.exists()


def test_is_interact_mcp_false_for_a_missing_pid():
    # The safety gate before killing: an unknown pid can't be confirmed as interact → never signalled.
    assert sr._is_interact_mcp(2_147_483_000) is False


def test_kill_stale_servers_only_signals_confirmed_interact_pids(monkeypatch):
    """`doctor --fix` must restart stale interact servers but NEVER a recycled pid an unrelated
    process now owns — so it only signals a pid whose cmdline still says interact, and prunes only
    that one's registry file."""
    monkeypatch.setattr(sr, "stale_servers", lambda: [{"pid": 111}, {"pid": 222}])
    monkeypatch.setattr(sr, "_is_interact_mcp", lambda pid: pid == 111)  # 222 = a recycled pid
    # This test is about WHICH pids are signalled, not about the SIGTERM→SIGKILL escalation. Left
    # unpatched it stops here: pid 111 is a live kernel thread on this box, so the escalation
    # correctly fires a second signal and the assertion below would be measuring that instead.
    monkeypatch.setattr(sr, "_still_running", lambda pid: False)
    signalled: list[int] = []
    monkeypatch.setattr(sr.os, "kill", lambda pid, sig: signalled.append(pid))
    d = sr._runtime_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "111.json").write_text("{}")
    (d / "222.json").write_text("{}")

    killed = sr.kill_stale_servers()

    assert killed == [111] and signalled == [111]   # the recycled pid is left untouched
    assert not (d / "111.json").exists()             # restarted server's registry file pruned
    assert (d / "222.json").exists()                 # recycled pid's file left alone


# ── A restart that does not restart ─────────────────────────────────────────────────────────
# `interact doctor --fix` sent SIGTERM and reported "restarted N stale server(s)". Measured on a
# real box: five of six servers ignored it and kept running on the old code, because the server
# blocks reading stdio and had no handler. The tool's claim was simply false.


def test_a_server_that_ignores_SIGTERM_is_killed(monkeypatch):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(sr, "stale_servers", lambda: [{"pid": 4242, "version": "0.1.0"}])
    monkeypatch.setattr(sr, "_is_interact_mcp", lambda pid: True)
    monkeypatch.setattr(sr.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(sr, "_still_running", lambda pid: len(sent) < 2)
    monkeypatch.setattr(sr, "_runtime_dir", lambda: Path("/nonexistent"))

    assert sr.kill_stale_servers() == [4242]
    assert [sig for _, sig in sent] == [signal.SIGTERM, signal.SIGKILL]


def test_a_server_that_stops_politely_is_not_killed(monkeypatch):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(sr, "stale_servers", lambda: [{"pid": 4242, "version": "0.1.0"}])
    monkeypatch.setattr(sr, "_is_interact_mcp", lambda pid: True)
    monkeypatch.setattr(sr.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(sr, "_still_running", lambda pid: False)
    monkeypatch.setattr(sr, "_runtime_dir", lambda: Path("/nonexistent"))

    sr.kill_stale_servers()
    assert [sig for _, sig in sent] == [signal.SIGTERM]


# ── Staleness by CODE, not just by version (formerly test_server_code_staleness) ──────────
#
# Comparing a server's start time to the CHECKER's source tree is wrong whenever they are not
# the same tree: the registry recorded only {pid, version}, so a doctor run from an editable
# checkout judged a server that may be running an entirely different install. It also needed
# /proc, absent on macOS and Windows, and mtimes a `git checkout` or a reproducible wheel can
# move either way.
#
# A server knows exactly what it loaded, so it records that at startup and staleness becomes a
# comparison between two facts about ONE tree.


def test_a_server_records_the_source_it_loaded(tmp_path, monkeypatch):
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "a.py").write_text("x")
    monkeypatch.setattr(reg, "_runtime_dir", lambda: tmp_path / "rt")
    monkeypatch.setattr(reg, "_source_root", lambda: src)

    path = reg.register_server()

    assert path is not None
    rec = json.loads(path.read_text())
    assert rec["source_root"] == str(src)
    assert rec["source_mtime"] >= (src / "a.py").stat().st_mtime


def test_a_server_whose_own_tree_moved_on_is_stale(tmp_path, monkeypatch):
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "a.py").write_text("x")
    monkeypatch.setattr(reg, "_runtime_dir", lambda: tmp_path / "rt")
    monkeypatch.setattr(reg, "_source_root", lambda: src)
    monkeypatch.setattr(reg, "latest_version", lambda: reg.installed_version())
    reg.register_server()

    (src / "a.py").write_text("edited after it started")  # a fix lands
    os.utime(src / "a.py", (time.time() + 5, time.time() + 5))

    stale = reg.stale_servers()
    assert [s["reason"] for s in stale] == ["code"], stale


def test_a_server_on_a_DIFFERENT_tree_is_judged_against_its_own(tmp_path, monkeypatch):
    """The bug the recorded root removes: two checkouts, and the doctor in one was judging the
    server in the other against the wrong source."""
    theirs, ours = tmp_path / "theirs", tmp_path / "ours"
    for d in (theirs, ours):
        d.mkdir()
        (d / "a.py").write_text("x")
    monkeypatch.setattr(reg, "_runtime_dir", lambda: tmp_path / "rt")
    monkeypatch.setattr(reg, "_source_root", lambda: theirs)
    monkeypatch.setattr(reg, "latest_version", lambda: reg.installed_version())
    reg.register_server()

    # OUR tree churns; the server running THEIRS is untouched by it.
    os.utime(ours / "a.py", (time.time() + 60, time.time() + 60))
    monkeypatch.setattr(reg, "_source_root", lambda: ours)

    assert reg.stale_servers() == [], "judged against the checker's tree instead of its own"


# ───────────── Stale-server repair (formerly test_stale_server_repair.py, #144) ────────────────
#
# A Codex session showed no interact tools while `interact status` reported stale MCP servers.
# Two gaps let that happen: `kill_stale_servers` claimed "restarted" for a pid it never re-checked
# after SIGKILL, and `status` had no `--fix` of its own — a second, undiscoverable command
# (`doctor --fix`) was the only repair. Both closed here.


def test_a_pid_that_survives_SIGKILL_is_not_reported_killed(monkeypatch):
    """A pid owned by another user (or otherwise unsignalable past SIGTERM) must never be handed
    back as "restarted" — the caller's success message would then be a claim this function cannot
    back with a fresh read (exactly what made #144's "restarted" message false)."""
    monkeypatch.setattr(sr, "stale_servers", lambda: [{"pid": 4242, "version": "0.1.0"}])
    monkeypatch.setattr(sr, "_is_interact_mcp", lambda pid: True)
    monkeypatch.setattr(sr.os, "kill", lambda pid, sig: None)  # both signals "succeed" to send
    monkeypatch.setattr(sr, "_still_running", lambda pid: True)  # yet the pid never actually dies
    monkeypatch.setattr(sr, "_runtime_dir", lambda: Path("/nonexistent"))

    assert sr.kill_stale_servers() == []  # signalled, but never confirmed dead → not returned


def test_a_pid_confirmed_dead_after_SIGKILL_is_reported_killed(monkeypatch):
    sent: list[int] = []

    def fake_still_running(pid):
        return len(sent) < 2  # dies only once SIGKILL (the 2nd signal) has been sent

    monkeypatch.setattr(sr, "stale_servers", lambda: [{"pid": 4242, "version": "0.1.0"}])
    monkeypatch.setattr(sr, "_is_interact_mcp", lambda pid: True)
    monkeypatch.setattr(sr.os, "kill", lambda pid, sig: sent.append(sig))
    monkeypatch.setattr(sr, "_still_running", fake_still_running)
    monkeypatch.setattr(sr, "_runtime_dir", lambda: Path("/nonexistent"))

    assert sr.kill_stale_servers() == [4242]


def test_status_command_accepts_fix(monkeypatch, tmp_path):
    """#144: `status` printed the stale-server warning but had no `--fix` of its own — repairing
    required knowing about a second command. It must be reachable from the command that reports it."""
    monkeypatch.setattr(app_commands, "stale_servers", lambda: [{"pid": 1, "version": "0.1.0", "reason": "version"}])
    monkeypatch.setattr(app_commands, "latest_version", lambda: "9.9.9")
    monkeypatch.setattr(app_commands, "kill_stale_servers", lambda: [1])
    monkeypatch.chdir(tmp_path)

    app_commands.status(fix=True)


def test_fix_reports_confirmed_stop_without_overclaiming_respawn(capsys):
    """The message must not assert every host reconnects automatically (#144's Codex host did
    not) — it states what is known (stopped) and names the manual fallback."""
    orig_stale, orig_latest, orig_kill = (
        app_commands.stale_servers, app_commands.latest_version, app_commands.kill_stale_servers,
    )
    try:
        app_commands.stale_servers = lambda: [{"pid": 1, "version": "0.1.0", "reason": "version"}]
        app_commands.latest_version = lambda: "9.9.9"
        app_commands.kill_stale_servers = lambda: [1]
        app_commands._print_stale_servers(fix=True)
    finally:
        app_commands.stale_servers, app_commands.latest_version, app_commands.kill_stale_servers = (
            orig_stale, orig_latest, orig_kill,
        )
    out = capsys.readouterr().out
    assert "stopped 1 stale MCP server" in out
    assert "reconnect" in out.lower()
    assert "guarantee every host reconnects" in out


def test_fix_names_the_unresolved_pid_and_the_manual_repair(capsys):
    orig_stale, orig_latest, orig_kill = (
        app_commands.stale_servers, app_commands.latest_version, app_commands.kill_stale_servers,
    )
    try:
        app_commands.stale_servers = lambda: [{"pid": 7, "version": "0.1.0", "reason": "version"}]
        app_commands.latest_version = lambda: "9.9.9"
        app_commands.kill_stale_servers = lambda: []  # could not confirm termination
        app_commands._print_stale_servers(fix=True)
    finally:
        app_commands.stale_servers, app_commands.latest_version, app_commands.kill_stale_servers = (
            orig_stale, orig_latest, orig_kill,
        )
    out = capsys.readouterr().out
    assert "pid 7" in out
    assert "v9.9.9" in out
    assert "reconnect" in out.lower() or "restart" in out.lower()
