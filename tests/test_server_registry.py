"""A long-lived `interact mcp` server serves the code it imported at startup, so after the package
advances it silently runs OLD code until reconnected — the trap behind "I shipped the fix but the
bug persists". Servers register pid+version; the CLI flags any LIVE one behind the latest and prunes
dead pids. (This is exactly why the user's aino sandbox bug persisted: a v0.2.5 server never reconnected.)"""

import json
import os

import pytest

from interact import server_registry as sr


@pytest.fixture(autouse=True)
def _runtime_in_tmp(monkeypatch, tmp_path):
    # The registry lives at ~/.interact/runtime (fixed, home-based) — redirect HOME so the test never
    # touches the real one.
    monkeypatch.setenv("HOME", str(tmp_path))


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
