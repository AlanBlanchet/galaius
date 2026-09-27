"""Clean up sandbox X servers whose owner is gone.

Every interact MCP server owns its own sandbox, so a user with several editor windows genuinely
has several servers — each spawns its own Xephyr on first launch. That part is by design.

What is NOT by design: when a server dies without tearing down (crash, reload, killed session),
its X server survives — reparented to init, nothing remembers it exists, sits on the user's
screen until killed by hand. This finds those and removes them.

An orphan must satisfy BOTH conditions — one of OURS (spawned with our own flags) AND its parent
is gone. Killing on either alone would destroy a display someone's still using, or a Xephyr the
user started themselves.
"""

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass

from interact.desktop.backend import SANDBOX_TITLE
from pathlib import Path

#: What one of OUR sandbox displays looks like. Re-states flags owned by
#: `backend.nested_server_command`; `test_the_markers_still_match_the_command_we_actually_spawn`
#: binds them together, so changing that builder fails a test instead of silently switching the
#: reaper off.
#:
#: HEADLESS (Xvfb) displays are deliberately NOT reaped. They render no window, cause none of
#: the symptoms this exists for, and their flags (`-screen`, `-nolisten tcp`) are what ANY Xvfb
#: carries — we couldn't tell ours from one the user started. Not reclaiming a resource is a
#: much cheaper mistake than killing someone else's server.
_OUR_EXECUTABLE = "Xephyr"
#: Our own window title, which nobody else sets — unlike `-noreset -no-host-grab`, which is
#: exactly the line a person types by hand and so never really identified anything.
_OUR_MARKERS = (SANDBOX_TITLE,)


@dataclass
class XServer:
    pid: int
    ppid: int
    cmdline: str


def _list_x_servers() -> list[XServer]:
    """Every running Xephyr, with its parent. Uses ps so there is no dependency to install."""
    out = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True, timeout=10, check=True
    ).stdout
    servers = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        # Match the EXECUTABLE, never the text. Testing `"Xephyr" in cmdline` matched the very
        # command hunting for orphans (mentions the name), a grep, anything else saying the word
        # — then signalled them. Same self-match trap as `pkill -f <own pattern>`.
        argv0 = parts[2].split()[0]
        if os.path.basename(argv0) != _OUR_EXECUTABLE:
            continue
        try:
            servers.append(XServer(pid=int(parts[0]), ppid=int(parts[1]), cmdline=parts[2]))
        except ValueError:
            continue
    return servers


def process_alive(pid: int) -> bool:
    """Whether a pid is running. PermissionError means it EXISTS and isn't ours — alive.

    One definition: the two copies of this used to disagree on exactly that case (one read it
    as dead), and this one decides whether something gets killed.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _terminate(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


#: Processes that ADOPT orphans. A modern Linux user session registers `systemd --user` as a
#: child-subreaper, so a dead owner's children reparent to IT rather than pid 1 — observed
#: directly on this machine, where a leaked Xephyr sat under systemd and a pid-1-only rule
#: never fired.
_REAPER_NAMES = ("systemd", "init", "launchd")


def _parent_is_reaper(ppid: int) -> bool:
    if ppid in (0, 1):
        return True
    try:
        comm = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(ppid)],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except Exception:
        return False  # cannot tell → assume it is owned; never kill on a guess
    return os.path.basename(comm) in _REAPER_NAMES


def is_orphan(server: XServer) -> bool:
    """Ours AND adopted. A dead owner's X server is reparented — to pid 1, or on a systemd user
    session to `systemd --user` — and the process itself cannot tell us who used to own it."""
    if not all(marker in server.cmdline for marker in _OUR_MARKERS):
        return False  # not one of ours — never touch a display we did not create
    return _parent_is_reaper(server.ppid)


#: Where our sandbox profiles live. A child path here is one WE created, per display, so killing
#: what holds it cannot reach the user's own editor — which uses ~/.config/Code.
_PROFILE_ROOT = Path.home() / ".interact" / "out" / "sandbox-profiles"


def _process_table() -> list[tuple[int, str]]:
    """(pid, cmdline) for every process, read from /proc.

    NOT via ``ps -o args=``, which truncates to the terminal width: Chromium puts
    ``--user-data-dir`` far into a several-thousand-character command line, so the flag fell off
    the end — sweep matched nothing while ``pgrep`` found four processes holding the profile.
    """
    return [(pid, cmdline) for pid in _all_pids() if (cmdline := cmdline_of(pid)) is not None]


def profile_clients(profile: str) -> list[int]:
    """Processes holding one of OUR sandbox profiles — minus this one.

    Sweeping by DISPLAY isn't enough. An editor launched into the sandbox can end up on the real
    display while still using the sandbox profile (measured: `--user-data-dir=…/editor-99` with
    `DISPLAY=:1`), which a display sweep must never follow. It then keeps the profile's
    singleton, so every later launch hands to that stale instance instead of starting fresh —
    looks like a rebuilt extension not changing, or a launch opening nothing.

    Refuses any path outside our own profile directory: that constraint is the entire safety
    argument, since the user's real editor profile would otherwise match.
    """
    profile_path = Path(profile)
    if not profile or profile_path == _PROFILE_ROOT or _PROFILE_ROOT not in profile_path.parents:
        return []
    return [pid for pid, args in _process_table()
            if f"--user-data-dir={profile}" in args.split() and not _own_handle(pid)]


def kill_profile_clients(profile: str, *, grace: float = 1.5) -> list[int]:
    """Terminate profile holders, escalating after a bounded grace period."""
    return _kill_escalating(profile_clients(profile), grace)


def display_of(cmdline: str) -> str | None:
    """The display an X server serves, read off its own command line (`Xephyr :99 …`)."""
    return next((tok for tok in cmdline.split()[1:] if re.fullmatch(r":\d+", tok)), None)


def sweep_if_owned(display: str | None, *, owned: bool) -> list[int]:
    """Sweep a display's clients only while WE still hold it.

    Display numbers are reclaimed the instant an X server's lock drops, and several interact
    servers at once is normal — so a sweep keyed on a display string must first prove our own X
    server is still running on it. Otherwise a server whose Xephyr died would kill the live
    sandbox of whichever server has since claimed that number.
    """
    return kill_display_clients(display) if owned else []


def reap_orphaned_displays() -> list[int]:
    """Terminate every orphaned sandbox display; returns the pids signalled.

    Best-effort by construction: it runs immediately before a launch, so a failure to enumerate
    must never be the reason a launch fails.
    """
    try:
        servers = _list_x_servers()
    except Exception:
        return []
    killed = []
    for server in servers:
        if not is_orphan(server):
            continue
        # Its CLIENTS outlive it: keep running bound to a dead display, hold the editor
        # profile's socket, so the next launch hands its window to a zombie and opens nothing.
        # Killing the X server alone fixes the window on screen, leaves the real breakage.
        kill_display_clients(display_of(server.cmdline))
        if _terminate(server.pid):
            killed.append(server.pid)
    return killed


# ── Clients that outlive the display ────────────────────────────────────────────────────────

_PROC = Path("/proc")


def _all_pids() -> list[int]:
    try:
        return [int(p.name) for p in _PROC.iterdir() if p.name.isdigit()]
    except OSError:
        return []


def _proc_display(pid: int) -> str | None:
    """Which X display a process is bound to, read from its OWN environment.

    Authoritative and app-agnostic: it does not care whether the process is an editor, a browser
    or a terminal, only which display it would draw on.
    """
    try:
        raw = (_PROC / str(pid) / "environ").read_bytes()
    except OSError:
        return None  # gone, or another user's process we cannot read
    for entry in raw.split(b"\0"):
        if entry.startswith(b"DISPLAY="):
            return entry[len(b"DISPLAY="):].decode("utf-8", "replace")
    return None


def _proc_ppid(pid: int) -> int | None:
    """A process's parent, read from its own /proc status — None once it is gone."""
    try:
        status = (_PROC / str(pid) / "status").read_text()
    except OSError:
        return None
    for line in status.splitlines():
        if not line.startswith("PPid:"):
            continue
        try:
            return int(line.split()[1])
        except (IndexError, ValueError):
            return None
    return None


