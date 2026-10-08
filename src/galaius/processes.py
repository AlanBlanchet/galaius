"""Bounded subprocess execution with cross-platform process-tree cancellation."""

import asyncio
import ctypes
import ctypes.util
import functools
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict

if sys.platform == "win32":
    from ctypes import wintypes

_OUTPUT_LIMIT = 2 * 1024 * 1024
_STDIN_LIMIT = 1024 * 1024
_TERM_GRACE = 0.75
_PIPE_GRACE = 0.25


#: What cmd.exe re-reads inside a batch file's `%*` (expansion, command separators, quoting,
#: and a line break ending the command): an argument carrying one cannot reach a `.cmd` intact.
_BATCH_UNSAFE = re.compile(r'[\r\n"%^&|<>]')


class NpmShim(BaseModel):
    """The `.cmd` launcher npm writes for a package's command on Windows (its `cmd-shim`):
    `"%_prog%" <args> "%dp0%\\<script>" %*` for a script run by an interpreter (`node`, set just
    above it, preferring a `node.exe` beside the shim), `"%dp0%\\<program>" %*` for a native one."""

    model_config = ConfigDict(frozen=True)
    _launch: ClassVar[re.Pattern[str]] = re.compile(r'^(?:.*&\s*"%_prog%"(?P<args>.*?))?\s*"%dp0%\\(?P<target>[^"]+)"\s+%\*\s*$')
    _program: ClassVar[re.Pattern[str]] = re.compile(r'^\s*SET "_prog=(?P<program>[^"]+)"\s*$', re.IGNORECASE)

    argv: tuple[str, ...]

    @classmethod
    def read(cls, shim: Path, search_path: str | None) -> Self | None:
        """The command `shim` starts, or None when it is not an npm launcher."""
        lines = shim.read_text(encoding="utf-8", errors="replace").splitlines()
        launch = next((match for line in reversed(lines) if (match := cls._launch.match(line))), None)
        if launch is None:
            return None
        target = str(shim.parent / launch["target"])
        if launch["args"] is None:
            return cls(argv=(target,))
        # In the shim's own order: `IF EXIST "%dp0%\node.exe"` first, else `node` on PATH.
        for program in (match["program"] for line in lines if (match := cls._program.match(line))):
            beside = program.startswith("%dp0%\\")
            found = str(shim.parent / program.removeprefix("%dp0%\\")) if beside else shutil.which(program, path=search_path)
            if found is not None and Path(found).is_file():
                return cls(argv=(found, *launch["args"].split(), target))
        return None


def spawnable(argv: Sequence[str], env: Mapping[str, str] | None = None) -> list[str]:
    """`argv` as the OS can start it, program looked up on `env`'s PATH (this process's by default).

    POSIX exec already searches PATH, so argv is unchanged there. Windows' CreateProcess finds only
    `.exe` files, while npm installs `codex` / `claude` as `.cmd` shims that cmd.exe would re-parse
    (a line break ends the command, `&` starts another): a bare name is resolved on PATH and an npm
    shim replaced by the interpreter + script it launches, so every argument arrives as written.
    """
    if sys.platform != "win32":
        return list(argv)
    search_path = (os.environ if env is None else env).get("PATH")
    program, *arguments = argv
    resolved = program if Path(program).parent != Path() else shutil.which(program, path=search_path)
    if resolved is None:
        return list(argv)  # not installed: the spawn itself says so, as exec does on POSIX
    if Path(resolved).suffix.lower() not in {".cmd", ".bat"}:
        return [resolved, *arguments]
    shim = NpmShim.read(Path(resolved), search_path)
    if shim is not None:
        return [*shim.argv, *arguments]
    if any(_BATCH_UNSAFE.search(argument) for argument in arguments):
        raise OSError(f"{resolved} is a batch script cmd.exe would rewrite this command for; install its .exe")
    return [resolved, *arguments]


def process_group_options() -> dict:
    """Spawn options making the child lead its own process tree, so `stop_process_tree` reaches
    every descendant (POSIX: a new session; Windows: a new process group)."""
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


