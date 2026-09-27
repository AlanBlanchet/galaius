"""Bounded subprocess execution with cross-platform process-tree cancellation."""

import asyncio
import os
import signal
import subprocess
from pathlib import Path

_OUTPUT_LIMIT = 2 * 1024 * 1024
_STDIN_LIMIT = 1024 * 1024
_TERM_GRACE = 0.75
_PIPE_GRACE = 0.25


def _process_group_options() -> dict:
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


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


async def _stop_process_group(process: asyncio.subprocess.Process) -> None:
    """TERM, then KILL the complete process tree created by :func:`run_isolated_process`."""
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (AttributeError, ProcessLookupError, PermissionError):
            pass
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
        *argv,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        **_process_group_options(),
    )
    stdout_task = asyncio.create_task(_read_tail(process.stdout, output_limit))
    stderr_task = asyncio.create_task(_read_tail(process.stderr, output_limit))
    stdin_task = asyncio.create_task(_write_stdin(process.stdin, stdin or b""))
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError as exc:
        await _stop_process_group(process)
        raise TimeoutError(f"process timed out after {timeout:g}s") from exc
    except asyncio.CancelledError:
        await _stop_process_group(process)
        raise
    finally:
        stdout, stderr = await _finish_process_io(stdout_task, stderr_task, stdin_task)
    return process.returncode or 0, stdout, stderr
