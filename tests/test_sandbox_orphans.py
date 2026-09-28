"""Leaked Xephyr displays.

Two complaints, one cause. Every interact MCP server owns its own sandbox, so several servers
(one per editor window) means several Xephyr windows — "2 at the start". And when a server dies
without tearing down, its X server survives as an orphan: reparented to init, invisible to the
owner that could have cleaned it up, sitting on the user's screen until they kill it by hand.

An orphan is identifiable: it is OUR X server (our own flags), and its parent is gone.
"""

import os
import signal
from pathlib import Path

import pytest

from interact.desktop import orphans
from interact.desktop.backend import nested_server_command
from interact.server_registry import HARD_KILL


@pytest.fixture(autouse=True)
def _never_signal_a_real_process(monkeypatch):
    """No test in this module may signal or enumerate a REAL process.

    This module's whole subject is code that kills things, and its fixtures carry a real display
    number (":99"), so a test that patches only part of the path reaches live processes through
    the rest of it — one such test was collecting the pids of the running sandbox. Cutting both
    sinks here makes that impossible rather than merely unlikely; a test needing specific
    behaviour overrides these itself.
    """
    monkeypatch.setattr(orphans.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(orphans, "_all_pids", lambda: [])
    yield


def _proc(pid, ppid, cmdline):
    return orphans.XServer(pid=pid, ppid=ppid, cmdline=cmdline)


# Derived from the real command builder, never hand-typed: a fixture that drifts from what we
# actually spawn is how the reaper silently stops recognising our own displays.
_OURS = " ".join(nested_server_command(":99", "1280x800", headless=False))
#: One of OUR sandbox profiles (the editor's isolated --user-data-dir for display :99).
_PROFILE = str(Path.home() / ".interact/out/sandbox-profiles/editor-99")
#: Stand-in for SOME OTHER user's home — never under our own profile root, which is the whole
#: safety property under test below.
_OTHER_HOME = os.path.join(os.sep, "tmp", "other-user")


def test_an_orphan_is_ours_and_has_lost_its_parent():
    assert orphans.is_orphan(_proc(10, 1, _OURS)) is True


def test_a_live_parent_means_it_is_still_owned():
    # Someone is still using it — killing this would destroy a running session's display.
    assert orphans.is_orphan(_proc(10, 4242, _OURS)) is False


@pytest.mark.parametrize(
    "cmdline",
    [
        "Xephyr :50 -screen 800x600",              # a user's own Xephyr, not ours
        "Xvfb :77 -screen 0 1280x1024x24",         # not even the same server
        "/usr/bin/some-other-thing --noreset",
    ],
)
def test_a_foreign_x_server_is_never_touched(cmdline):
    # Killing a display we did not create is destroying someone else's work.
    assert orphans.is_orphan(_proc(10, 1, cmdline)) is False


def test_reaping_kills_only_the_orphans(monkeypatch):
    listed = [
        _proc(1, 1, _OURS),          # orphan
        _proc(2, 999, _OURS),        # owned
        _proc(3, 1, "Xephyr :50 -screen 800x600"),  # foreign
    ]
    killed: list[int] = []
    monkeypatch.setattr(orphans, "_list_x_servers", lambda: listed)
    monkeypatch.setattr(orphans, "_terminate", lambda pid: killed.append(pid) or True)
    assert orphans.reap_orphaned_displays() == [1]
    assert killed == [1]


def test_reaping_never_raises_when_nothing_can_be_listed(monkeypatch):
    # Cleanup is best-effort: it must never break the launch it runs before.
    monkeypatch.setattr(orphans, "_list_x_servers", lambda: (_ for _ in ()).throw(OSError("nope")))
    assert orphans.reap_orphaned_displays() == []


def test_a_process_that_merely_mentions_xephyr_is_not_an_x_server(monkeypatch):
    """Caught live: matching any command line CONTAINING "Xephyr" matched the very shell command
    that was hunting for orphans. Same self-match family as `pkill -f` on your own pattern — and
    here it would have sent SIGTERM to an innocent process."""
    ps_output = (
        "  10     1 Xephyr :99 -screen 1280x800 -br -ac -noreset -no-host-grab\n"
        "  11     1 python -c print('Xephyr -noreset -no-host-grab')\n"
        "  12     1 grep Xephyr -noreset -no-host-grab\n"
    )

    class _Done:
        stdout = ps_output

    monkeypatch.setattr(orphans.subprocess, "run", lambda *a, **k: _Done())
    found = orphans._list_x_servers()
    assert [s.pid for s in found] == [10], "matched something that only talks about Xephyr"


def test_an_orphan_adopted_by_systemd_is_still_an_orphan(monkeypatch):
    """Observed on the real machine: a leaked Xephyr's parent was `systemd --user`, not pid 1.
    Modern Linux user sessions register systemd as a child-subreaper, so orphans are adopted by
    it — a rule that only recognises pid 1 never fires, which is exactly why the leak survived."""
    monkeypatch.setattr(orphans, "_parent_is_reaper", lambda ppid: ppid == 1704)
    assert orphans.is_orphan(_proc(10, 1704, _OURS)) is True


def test_a_parent_that_is_a_real_owner_still_protects_it(monkeypatch):
    monkeypatch.setattr(orphans, "_parent_is_reaper", lambda ppid: False)
    assert orphans.is_orphan(_proc(10, 4242, _OURS)) is False


def test_systemd_user_is_recognised_as_a_reaper(monkeypatch):
    class _Done:
        stdout = "systemd\n"

    monkeypatch.setattr(orphans.subprocess, "run", lambda *a, **k: _Done())
    assert orphans._parent_is_reaper(1704) is True


# ── Clients that outlive the display ────────────────────────────────────────────────────────
# Killing the launcher's process GROUP is not enough: Chromium (so VS Code, and every Electron
# app) puts its helper processes in their own session, so killpg never reaches them. Observed
# live — after a sandbox teardown, `--type=gpu-process/renderer/utility` processes were still
# running under systemd, holding the profile lock and pushing the next launch onto a new display.
# A sandbox owns its display, so anything still bound to it at teardown is the sandbox's to clean.


def test_a_process_bound_to_the_sandbox_display_is_a_client(monkeypatch):
    monkeypatch.setattr(orphans, "_proc_display", lambda pid: ":99" if pid == 7 else ":0")
    monkeypatch.setattr(orphans, "_all_pids", lambda: [7, 8])
    assert orphans.display_clients(":99") == [7]


def test_the_server_and_its_own_direct_children_are_never_clients(monkeypatch):
    """The sweep must not signal the process running it — the self-kill trap again — nor the
    handles that process holds itself: a live ffmpeg recorder, a `maim` / `xdotool` in flight, a
    launched app's group leader are direct children bound to the same display, each managed by
    whatever spawned it. The sweep exists for what ESCAPED those handles — an app's helper that
    `setsid`d out of its group (#118) — and must not cut a recording short on the way."""
    monkeypatch.setattr(orphans, "_proc_display", lambda pid: ":99")
    monkeypatch.setattr(orphans, "_all_pids", lambda: [os.getpid(), 12, 13])
    monkeypatch.setattr(orphans, "_proc_ppid", lambda pid: os.getpid() if pid == 12 else 1)
    assert orphans.display_clients(":99") == [13]


def test_the_server_ancestors_are_never_clients(monkeypatch):
    parent = 20
    monkeypatch.setattr(orphans, "_proc_display", lambda pid: ":99")
    monkeypatch.setattr(orphans, "_all_pids", lambda: [parent, 13])
    monkeypatch.setattr(
        orphans,
        "_proc_ppid",
        lambda pid: parent if pid == os.getpid() else 1,
    )
    assert orphans.display_clients(":99") == [13]


@pytest.mark.parametrize("display", ["", None, ":0"])
def test_it_refuses_to_sweep_the_real_session_display(monkeypatch, display):
    """A bug that pointed this at the user's own display would kill their whole desktop."""
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(orphans, "_proc_display", lambda pid: ":0")
    monkeypatch.setattr(orphans, "_all_pids", lambda: [7])
    assert orphans.display_clients(display) == []


def test_killing_clients_reports_what_it_signalled(monkeypatch):
    monkeypatch.setattr(orphans, "display_clients", lambda d: [21, 22])
    monkeypatch.setattr(orphans, "_terminate", lambda pid: pid != 22)
    monkeypatch.setattr(orphans, "_still_alive", lambda pid: False)
    assert orphans.kill_display_clients(":99") == [21]


def test_proc_display_reads_the_processes_own_environment(tmp_path, monkeypatch):
    proc = tmp_path / "42"
    proc.mkdir()
    (proc / "environ").write_bytes(b"PATH=/bin\x00DISPLAY=:99\x00HOME=/home/x\x00")
    monkeypatch.setattr(orphans, "_PROC", tmp_path)
    assert orphans._proc_display(42) == ":99"


def test_proc_display_is_none_when_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(orphans, "_PROC", tmp_path)
    assert orphans._proc_display(999) is None


@pytest.mark.parametrize(
    ("source", "sweep"),
    [
        ("display_clients", lambda: orphans.kill_display_clients(":99", grace=0.1)),
        ("profile_clients", lambda: orphans.kill_profile_clients(_PROFILE, grace=0.1)),
    ],
    ids=["display", "profile"],
)
def test_a_client_that_ignores_SIGTERM_is_killed(monkeypatch, source, sweep):
    """Observed live: a survivor stayed up through SIGTERM and kept the profile's IPC socket, so
    the next launch handed its window to a displayless zombie and the sandbox stayed empty. Both
    sweeps escalate the same way — a profile holder that shrugs off SIGTERM keeps the singleton
    exactly like a display client does (#118)."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(orphans, source, lambda target: [31])
    monkeypatch.setattr(orphans.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    # Without this the grace loop calls the REAL os.kill(31, 0) and then SIGKILLs pid 31.
    monkeypatch.setattr(orphans, "_still_alive", lambda pid: len(sent) < 2)
    assert sweep() == [31]
    assert [sig for _, sig in sent] == [signal.SIGTERM, HARD_KILL]


# ── Never sweep a display we no longer own ──────────────────────────────────────────────────
# Display numbers are reclaimed the moment a Xephyr's lock drops, and several interact servers
# running at once is the NORMAL state. So if OUR X server died and server B claimed :99, sweeping
# ":99" on our way out would SIGKILL B's live sandbox. Our Xephyr still running is the proof that
# nobody else can hold that display — so that, and only that, licenses the sweep.


def test_teardown_sweeps_while_our_x_server_still_holds_the_display(monkeypatch):
    swept: list[str] = []
    monkeypatch.setattr(orphans, "kill_display_clients", lambda d, **k: swept.append(d) or [])
    orphans.sweep_if_owned(":99", owned=True)
    assert swept == [":99"]


def test_teardown_does_NOT_sweep_a_display_our_x_server_already_lost(monkeypatch):
    """Our Xephyr is gone → another server may already own :99 → its apps are not ours to kill."""
    swept: list[str] = []
    monkeypatch.setattr(orphans, "kill_display_clients", lambda d, **k: swept.append(d) or [])
    orphans.sweep_if_owned(":99", owned=False)
    assert swept == []


# ── Reaping an orphan must take its clients too ─────────────────────────────────────────────
# Killing the orphaned X server alone leaves its clients running and bound to a dead display —
# the very zombies that hold the editor profile socket and make the NEXT launch open nothing.
# The crash path has to clean both halves, exactly as teardown does.


def test_reaping_an_orphan_display_also_removes_its_clients(monkeypatch):
    swept: list[str] = []
    monkeypatch.setattr(orphans, "_list_x_servers",
                        lambda: [_proc(10, 1, _OURS + " :99 -screen 1280x800")])
    monkeypatch.setattr(orphans, "_parent_is_reaper", lambda ppid: True)
    monkeypatch.setattr(orphans, "_terminate", lambda pid: True)
    monkeypatch.setattr(orphans, "kill_display_clients", lambda d, **k: swept.append(d) or [])
    assert orphans.reap_orphaned_displays() == [10]
    assert swept == [":99"], "a reaped display's clients outlive it and break the next launch"


def test_the_display_a_server_serves_is_read_off_its_own_command_line():
    assert orphans.display_of("Xephyr :101 -screen 1280x800 -br -ac") == ":101"
    assert orphans.display_of("Xephyr -screen 1280x800") is None


def test_the_markers_still_match_the_command_we_actually_spawn():
    """`_OUR_MARKERS` re-states flags owned by `nested_server_command`. If that builder changes,
    the reaper silently stops recognising our OWN displays and the leak returns with no test
    failing — so bind them here."""
    argv = nested_server_command(":99", "1280x800", headless=False)
    assert os.path.basename(argv[0]) == orphans._OUR_EXECUTABLE
    joined = " ".join(argv)
    for marker in orphans._OUR_MARKERS:
        assert marker in joined, f"{marker!r} is no longer in the command we spawn"


def test_a_headless_xvfb_is_deliberately_left_alone():
    """Headless mode is invisible (no window, so no user-facing symptom) and its flags — `-screen`,
    `-nolisten tcp` — are what ANY Xvfb carries, so we could not tell ours from the user's. Not
    reaping it is a deliberate choice in favour of never killing someone else's server."""
    headless = " ".join(nested_server_command(":99", "1280x800", headless=True))
    assert orphans.is_orphan(_proc(10, 1, headless)) is False


def test_doctor_labels_an_ownerless_sandbox_so_the_extra_window_is_explained(capsys, monkeypatch):
    """"Why do I see two Xephyr windows?" — one per interact server is BY DESIGN, one with no
    owner left is the leak. The report has to tell those two apart, or every window looks wrong."""
    from interact.cli.app import _print_sandboxes

    monkeypatch.setattr(orphans, "_list_x_servers", lambda: [
        _proc(10, 1, _OURS),        # adopted → orphan
        _proc(11, 4242, _OURS),     # a live server still owns it
    ])
    monkeypatch.setattr(orphans, "_parent_is_reaper", lambda ppid: ppid == 1)
    monkeypatch.setattr(orphans, "display_clients", lambda d: [1, 2])
    _print_sandboxes(":1")
    out = capsys.readouterr().out
    assert "2 open" in out
    assert "ORPHANED" in out
    assert "owner pid 4242" in out


# ── The window says whose it is ─────────────────────────────────────────────────────────────
# "sometimes (usually at the start) i always see 2 xephyr windows" — one per interact server is
# by design, but an unlabelled `Xephyr on :99.0` window gives no way to know that. Titling it
# also gives us a marker WE control: `-noreset -no-host-grab` is the canonical hand-typed Xephyr
# line, so it could never really distinguish ours from someone else's.


def test_the_sandbox_window_says_what_it_is():
    argv = nested_server_command(":99", "1280x800", headless=False)
    assert "-title" in argv
    title = argv[argv.index("-title") + 1]
    assert "interact" in title.lower() and ":99" in title


def test_a_display_we_titled_is_recognised_as_ours():
    ours = " ".join(nested_server_command(":99", "1280x800", headless=False))
    assert orphans.is_orphan(_proc(10, 1, ours)) is True


def test_a_hand_started_xephyr_with_the_same_plain_flags_is_NOT_ours():
    """The old markers were exactly what someone types by hand, so they proved nothing."""
    hand_typed = "Xephyr :50 -screen 1280x800 -br -ac -noreset -no-host-grab"
    assert orphans.is_orphan(_proc(10, 1, hand_typed)) is False


# ── Sweeping by profile, not only by display ────────────────────────────────────────────────
# An editor launched into the sandbox can end up running on the REAL display while still using
# the sandbox PROFILE — measured live: a `code` process with
# `--user-data-dir=…/sandbox-profiles/editor-99` and `DISPLAY=:1`. A display sweep can never touch
# it (:1 is the user's own session, correctly off limits), so it survives every teardown, keeps
# the profile's singleton, and hands each new launch to its own stale extension host. That is what
# makes a rebuilt extension appear not to change and a launch appear to open nothing.


def test_a_process_holding_our_sandbox_profile_is_ours_wherever_it_runs(monkeypatch):
    monkeypatch.setattr(orphans, "_process_table", lambda: [
        (10, f"/usr/share/code/code --user-data-dir={_PROFILE} --type=renderer"),
        (11, f"/usr/share/code/code --user-data-dir={_OTHER_HOME}/.config/Code"),  # the user's own
        (os.getpid(), f"grep {_PROFILE}"),                                      # us, hunting
    ])
    assert orphans.profile_clients(_PROFILE) == [10]


def test_the_users_own_editor_is_never_swept(monkeypatch):
    monkeypatch.setattr(orphans, "_process_table", lambda: [
        (11, f"/usr/share/code/code --user-data-dir={_OTHER_HOME}/.config/Code"),
    ])
    assert orphans.profile_clients(_PROFILE) == []


@pytest.mark.parametrize("profile", ["", "/", _OTHER_HOME, f"{_OTHER_HOME}/.config/Code"])
def test_it_refuses_a_profile_outside_our_own_sandbox_directory(monkeypatch, profile):
    """The whole safety of this rests on the path being one WE created. Anything else could name
    the user's real profile — or their home — and sweep their editor."""
    monkeypatch.setattr(orphans, "_process_table", lambda: [(10, f"code --user-data-dir={profile}")])
    assert orphans.profile_clients(profile) == []


def test_it_refuses_a_lookalike_profile_root(monkeypatch, tmp_path):
    profile = tmp_path / ".interact" / "out" / "sandbox-profiles" / "editor-99"
    monkeypatch.setattr(orphans, "_process_table", lambda: [(10, f"code --user-data-dir={profile}")])
    assert orphans.profile_clients(str(profile)) == []


def test_the_process_table_is_read_untruncated(tmp_path, monkeypatch):
    """`ps -o args=` truncates to the terminal width, and Chromium puts `--user-data-dir` far into
    a very long command line — so the profile sweep matched nothing while `pgrep` found four
    processes. /proc/<pid>/cmdline is the untruncated, authoritative copy."""
    proc = tmp_path / "42"
    proc.mkdir()
    long_flag = f"--user-data-dir={_OTHER_HOME}/.interact/out/sandbox-profiles/editor-99"
    (proc / "cmdline").write_bytes(b"\0".join(
        [b"/usr/share/code/code", *(b"--padding-flag-that-is-long" for _ in range(40)),
         long_flag.encode()]
    ))
    monkeypatch.setattr(orphans, "_PROC", tmp_path)
    monkeypatch.setattr(orphans, "_all_pids", lambda: [42])  # the module fixture stubs this empty
    assert (42, ) == tuple(pid for pid, args in orphans._process_table() if long_flag in args)
    # The same untruncated read names a survivor in a kill report (#118).
    assert orphans.cmdline_of(42).endswith(long_flag)
    assert orphans.cmdline_of(43) is None