def end_process_tree(pid: int) -> bool:
    """Ask the tree `pid` leads (spawned with `process_group_options`) to stop, without waiting:
    POSIX TERMs its group; Windows has no signal a console-less process receives, so `taskkill /T /F`.
    False when nothing could be signalled."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            return True
        return subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


async def _read_tail(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    """Drain a pipe without allowing unbounded vendor/tool output into memory."""
    if stream is None:
        return b""
    kept = bytearray()
    while chunk := await stream.read(64 * 1024):
        kept.extend(chunk)
        if len(kept) > limit:
            del kept[:-limit]
    return bytes(kept)


async def _write_stdin(
    stream: asyncio.StreamWriter | None, content: bytes
) -> None:
    if stream is None:
        return
    try:
        stream.write(content)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()
        try:
            await stream.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


def _signal_group(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    """POSIX only (Windows ends the tree with `taskkill` in `stop_process_tree`)."""
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def stop_process_tree(process: asyncio.subprocess.Process) -> None:
    """TERM, then KILL the complete process tree of a child spawned with `process_group_options`.
    Windows: `taskkill /T /F` only. No console event (Ctrl-Break / Ctrl-C) is ever sent: one aimed
    at a child that does not lead its own group reaches every process on the console, this one and
    whatever started it included."""
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=_TERM_GRACE)
        except (TimeoutError, OSError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=_TERM_GRACE)
        except TimeoutError:
            pass
        return

    _signal_group(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=_TERM_GRACE)
    except TimeoutError:
        pass
    # The group leader may exit while a descendant ignores TERM, so KILL the group regardless.
    _signal_group(process, signal.SIGKILL)
    try:
        await asyncio.wait_for(process.wait(), timeout=_TERM_GRACE)
    except TimeoutError:
        pass


async def _finish_process_io(
    stdout_task: asyncio.Task[bytes],
    stderr_task: asyncio.Task[bytes],
    stdin_task: asyncio.Task[None],
) -> tuple[bytes, bytes]:
    """Bound pipe draining after process exit/kill and cancel readers that retain inherited FDs."""
    tasks = (stdout_task, stderr_task, stdin_task)
    _, pending = await asyncio.wait(tasks, timeout=_PIPE_GRACE)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=_PIPE_GRACE)

    def bytes_result(task: asyncio.Task[bytes]) -> bytes:
        if not task.done() or task.cancelled():
            return b""
        try:
            return task.result()
        except (OSError, asyncio.CancelledError):
            return b""

    return bytes_result(stdout_task), bytes_result(stderr_task)


async def run_isolated_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    output_limit: int = _OUTPUT_LIMIT,
    stdin: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    """Run argv in an isolated process group and kill all descendants on timeout/cancellation."""
    if stdin is not None and len(stdin) > _STDIN_LIMIT:
        raise ValueError(f"stdin input exceeds {_STDIN_LIMIT} bytes")
    process = await asyncio.create_subprocess_exec(
        *spawnable(argv, env),
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        **process_group_options(),
    )
    stdout_task = asyncio.create_task(_read_tail(process.stdout, output_limit))
    stderr_task = asyncio.create_task(_read_tail(process.stderr, output_limit))
    stdin_task = asyncio.create_task(_write_stdin(process.stdin, stdin or b""))
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError as exc:
        await stop_process_tree(process)
        raise TimeoutError(f"process timed out after {timeout:g}s") from exc
    except asyncio.CancelledError:
        await stop_process_tree(process)
        raise
    finally:
        stdout, stderr = await _finish_process_io(stdout_task, stderr_task, stdin_task)
    return process.returncode or 0, stdout, stderr


def process_started(pid: int) -> int | None:
    """When process `pid` started, as an opaque number only compared for equality: with the pid it
    names one process for good (a reused pid starts later). Linux: clock ticks since boot
    (`/proc/<pid>/stat`); Windows: its creation FILETIME; macOS: its start in microseconds (libproc's
    BSD info). None when it is gone or the system has no such record."""
    if sys.platform == "win32":
        return _started_windows(pid)
    if sys.platform == "darwin":
        return _started_darwin(pid)
    fields = _stat_fields(pid)
    return int(fields[19]) if fields is not None and len(fields) > 19 else None  # field 22, `starttime`


def process_exited(pid: int) -> bool:
    """Whether `pid` is a process that has EXITED but still holds its pid: a zombie its parent has not
    collected yet (Linux state Z, or X while being removed). Every pid probe (`kill(pid, 0)`, `/proc/<pid>`)
    still finds it, so a liveness test without this keeps a finished run « running » for as long as its
    parent never waits on it. False where there is no `/proc` (it cannot tell)."""
    fields = _stat_fields(pid)
    return fields is not None and bool(fields) and fields[0] in ("Z", "X")


def process_unit(pid: int) -> str | None:
    """The systemd unit holding `pid` (a `run-….scope` / `….service`), from its cgroup v2 path: the journal
    records how that unit ENDED (out of memory, a failure) under its name, never under the pid. None off
    systemd, for a pid already gone, or in a cgroup no unit owns."""
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except OSError:
        return None
    path = next((line.partition("::")[2] for line in lines if line.startswith("0::")), "")
    leaf = path.rstrip("/").rpartition("/")[2]
    return leaf if leaf.endswith((".scope", ".service")) else None


def _stat_fields(pid: int) -> list[str] | None:
    """`/proc/<pid>/stat` from field 3 (`state`) on, or None when it cannot be read."""
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # `comm` (field 2) may hold spaces and parentheses: fields restart after its last ')'.
    return stat_line.rpartition(")")[2].split()


class _ProcBsdInfo(ctypes.Structure):
    """`struct proc_bsdinfo` (<sys/proc_info.h>), up to the start time."""

    _fields_ = [
        ("ids", ctypes.c_uint32 * 12),  # flags, status, xstatus, pid, ppid, uid, gid, ruid, rgid, svuid, svgid, rfu
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("counts", ctypes.c_uint32 * 6),  # nfiles, pgid, pjobc, tdev, tpgid, nice
        ("start_sec", ctypes.c_uint64),
        ("start_usec", ctypes.c_uint64),
    ]


@functools.cache
def _libproc() -> ctypes.CDLL:
    libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
    libproc.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    libproc.proc_pidinfo.restype = ctypes.c_int
    return libproc


def _started_darwin(pid: int) -> int | None:
    """`pbi_start_tvsec` / `pbi_start_tvusec` of `proc_pidinfo(pid, PROC_PIDTBSDINFO)`; None when
    libproc fills less than the whole struct (the process is gone)."""
    info = _ProcBsdInfo()
    filled = _libproc().proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))  # PROC_PIDTBSDINFO
    if filled != ctypes.sizeof(info):
        return None
    return info.start_sec * 1_000_000 + info.start_usec


def _started_windows(pid: int) -> int | None:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return created.dwHighDateTime << 32 | created.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)
