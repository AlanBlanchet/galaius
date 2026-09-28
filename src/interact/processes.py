"""Bounded subprocess execution with cross-platform process-tree cancellation."""

import asyncio
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
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass


async def stop_process_tree(process: asyncio.subprocess.Process) -> None:
    """TERM, then KILL the complete process tree of a child spawned with `process_group_options`
    (Windows: Ctrl-Break to its group, then `taskkill /T /F`)."""
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (AttributeError, OSError):
            pass  # no console to raise it through, or already gone: taskkill below still ends it
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
    (`/proc/<pid>/stat`); Windows: its creation FILETIME. None when it is gone or the system has
    no such record here (macOS)."""
    if sys.platform == "win32":
        return _started_windows(pid)
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # `comm` (field 2) may hold spaces and parentheses: fields restart after its last ')'.
    fields = stat_line.rpartition(")")[2].split()
    return int(fields[19]) if len(fields) > 19 else None  # field 22, `starttime`


def _started_windows(pid: int) -> int | None:
    import ctypes
    from ctypes import wintypes

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
