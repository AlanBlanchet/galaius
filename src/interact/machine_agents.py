"""The machine owner drives coding agents on this computer from the web, through its machine
connection: start one in a folder he allowed, read what it does, message it, stop it.

Everything a request may touch is decided HERE, on the machine, never by the server: the folders
(`MachineConfig.agent_roots`, set with `interact machine agent-roots`), the permission agents start
with (`agent_permission`, never bypass unless set here), and the runs a request may read or act on
(only runs started this way and what they launched: `WebRuns`). Starting and messaging go through
the launcher's own CLI (`interact agents spawn / send`) in a child process given the scrubbed
environment explicitly, so no request ever changes this process's environment."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from interact_core import MACHINE_AGENT_TAIL, MachineAgentAnswer, MachineAgentRequest, MachineAgentRun, MachineFileEntry
from interact.agents import registry as reg
from interact.file_lock import exclusive

if TYPE_CHECKING:
    from interact.machines import MachineConfig

#: Web-started runs working at once on one computer; one more start is refused until one ends.
LIVE_WEB_RUNS = 4
#: What a cold `tail` (no cursor) reads back from the end of a run's stream.
COLD_TAIL = 48 * 1024


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
    """Answers one MachineAgentRequest (already checked: this machine, signed, not expired)."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)
    config: "MachineConfig"
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
        if not self.config.run_agents:
            raise PermissionError("agents are off on this computer; its owner turns them on there with `interact machine agents on`")
        roots = self.roots()
        if not roots:
            raise PermissionError("no agent folders on this computer yet; its owner adds some there with `interact machine agent-roots <folder>`")
        return {"folders": self._folders, "runs": self._runs, "tail": self._tail, "start": self._start, "send": self._send, "stop": self._stop}[request.op](request, roots)

    def roots(self) -> dict[str, Path]:
        base = self.config.working_directory.resolve()
        return {root.relative_to(base).as_posix(): root for root in self.config.usable_agent_roots()[0]}

    @staticmethod
    def folder(roots: dict[str, Path], root: str, path: str) -> Path:
        """`path` beneath agent root `root`, walked part by part: every part a plain, unhidden
        folder, never a link. Raises PermissionError otherwise."""
        base = roots.get(root)
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

    def _folders(self, request: MachineAgentRequest, roots: dict[str, Path]) -> MachineAgentAnswer:
        permission = self.config.agent_permission
        if not request.root:
            return MachineAgentAnswer(request_id=request.id, roots=tuple(sorted(roots)), permission=permission)
        target = self.folder(roots, request.root, request.path)
        names = []
        with os.scandir(target) as entries:
            for entry in entries:
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False):
                    names.append(entry.name)
        names.sort(key=str.lower)
        folders = tuple(MachineFileEntry(name=name, kind="folder") for name in names[:500])
        return MachineAgentAnswer(request_id=request.id, roots=tuple(sorted(roots)), entries=folders, truncated=len(names) > 500, permission=permission)

    def _allowed(self) -> tuple[dict[str, WebRun], list[reg.AgentRun]]:
        started = {str(item.run_id): item for item in self.runs.read()}
        return started, reg.trees(frozenset(started))

    def _runs(self, request: MachineAgentRequest, roots: dict[str, Path]) -> MachineAgentAnswer:
        started, runs = self._allowed()
        base = self.config.working_directory.resolve()
        found = []
        for run in runs[:200]:
            placed = started.get(run.run_id)
            root, path = (placed.root, placed.path) if placed else self._place(run.cwd, base)
            found.append(MachineAgentRun(
                run_id=UUID(run.run_id), name=run.name[:120], role=(run.agent or None) and run.agent[:80], provider=run.provider[:40],
                model=run.model and run.model[:120], status=run.status, root=root[:240], path=path[:1024], task=run.task[:8000],
                last=run.last[:400], started_at=run.started_at, finished_at=run.finished_at, cost_usd=run.cost_usd,
                parent_run_id=UUID(run.parent_run_id) if run.parent_run_id and run.run_id not in started else None,
            ))
        return MachineAgentAnswer(request_id=request.id, runs=tuple(found))

    @staticmethod
    def _place(cwd: str, base: Path) -> tuple[str, str]:
        try:
            relative = Path(cwd).resolve().relative_to(base).as_posix()
        except (ValueError, OSError):
            return "", ""
        root, _, path = relative.partition("/")
        return root, path

    def _require_run(self, run_id: UUID) -> reg.AgentRun:
        _, runs = self._allowed()
        run = next((item for item in runs if item.run_id == str(run_id)), None)
        if run is None:
            raise PermissionError("this run was not started from the web on this computer")
        return run

    def _tail(self, request: MachineAgentRequest, roots: dict[str, Path]) -> MachineAgentAnswer:
        assert request.run_id is not None
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
        whole = data.rfind(b"\n") + 1  # a line still being written is read next time
        lines = tuple(line.decode("utf-8", "replace") for line in data[:whole].split(b"\n") if line)
        return MachineAgentAnswer(request_id=request.id, lines=lines[-4000:], cursor=start + whole, truncated=skipped)

    def _run_cli(self, *arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*self.cli, *arguments], env=self.environment, capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)

    @staticmethod
    def _first_line(text: str) -> str:
        return next((line.strip() for line in text.splitlines() if line.strip()), "")[:400]

    def _start(self, request: MachineAgentRequest, roots: dict[str, Path]) -> MachineAgentAnswer:
        _, runs = self._allowed()
        working = sum(1 for run in runs if run.status in {"running", "waiting"})
        if working >= LIVE_WEB_RUNS:
            raise PermissionError(f"{working} agents started from the web are already working on this computer; stop one first")
        folder = self.folder(roots, request.root, request.path)
        arguments = ["agents", "spawn", request.text, "--agent", str(request.role), "--cwd", str(folder),
                     "--permission-mode", self.config.agent_permission, "--session-id", f"web-{self.config.machine_id}"]
        if request.provider is not None:
            arguments += ["--provider", request.provider]
        done = self._run_cli(*arguments, timeout=120)
        output = done.stdout.strip().splitlines()
        try:
            run_id = UUID(output[-1].strip()) if done.returncode == 0 and output else None
        except ValueError:
            run_id = None
        if run_id is None:
            raise RuntimeError(self._first_line(done.stderr).removeprefix("ERROR: ") or f"the agent did not start (exit {done.returncode})")
        self.runs.add(WebRun(run_id=run_id, root=request.root, path=request.path))
        return MachineAgentAnswer(request_id=request.id, run_id=run_id)

    def _send(self, request: MachineAgentRequest, roots: dict[str, Path]) -> MachineAgentAnswer:
        assert request.run_id is not None
        self._require_run(request.run_id)
        done = self._run_cli("agents", "send", str(request.run_id), request.text, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(self._first_line(done.stdout + "\n" + done.stderr).removeprefix("ERROR: ") or f"not delivered (exit {done.returncode})")
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail=self._first_line(done.stdout))

    def _stop(self, request: MachineAgentRequest, roots: dict[str, Path]) -> MachineAgentAnswer:
        assert request.run_id is not None
        run = self._require_run(request.run_id)
        if run.status not in {"running", "waiting"}:
            return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail="already ended")
        if not reg.stop(str(request.run_id)):
            raise RuntimeError("the run could not be stopped")
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail="stopped")
