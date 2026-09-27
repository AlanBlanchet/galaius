"""Track running MCP servers so the CLI can flag a STALE one.

A long-lived ``interact mcp`` process keeps the CODE it imported at startup. After the package
advances (an editable checkout especially — source is live but a running process is not), that
server silently serves OLD code until it is reconnected. This is the trap behind "I shipped the
fix but the bug persists": the maintainer's fix is committed, yet the editor's long-lived MCP
server still runs the pre-fix code.

Each server records its pid + version under ``<debug_dir>/runtime/`` at startup; ``interact
doctor``/``status`` read these, prune dead pids, and flag any LIVE server whose version is behind
the latest — naming the pid so the user knows exactly which editor connection to reconnect.
"""

import ctypes
import json
import os
import time
from contextlib import suppress
import sys
from pathlib import Path

from interact import installed_version


def _runtime_dir() -> Path:
    """A FIXED well-known path (``~/.interact/out/runtime``), NOT ``debug_dir``-relative. This is
    cross-process IPC — servers announce here, the CLI reads here — so every interact process must
    agree on the path regardless of an ``INTERACT_DEBUG_DIR`` override (that relocates debug OUTPUT,
    not runtime state; ``config.env`` likewise always lives at the fixed ``~/.interact``)."""
    return Path.home() / ".interact" / "out" / "runtime"


def _source_version() -> str | None:
    """The version in the source tree's pyproject.toml when interact runs from an editable checkout
    (``src/interact/`` below the repo root). None for a wheel install — there the
    installed metadata IS the latest, so no drift is possible without a reinstall+restart."""
    try:
        import tomllib

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        if pyproject.exists():
            return tomllib.loads(pyproject.read_text()).get("project", {}).get("version")
    except Exception:
        pass
    return None


def latest_version() -> str:
    """The newest interact version available on this machine: the editable SOURCE version if higher
    than the installed metadata (the source advanced without a reinstall), else the installed one."""
    return _source_version() or installed_version()


def register_server() -> Path | None:
    """Record this MCP server's pid, the version it loaded, and the SOURCE it loaded it from.

    Source is recorded because a server is the only thing that knows which tree it actually
    imported. Judging it later by comparing a process start time against whatever tree the
    CHECKER happens to be in gives the wrong answer the moment those differ — two checkouts on
    one machine, or a doctor run from an editable clone against a server running the installed
    package. Also needed /proc (Linux-only), and start times say nothing about WHICH code ran.
    """
    try:
        d = _runtime_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{os.getpid()}.json"
        root = _source_root()
        path.write_text(json.dumps({
            "pid": os.getpid(),
            "version": installed_version(),
            "source_root": str(root),
            "source_mtime": _newest_mtime(root),
        }))
        return path
    except OSError:
        return None


def unregister_server(path: Path | None) -> None:
    if path:
        try:
            path.unlink()
        except OSError:
            pass


def _still_running(pid: int) -> bool:
    """Liveness WITHOUT sending anything — reads the process table.

    The restart path must not probe with ``os.kill(pid, 0)``: every test mocks ``os.kill`` to
    record what was sent, so a probe through the same call counts as a signal and the test
    measures its own polling. Reading /proc keeps probe and signal separate.
    """
    if sys.platform == "win32":
        return _alive_windows(pid)
    return Path(f"/proc/{pid}").exists()


def _alive(pid: int) -> bool:
    # os.kill(pid, 0) is the POSIX liveness probe, but on Windows signal 0 IS CTRL_C_EVENT:
    # os.kill would GenerateConsoleCtrlEvent, sending Ctrl-C to the pid's console group and
    # interrupting US — the KeyboardInterrupt that broke Windows CI (every test passed, yet exit
    # 1, #73). Query the process handle instead; sends no signal.
    if sys.platform == "win32":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user — still running
    return True


