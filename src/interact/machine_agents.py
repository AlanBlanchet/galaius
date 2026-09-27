"""The machine owner drives coding agents on this computer from the web, through its machine
connection: start one in a folder he allowed, read what it does, message it, stop it.

Everything a request may touch is decided HERE, on the machine, never by the server: the folders
(`MachineConfig.agent_roots`, set with `interact machine agent-roots`), the permission agents start
with (`agent_permission`, never bypass unless set here), and the runs a request may read or act on
(only runs started this way and what they launched: `WebRuns`). Starting and messaging go through
the launcher's own CLI (`interact agents spawn / send`) in a child process given the scrubbed
environment explicitly, so no request ever changes this process's environment."""

import json
import logging
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from interact_core import (
    MACHINE_AGENT_TAIL, AgentFoldersRequest, AgentRunsRequest, AgentSendRequest, AgentStartRequest, AgentStopRequest, AgentTailRequest,
    AgentTouchScope, MachineAgentAnswer, MachineAgentRequest, MachineAgentRun, MachineFileEntry,
)
from interact.agents import registry as reg
from interact.agents.run import load_policy
from interact.file_lock import exclusive

logger = logging.getLogger(__name__)

#: Web-started runs working at once on one computer; one more start is refused until one ends.
LIVE_WEB_RUNS = 4
#: What a cold `tail` (no cursor) reads back from the end of a run's stream.
COLD_TAIL = 48 * 1024
#: Lines one `tail` answer carries at most (`MachineAgentAnswer.lines`); the cursor stops after the last.
TAIL_LINES = 4000


class WebRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    root: str
    path: str = ""


class WebRuns(BaseModel):
    """The runs this computer started for the web, kept in one 0600 file beside the machine config:
    the only runs (with what they launched) a web request may read, message or stop."""

    model_config = ConfigDict(frozen=True)
    path: Path
    KEEP: ClassVar[int] = 500

    def read(self) -> tuple[WebRun, ...]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        return tuple(WebRun.model_validate(item) for item in raw)

    def add(self, run: WebRun) -> None:
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        with exclusive(descriptor):
            kept = (*self.read(), run)[-self.KEEP:]
            temporary = self.path.with_suffix(".tmp")
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump([item.model_dump(mode="json") for item in kept], stream)
            os.replace(temporary, self.path)