def cmdline_of(pid: int) -> str | None:
    """A process's full command line, read untruncated from /proc."""
    try:
        raw = (_PROC / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def _own_ancestors() -> set[int]:
    """Pids in this server's current parent chain, stopping at an unreadable or root entry."""
    ancestors: set[int] = set()
    parent = _proc_ppid(os.getpid())
    while parent not in (None, 0, 1) and parent not in ancestors:
        ancestors.add(parent)
        parent = _proc_ppid(parent)
    return ancestors


def _own_handle(pid: int) -> bool:
    """Whether this process is this server, its ancestor, or a direct child handle."""
    me = os.getpid()
    return pid == me or pid in _own_ancestors() or _proc_ppid(pid) == me


def display_clients(display: str | None) -> list[int]:
    """Every process still bound to a sandbox display — minus this one's handles.

    Killing the launcher's process GROUP misses these: Chromium (so VS Code and every Electron
    app) puts its helpers in their own session, so `killpg` never reaches them. They outlive the
    display, hold the profile lock, push the next launch onto a different display.
    """
    if not display or display == os.environ.get("DISPLAY"):
        return []  # never sweep the user's real session — that would kill their whole desktop
    return [pid for pid in _all_pids()
            if _proc_display(pid) == display and not _own_handle(pid)]


def _still_alive(pid: int) -> bool:
    return process_alive(pid)


def _await_exit(pids: list[int], timeout: float) -> list[int]:
    """Wait, bounded by ``timeout``, for signalled processes to exit."""
    deadline = time.monotonic() + timeout
    alive = [pid for pid in pids if _still_alive(pid)]
    while alive and time.monotonic() < deadline:
        time.sleep(0.05)
        alive = [pid for pid in alive if _still_alive(pid)]
    return alive


def _kill_escalating(pids: list[int], grace: float) -> list[int]:
    """SIGTERM, bounded grace, then SIGKILL for survivors."""
    signalled = [pid for pid in pids if _terminate(pid)]
    stubborn = _await_exit(signalled, grace)
    for pid in stubborn:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    _await_exit(stubborn, grace)
    return signalled


def kill_display_clients(display: str | None, *, grace: float = 1.5) -> list[int]:
    """Terminate everything left on a sandbox display, escalating; returns the pids signalled.

    A polite SIGTERM isn't always enough — a survivor that ignores it keeps the profile's IPC
    socket open, and the next launch hands its window to that displayless zombie instead of
    opening one, so the sandbox stays empty with no error anywhere.
    """
    return _kill_escalating(display_clients(display), grace)