def _alive_windows(pid: int) -> bool:
    """Liveness via OpenProcess — no signal sent. A pid we can open whose exit code is STILL_ACTIVE
    is running; one we cannot open (or that has exited) is dead, so its registry file is pruned."""
    STILL_ACTIVE = 259
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p  # HANDLE is pointer-sized — don't truncate on 64-bit
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _is_interact_mcp(pid: int) -> bool:
    """Confirm a pid is genuinely an ``interact mcp`` process before we'd signal it. A registry pid
    can be recycled by an unrelated process after the server died but before its file was pruned —
    killing that would be a serious bug, so we only ever act on a pid whose cmdline still says
    interact. Reads ``/proc`` (Linux); anywhere without it, returns False → nothing is killed."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    cmdline = raw.replace(b"\0", b" ").decode("utf-8", "replace")
    return "interact" in cmdline and "mcp" in cmdline


def kill_stale_servers() -> list[int]:
    """Stop every stale MCP server so its editor respawns it on current code — the opt-in
    ``interact doctor --fix`` / ``interact status --fix``. Only signals a pid whose cmdline still
    confirms it's ``interact mcp`` (a recycled pid is left untouched), prunes its registry file, and
    returns only the pids CONFIRMED terminated. Best-effort: a pid that's gone or unsignalable is
    skipped, never raised.

    SIGTERM first, then SIGKILL for anything still standing. Measured on a real box, five of six
    servers once ignored SIGTERM entirely — the server blocks reading stdio, versions before the
    teardown handler have nothing to catch it — while this reported them all "restarted". A
    restart command leaving old code running is worse than none: the user then believes the fix
    reached them. Same reasoning applies past SIGKILL (#144): a pid this returns as killed is read
    back from ``/proc`` one more time, never assumed dead just because a signal was accepted — the
    caller's "restarted" message must never be a claim this function cannot back with a fresh read.
    """
    import signal
    import time

    signalled: list[int] = []
    for info in stale_servers():
        pid = info.get("pid")
        if not isinstance(pid, int) or not _is_interact_mcp(pid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        signalled.append(pid)
        (_runtime_dir() / f"{pid}.json").unlink(missing_ok=True)
    if not signalled:
        return signalled
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and any(_still_running(pid) for pid in signalled):
        time.sleep(0.1)
    for pid in signalled:
        if _still_running(pid):
            with suppress(OSError):
                os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and any(_still_running(pid) for pid in signalled):
        time.sleep(0.05)
    return [pid for pid in signalled if not _still_running(pid)]


def _source_root() -> Path:
    """The tree this process imported interact from."""
    return Path(__file__).resolve().parent


def _newest_mtime(root: Path, pattern: str = "*.py") -> float:
    """When anything under ``root`` was last written.

    An editable install serves the tree directly, so this moves every time a fix lands — exactly
    when a long-lived server becomes stale, exactly when the version string does NOT move (this
    project bumps once per release, not once per change).
    """
    newest = 0.0
    try:
        for f in root.rglob(pattern):
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return 0.0
    return newest


def _process_start(pid: int) -> float | None:
    """Wall-clock seconds at which ``pid`` started, or None if it is gone or unreadable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        # field 22 (1-based) is starttime in clock ticks since boot; everything after the comm
        # field is fixed-width, and comm can itself contain spaces, so split from the last ')'.
        fields = stat[stat.rindex(")") + 2:].split()
        ticks = float(fields[19])
    except (ValueError, IndexError):
        return None
    hz = os.sysconf("SC_CLK_TCK")
    try:
        with open("/proc/uptime") as fh:
            uptime = float(fh.read().split()[0])
    except OSError:
        return None
    return time.time() - uptime + ticks / hz


def stale_servers() -> list[dict]:
    """Live MCP servers whose loaded version is behind :func:`latest_version` (→ serving old code;
    reconnect them). Prunes registry files for dead pids as a side effect, so a crashed server
    doesn't linger as a false positive."""
    d = _runtime_dir()
    if not d.exists():
        return []
    latest = latest_version()
    out: list[dict] = []
    for f in d.glob("*.json"):
        try:
            info = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        pid = info.get("pid")
        if not isinstance(pid, int) or not _alive(pid):
            f.unlink(missing_ok=True)  # dead → prune
            continue
        if info.get("version") != latest:
            out.append({**info, "reason": "version"})
            continue
        # Same version, older code. Between releases this is the ONLY way to see it, and the
        # common case: version moves once per release, code moves every fix. Compared against the
        # tree the SERVER recorded, not the one this checker happens to be running in.
        root, loaded = info.get("source_root"), info.get("source_mtime")
        if root and loaded is not None and loaded < _newest_mtime(Path(root)):
            out.append({**info, "reason": "code"})
    return out