class MachineAgents(BaseModel):
    """Answers one MachineAgentRequest (already checked: this machine, signed, not expired), from
    the owner's CURRENT settings on this machine: its agent roots (name -> resolved folder), the
    permission web-started agents get, and whether agents run here at all."""

    model_config = ConfigDict(frozen=True)
    roots: dict[str, Path]
    permission: AgentTouchScope
    run_agents: bool
    #: The launcher session every web-started run is recorded under (one per machine).
    session: str
    runs: WebRuns
    environment: dict[str, str]
    #: How to run this installation's own CLI (the `interact` beside this interpreter).
    cli: tuple[str, ...] = Field(default_factory=lambda: MachineAgents.own_cli())

    @staticmethod
    def own_cli() -> tuple[str, ...]:
        beside = Path(sys.executable).with_name("interact")
        found = str(beside) if beside.is_file() else shutil.which("interact")
        if found is None:
            raise RuntimeError("the interact command is not installed beside this runner")
        return (found,)

    def answer(self, request: MachineAgentRequest) -> MachineAgentAnswer:
        if not self.run_agents:
            raise PermissionError("agents are off on this computer; its owner turns them on there with `interact machine agents on`")
        if not self.roots:
            raise PermissionError("no agent folders on this computer yet; its owner adds some there with `interact machine agent-roots <folder>`")
        match request:
            case AgentFoldersRequest():
                return self._folders(request)
            case AgentRunsRequest():
                return self._runs(request)
            case AgentTailRequest():
                return self._tail(request)
            case AgentStartRequest():
                return self._start(request)
            case AgentSendRequest():
                return self._send(request)
            case AgentStopRequest():
                return self._stop(request)

    @staticmethod
    def roles() -> tuple[str, ...]:
        """The roles `interact agents spawn --agent` accepts here: the launcher's active catalog."""
        try:
            return tuple(sorted(load_policy().agents))[:500]
        except (OSError, ValueError, RuntimeError) as error:
            logger.warning("agent roles unavailable: %s", error)
            return ()

    def folder(self, root: str, path: str) -> Path:
        """`path` beneath agent root `root`, walked part by part: every part a plain, unhidden
        folder, never a link. Raises PermissionError otherwise."""
        base = self.roots.get(root)
        if base is None:
            raise PermissionError(f"{root!r} is not an agent folder on this computer")
        current = base
        for part in (part for part in path.split("/") if part):
            if part in {".", ".."} or part.startswith(".") or "\\" in part:
                raise PermissionError("agent folders never include hidden or relative names")
            current = current / part
            try:
                facts = current.lstat()
            except FileNotFoundError:
                raise PermissionError(f"{root}/{path} does not exist") from None
            if not stat.S_ISDIR(facts.st_mode):
                raise PermissionError(f"{root}/{path} is not a plain folder (links are never followed)")
        return current

    def _folders(self, request: AgentFoldersRequest) -> MachineAgentAnswer:
        permission, roots = self.permission, tuple(sorted(self.roots))
        if not request.root:
            return MachineAgentAnswer(request_id=request.id, roots=roots, permission=permission, roles=self.roles())
        target = self.folder(request.root, request.path)
        names = []
        with os.scandir(target) as entries:
            for entry in entries:
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False):
                    names.append(entry.name)
        names.sort(key=str.lower)
        folders = tuple(MachineFileEntry(name=name, kind="folder") for name in names[:500])
        return MachineAgentAnswer(request_id=request.id, roots=roots, entries=folders, truncated=len(names) > 500, permission=permission)

    def _allowed(self) -> tuple[dict[str, WebRun], list[reg.AgentRun]]:
        started = {str(item.run_id): item for item in self.runs.read()}
        return started, reg.trees(frozenset(started))

    def _runs(self, request: AgentRunsRequest) -> MachineAgentAnswer:
        started, runs = self._allowed()
        found = []
        for run in runs[:200]:
            placed = started.get(run.run_id)
            root, path = (placed.root, placed.path) if placed else self._place(run.cwd)
            found.append(MachineAgentRun(
                run_id=UUID(run.run_id), name=run.name[:120], role=(run.agent or None) and run.agent[:80], provider=run.provider[:40],
                model=run.model and run.model[:120], status=run.status, root=root[:240], path=path[:1024], task=run.task[:8000],
                last=run.last[:400], started_at=run.started_at, finished_at=run.finished_at, cost_usd=run.cost_usd,
                parent_run_id=UUID(run.parent_run_id) if run.parent_run_id and run.run_id not in started else None,
            ))
        return MachineAgentAnswer(request_id=request.id, runs=tuple(found))

    def _place(self, cwd: str) -> tuple[str, str]:
        """(agent root, path beneath it) of a folder a launched child works in; ("", "") elsewhere."""
        try:
            folder = Path(cwd).resolve()
        except OSError:
            return "", ""
        for name, root in self.roots.items():
            if folder == root or root in folder.parents:
                return name, "" if folder == root else folder.relative_to(root).as_posix()
        return "", ""

    def _require_run(self, run_id: UUID) -> reg.AgentRun:
        _, runs = self._allowed()
        run = next((item for item in runs if item.run_id == str(run_id)), None)
        if run is None:
            raise PermissionError("this run was not started from the web on this computer")
        return run

    def _tail(self, request: AgentTailRequest) -> MachineAgentAnswer:
        self._require_run(request.run_id)
        path = reg.events_path(str(request.run_id))
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return MachineAgentAnswer(request_id=request.id, cursor=0)
        cursor = request.cursor
        start = max(0, size - COLD_TAIL) if cursor is None else (0 if cursor > size else cursor)
        end = min(size, start + MACHINE_AGENT_TAIL)
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(end - start)
        skipped = start > 0 and cursor is None
        if skipped:
            newline = data.find(b"\n")
            data, start = (data[newline + 1:], start + newline + 1) if newline >= 0 else (b"", end)
        # Whole lines only (one still being written is read next time), at most TAIL_LINES: the
        # cursor stops right after the last line sent, so the next read continues from there.
        whole, count = 0, 0
        while count < TAIL_LINES and (newline := data.find(b"\n", whole)) >= 0:
            whole, count = newline + 1, count + 1
        lines = tuple(line.decode("utf-8", "replace") for line in data[:whole].split(b"\n") if line)
        return MachineAgentAnswer(request_id=request.id, lines=lines, cursor=start + whole, truncated=skipped)

    def _run_cli(self, *arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*self.cli, *arguments], env=self.environment, capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)

    @staticmethod
    def _first_line(text: str) -> str:
        return next((line.strip() for line in text.splitlines() if line.strip()), "")[:400]

    def _start(self, request: AgentStartRequest) -> MachineAgentAnswer:
        _, runs = self._allowed()
        working = sum(1 for run in runs if run.status in {"running", "waiting"})
        if working >= LIVE_WEB_RUNS:
            raise PermissionError(f"{working} agents started from the web are already working on this computer; stop one first")
        folder = self.folder(request.root, request.path)
        options = ["--agent", request.role, "--cwd", str(folder), "--permission-mode", self.permission, "--session-id", self.session,
                   *(["--provider", request.provider] if request.provider is not None else [])]
        # "--" ends the options: a brief starting with "-" (a markdown bullet, "--help") is the brief.
        done = self._run_cli("agents", "spawn", *options, "--", request.text, timeout=120)
        output = done.stdout.strip().splitlines()
        try:
            run_id = UUID(output[-1].strip()) if done.returncode == 0 and output else None
        except ValueError:
            run_id = None
        if run_id is None:
            raise RuntimeError(self._first_line(done.stderr).removeprefix("ERROR: ") or f"the agent did not start (exit {done.returncode})")
        self.runs.add(WebRun(run_id=run_id, root=request.root, path=request.path))
        return MachineAgentAnswer(request_id=request.id, run_id=run_id)

    def _send(self, request: AgentSendRequest) -> MachineAgentAnswer:
        self._require_run(request.run_id)
        done = self._run_cli("agents", "send", "--", str(request.run_id), request.text, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(self._first_line(done.stdout + "\n" + done.stderr).removeprefix("ERROR: ") or f"not delivered (exit {done.returncode})")
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail=self._first_line(done.stdout))

    def _stop(self, request: AgentStopRequest) -> MachineAgentAnswer:
        run = self._require_run(request.run_id)
        if run.status not in {"running", "waiting"}:
            return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail="already ended")
        if not reg.stop(str(request.run_id)):
            raise RuntimeError("the run could not be stopped")
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail="stopped")
