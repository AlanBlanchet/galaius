"""The machine owner drives coding agents on this computer from the web, through its machine
connection, as the editor panel does: start an agent (a role) or a session (a conversation that
asks before commands) in a folder he allowed, on a model he picks, read what it does, answer what
it asks, message it, stop it; continue one of his own editor conversations; read the logs.

Everything a request may touch is checked HERE, on the machine, against its CURRENT settings: the
folders (`MachineConfig.agent_roots`), the permission agents start with (`agent_permission`; a
start may ask for less, never more; bypass only when set on the PC itself), and the runs a request
may read or act on (only runs started this way and what they launched: `WebRuns`). The owner sets
those settings on the PC or on its web page (`MachineAgentSettings`, while `remote_settings` is on). Starting and messaging go through
the launcher's own CLI (`galaius agents spawn / send`) in a child process given the scrubbed
environment explicitly, so no request ever changes this process's environment."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from galaius_core import (
    AGENT_TOUCH_SCOPES, MACHINE_AGENT_MEDIA, MACHINE_AGENT_TAIL, AgentAnswerRequest, AgentMedia, AgentMediaRequest, image_type, AgentContinueRequest, AgentFoldersRequest, AgentInteraction, AgentLogsRequest, AgentOptionsRequest,
    AgentProviderState, AgentProviderSwitchRequest, AgentRunKind, AgentRunsRequest, AgentSettingsRequest, ToolRoleModels, AgentSendRequest, AgentSessionsRequest, AgentStartRequest, AgentStopRequest, AgentTailRequest, AgentTouchScope,
    MachineAgentAnswer, MachineAgentModel, MachineAgentRequest, MachineAgentRun, PassedOverCandidate, media_key, media_paths, MachineAgentSession, MachineFileEntry, WorkspacePrepareRequest, WorkspacesRequest,
)
from galaius.agents import registry as reg
from galaius.agents.host import ConversationHost, ConversationRefused
from galaius.agents.messaging import deliver_message
from galaius.agents.providers import PROJECT_SETTINGS_OFF, PROVIDERS
from galaius.agents.run import LAUNCH_STAMP, launch_editor_turn, load_policy, rank_candidates
from galaius.fence import EGRESS, FenceSpec, available
from galaius.file_lock import exclusive
from galaius.machine_workspaces import MachineWorkspaces
from galaius.place_reviews import PlaceReviews
from galaius.places import LEVEL_RANK, PlaceMap, split
from galaius.project_secrets import ProjectEnv
from cryptography.exceptions import InvalidTag
from galaius_core.sealing import SecretsSeal

logger = logging.getLogger(__name__)

#: Web-started runs working at once on one computer; one more start is refused until one ends.
LIVE_WEB_RUNS = 4
#: What a cold `tail` (no cursor) reads back from the end of a run's stream.
COLD_TAIL = 48 * 1024
#: Lines one `tail` answer carries at most (`MachineAgentAnswer.lines`); the cursor stops after the last.
TAIL_LINES = 4000
#: The CLIs agents run on here, and the ranking the model list follows: every benchmarked model,
#: most capable first (the order a role's own criterion walks).
AGENT_PROVIDERS = ("claude", "codex")
MODEL_RANKING = "aa.intelligence and price.in >= 0"
MODELS_PER_PROVIDER = 12
#: A release date closing a model id: -20260416 or -2026-04-16.
_DATED = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
#: Editor conversations offered for continuing: written in the last two weeks, newest first.
EDITOR_SESSIONS_DAYS, EDITOR_SESSIONS_MAX = 14, 40
#: An editor conversation written this recently is open in the editor right now.
EDITOR_LIVE_SECONDS = 120


class LogRing(logging.Handler):
    """This machine connection's recent log lines, for the owner reading them from the web: only
    galaius's own loggers at INFO and above (a transport at DEBUG logs request headers), each
    line redacted before it is kept (`redact`), the ring bounded."""

    def __init__(self, keep: int = 400, secrets: tuple[str, ...] = ()) -> None:
        super().__init__(logging.INFO)
        self.lines: deque[str] = deque(maxlen=keep)
        self.secrets = secrets
        self.addFilter(lambda record: record.name == "galaius" or record.name.startswith("galaius."))

    def emit(self, record: logging.LogRecord) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        self.lines.append(redact(f"{stamp} {record.levelname.lower():7} {record.name.removeprefix('galaius.')}: {record.getMessage()}", self.secrets)[:600])


#: Credential shapes never shown on the web, whatever line carries them.
_SECRET_SHAPES = re.compile(
    r"(?i)(bearer\s+)\S+|\b(iwm_|iwk_|sk-|ghp_|gho_|github_pat_|xox[bpas]-)[A-Za-z0-9_\-]{6,}|\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}|\bAKIA[0-9A-Z]{16}\b|(://[^/\s:@]+:)[^@\s]+@"
)


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """`text` with credential shapes and the exact `secrets` (this runner's own key values) masked."""
    masked = _SECRET_SHAPES.sub(lambda found: (found.group(1) or found.group(2) or found.group(3) or "") + "•••", text)
    for secret in secrets:
        masked = masked.replace(secret, "•••")
    return masked


def secret_values(environment: dict[str, str]) -> tuple[str, ...]:
    """The values in `environment` that are credentials (their names say so), longest first."""
    named = (value for key, value in environment.items() if re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", key, re.I) and len(value) >= 8)
    return tuple(sorted(set(named), key=len, reverse=True))


def interaction_digest(interaction: dict) -> str:
    """sha256 of an approval request as the machine holds it (its JSON, without its own digest)."""
    unsigned = {key: item for key, item in interaction.items() if key != "digest"}
    return hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class WebRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    root: str
    path: str = ""
    kind: AgentRunKind = "agent"
    #: A continued copy: the editor conversation it copies (its history opens the copy's transcript).
    source: UUID | None = None
    #: Started inside the machine's OS fence (`galaius.fence`).
    fenced: bool = False


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

    def runs(self) -> list[reg.AgentRun]:
        """These runs and every run they launched, as the launcher's registry has them now."""
        return reg.trees(frozenset(str(item.run_id) for item in self.read()))

    def stop_live(self) -> tuple[int, int]:
        """Stops every one still working (agents switched off here): (how many stopped, how many
        could not be - each failure logged, never stopping the rest)."""
        stopped = failed = 0
        try:
            runs = self.runs()
        except (OSError, ValueError) as error:
            logger.warning("web runs unreadable; none stopped: %s", error)
            return 0, 1
        for run in runs:
            if run.status not in {"running", "waiting"}:
                continue
            try:
                stopped += bool(reg.stop(run.run_id))
            except (OSError, RuntimeError, ValueError) as error:
                failed += 1
                logger.warning("could not stop run %s: %s", run.run_id[:8], error)
        return stopped, failed


class MachineSessions:
    """The conversations ("sessions") this machine hosts for the web, for the runner's lifetime:
    the same host the editor's chat uses (`ConversationHost`), opened on first use. A session asks
    before a command or a file change; those questions are what the web answers."""

    def __init__(self, working_directory: Path) -> None:
        self.working_directory = working_directory
        self._host: ConversationHost | None = None
        self._lock = asyncio.Lock()
        #: Messages to a session that was mid-turn, delivered in order once each turn ends.
        self._queued: dict[str, list[str]] = {}
        self._deliveries: dict[str, asyncio.Task] = {}

    async def send(self, run_id: str, text: str) -> str:
        """"sent", or "queued" when the session is still working on a turn: it gets the message
        as soon as that turn ends (as in the editor, where you can type while it works)."""
        if self._queued.get(run_id):
            self._queued[run_id].append(text)
            return "queued"
        try:
            await (await self.host()).send(run_id, text)
            return "sent"
        except ConversationRefused as error:
            if error.code != "conflict":
                raise
        self._queued.setdefault(run_id, []).append(text)
        if run_id not in self._deliveries:
            self._deliveries[run_id] = asyncio.get_running_loop().create_task(self._deliver(run_id))
        return "queued"

    async def _deliver(self, run_id: str, every: float = 2.0) -> None:
        try:
            while self._queued.get(run_id):
                await asyncio.sleep(every)
                try:
                    await (await self.host()).send(run_id, self._queued[run_id][0])
                except ConversationRefused as error:
                    if error.code == "conflict":
                        continue  # still on its turn
                    logger.warning("queued message to session %s not delivered: %s", run_id, error)
                    self._queued.pop(run_id, None)
                    return
                self._queued[run_id].pop(0)
        finally:
            self._deliveries.pop(run_id, None)
            if not self._queued.get(run_id):
                self._queued.pop(run_id, None)

    @property
    def busy(self) -> bool:
        """A session is on a turn or has messages waiting for one."""
        return bool(self._queued) or (self._host is not None and self._host.busy)

    async def host(self) -> ConversationHost:
        async with self._lock:
            if self._host is None:
                self._host = ConversationHost(self.working_directory)
            return self._host

    async def route(self) -> tuple[str | None, tuple[str, ...], str]:
        """(the session route's id when it can open now, its models, why not otherwise)."""
        catalog = await (await self.host()).catalog()
        # Only the provider's own local session asks before acting; a completion route has no tools.
        route = next((item for item in catalog.routes if item.connection == "local_session"), None)
        if route is None:
            return None, (), "No conversation route exists on this computer."
        if route.availability != "available":
            return None, (), route.reason or f"Sessions are {route.availability} on this computer."
        return route.id, tuple(model.id for model in route.models)[:200], ""

    async def close(self) -> None:
        if self._host is not None:
            await self._host.close()


class MachineAgents(BaseModel):
    """Answers one MachineAgentRequest (already checked: this machine, signed, not expired), from
    the owner's CURRENT settings on this machine: its agent roots (name -> resolved folder), the
    permission web-started agents get, whether agents run here at all, and the two opt-ins
    (continuing his editor conversations, answering a session's approvals), each off by default."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    roots: dict[str, Path]
    permission: AgentTouchScope
    run_agents: bool
    continue_conversations: bool = False
    answer_approvals: bool = False
    #: The launcher session every web-started run is recorded under (one per machine).
    session: str
    runs: WebRuns
    environment: dict[str, str]
    sessions: MachineSessions | None = None
    #: The repositories prepared here from the web (`workspace_prepare`).
    workspaces: MachineWorkspaces | None = None
    #: Opens project secrets sealed for this machine (`SealedSecrets`).
    seal: SecretsSeal | None = None
    logs: LogRing | None = None
    #: Where the editor keeps its conversations (Claude Code: one JSONL file per conversation).
    editor_projects: Path = Field(default_factory=lambda: Path.home() / ".claude" / "projects")
    #: How to run this installation's own CLI (the `galaius` beside this interpreter).
    cli: tuple[str, ...] = Field(default_factory=lambda: MachineAgents.own_cli())
    #: The owner's levels, and whether agents must start inside the fence built from them (set on the
    #: machine only); staged copies for `write_on_review` folders live in `reviews`.
    places: PlaceMap | None = None
    fence_agents: bool = False
    reviews: PlaceReviews | None = None
    #: The machine file the fence reads the levels from at each turn; extra hosts agents reach (this
    #: machine's server).
    levels_file: Path | None = None
    egress: tuple[str, ...] = ()

    @staticmethod
    def own_cli() -> tuple[str, ...]:
        # `which` reads PATHEXT: beside the interpreter on Windows it is `galaius.exe`.
        found = shutil.which("galaius", path=str(Path(sys.executable).parent)) or shutil.which("galaius")
        if found is None:
            raise RuntimeError("the galaius command is not installed beside this runner")
        return (found,)

    async def answer(self, request: MachineAgentRequest) -> MachineAgentAnswer:
        """Session work runs on the runner's loop (its host is async); everything else on a worker
        thread (files, the launcher's CLI)."""
        # With agents off (or no folder) what the web started here can still be read and stopped.
        if not isinstance(request, AgentRunsRequest | AgentTailRequest | AgentMediaRequest | AgentStopRequest | AgentLogsRequest | WorkspacesRequest):
            if not self.run_agents:
                raise PermissionError("agents are off on this computer; its owner turns them on on its page on the web, or there with `galaius machine agents on`")
            if not self.roots:
                raise PermissionError("no agent folders on this computer yet; its owner adds one on its page on the web, or there with `galaius machine agent-roots <folder>`")
        if self.fence_agents and isinstance(request, (AgentStartRequest, AgentContinueRequest)) and (request.kind == "session" if isinstance(request, AgentStartRequest) else True):
            raise PermissionError("sessions and continued conversations are not fenced yet, and the agent fence is on here: start an agent (a role), "
                                  "or its owner switches the fence off there with `galaius machine fence off`")
        match request:
            case AgentFoldersRequest():
                return await asyncio.to_thread(self._folders, request)
            case AgentRunsRequest():
                return await asyncio.to_thread(self._runs, request)
            case AgentTailRequest():
                return await asyncio.to_thread(self._tail, request)
            case AgentMediaRequest():
                return await asyncio.to_thread(self._media, request)
            case AgentOptionsRequest():
                return await self._options(request)
            case AgentStartRequest(kind="session"):
                return await self._start_session(request)
            case AgentStartRequest():
                return await asyncio.to_thread(self._start, request)
            case AgentSendRequest() | AgentStopRequest() | AgentAnswerRequest() if self._kind(request.run_id) == "session":
                return await self._session_action(request)
            case AgentSendRequest():
                return await asyncio.to_thread(self._send, request)
            case AgentStopRequest():
                return await asyncio.to_thread(self._stop, request)
            case AgentAnswerRequest():
                raise PermissionError("only a session asks before acting; this run is not one")
            case AgentSessionsRequest():
                claude = await asyncio.to_thread(self._editor_sessions, request)
                codex = await self._codex_conversations()
                return claude.model_copy(update={"sessions": tuple(sorted((*claude.sessions, *codex), key=lambda item: item.updated_at, reverse=True))[:EDITOR_SESSIONS_MAX]})
            case AgentContinueRequest() if not await asyncio.to_thread(self._is_claude_conversation, request.session_id):
                return await self._continue_codex(request)
            case AgentContinueRequest():
                return await asyncio.to_thread(self._continue, request)
            case AgentLogsRequest():
                return await asyncio.to_thread(self._logs, request)
            case AgentSettingsRequest():
                return await asyncio.to_thread(self._settings, request)
            case AgentProviderSwitchRequest():
                return await asyncio.to_thread(self._switch_provider, request)
            case WorkspacePrepareRequest():
                job = await asyncio.to_thread(self._workspaces().prepare, request)
                return MachineAgentAnswer(request_id=request.id, workspaces=(job,), detail=f"preparing {job.root}/{job.name}")
            case WorkspacesRequest():
                return MachineAgentAnswer(request_id=request.id, workspaces=self._workspaces().jobs.read())

    def _workspaces(self) -> MachineWorkspaces:
        if self.workspaces is None:
            raise PermissionError("this computer does not prepare workspaces")
        return self.workspaces

    def scope(self, asked: AgentTouchScope | None) -> AgentTouchScope:
        """What a run started now may do: `asked`, capped by this PC's permission."""
        return self.permission if asked is None else min(asked, self.permission, key=AGENT_TOUCH_SCOPES.index)

    def environment_in(self, folder: Path) -> dict[str, str]:
        """The child's environment for an agent working in `folder`: a workspace cloned from the
        web loads none of its own agent settings (its hooks would run code its owner never read)."""
        untrusted = self.workspaces is not None and self.workspaces.prepared(folder)
        return {**self.environment, PROJECT_SETTINGS_OFF: "1"} if untrusted else self.environment

    def _kind(self, run_id: UUID) -> AgentRunKind:
        return next((item.kind for item in self.runs.read() if item.run_id == run_id), "agent")

    @staticmethod
    def roles() -> tuple[str, ...]:
        """The roles `galaius agents spawn --agent` accepts here: the launcher's active catalog."""
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
        return {str(item.run_id): item for item in self.runs.read()}, self.runs.runs()

    def _runs(self, request: AgentRunsRequest) -> MachineAgentAnswer:
        started, runs = self._allowed()
        # A run launched inside a fenced run is inside the same fence.
        fenced = {run.run_id for run in runs if run.fence is not None}
        found = []
        for run in runs[:200]:
            placed = started.get(run.run_id)
            root, path = (placed.root, placed.path) if placed else self._place(run.cwd)
            kind = placed.kind if placed else "agent"
            found.append(MachineAgentRun(
                run_id=UUID(run.run_id), name=run.name[:120], role=(run.agent or None) and run.agent[:80], provider=run.provider[:40],
                model=run.model and run.model[:120], status=run.status, root=root[:240], path=path[:1024], task=run.task[:8000],
                last=redact(run.last, self.secrets)[:400], started_at=run.started_at, finished_at=run.finished_at, cost_usd=run.cost_usd,
                parent_run_id=UUID(run.parent_run_id) if run.parent_run_id and run.run_id not in started else None,
                kind=kind, pending=self.pending(run.run_id) if kind == "session" and run.status == "waiting" else (),
                fenced=bool({run.run_id, run.root_run_id, run.parent_run_id} & fenced),
                passed_over=tuple(PassedOverCandidate(provider=item.candidate.provider, model=item.candidate.model[:120], reason=item.reason, until=item.until)
                                  for item in run.skipped[:16]),
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

    @property
    def secrets(self) -> tuple[str, ...]:
        return secret_values(self.environment)

    @staticmethod
    def pending(run_id: str) -> tuple[AgentInteraction, ...]:
        """The approvals `run_id` (a session) still waits for, each with the digest an answer must
        carry back."""
        waiting: dict[str, dict] = {}
        for event in reg.read_events(run_id):
            if event.kind in {"done", "cancelled", "error"}:
                waiting.clear()  # a turn that ended took its unanswered questions with it
            elif event.kind == "interaction" and event.interaction is not None:
                waiting[event.interaction.id] = event.interaction.model_dump(mode="json")
            elif event.kind == "interaction_resolved":
                # The host closes one as "<id>:resolved", the Codex transport as "<id>:closed:<status>".
                closed = event.event_id.removesuffix(":resolved").split(":closed:")[0]
                waiting.pop(closed, None)
        return tuple(AgentInteraction.model_validate({**value, "digest": interaction_digest(value)}) for value in list(waiting.values())[-16:])

    def _tail(self, request: AgentTailRequest) -> MachineAgentAnswer:
        run = self._require_run(request.run_id)
        if run.status in {"running", "waiting"}:
            # The readable copy of a working run's stream is rebuilt on demand; nothing else on a
            # PC with no editor open would rebuild it while the owner watches.
            reg.read_events(run.run_id)
        path = reg.events_path(str(request.run_id))
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0  # nothing written yet (a copy's first turn is still starting)
        cursor = request.cursor
        start = max(0, size - COLD_TAIL) if cursor is None else (0 if cursor > size else cursor)
        end = min(size, start + MACHINE_AGENT_TAIL)
        data = b""
        if size:
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
        if cursor is None and (placed := next((item for item in self.runs.read() if item.run_id == request.run_id and item.source), None)):
            lines = (*self._editor_history(placed.source), *lines)[-TAIL_LINES:]
        return MachineAgentAnswer(request_id=request.id, lines=lines, cursor=start + whole, truncated=skipped)

    def _media(self, request: AgentMediaRequest) -> MachineAgentAnswer:
        """One image a step of this web run names (`media_key` of the path it wrote): only a path
        that run's own stream names, only a plain file (never a link, a FIFO or a device: opened
        non-blocking, checked before any read), at most MACHINE_AGENT_MEDIA bytes, its type proven
        by its first bytes. A path a later step overwrote serves its CURRENT bytes."""
        run = self._require_run(request.run_id)
        written = self._named_image(run.run_id, request.name)
        if written is None or not written.startswith(("/", "~/")):
            raise PermissionError("no step of this run names that image")
        path = Path.home() / written[2:] if written.startswith("~/") else Path(written)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            raise PermissionError("that image is no longer on this computer") from None
        with os.fdopen(descriptor, "rb") as handle:
            facts = os.fstat(handle.fileno())
            if not stat.S_ISREG(facts.st_mode):
                raise PermissionError("that image is not a plain file")
            if facts.st_size > MACHINE_AGENT_MEDIA:
                raise PermissionError(f"that image is larger than {MACHINE_AGENT_MEDIA // (1024 * 1024)} MiB")
            data = handle.read(MACHINE_AGENT_MEDIA + 1)
        content_type = image_type(data)
        if content_type is None or len(data) > MACHINE_AGENT_MEDIA:
            raise PermissionError("that file is not an image")
        return MachineAgentAnswer(request_id=request.id, media=AgentMedia(content_type=content_type, data=base64.b64encode(data).decode("ascii")))

    @staticmethod
    def _named_image(run_id: str, name: str) -> str | None:
        """The path, as a tool step of `run_id` wrote it, whose `media_key` is `name`; None if none does."""
        try:
            handle = reg.events_path(run_id).open("rb")
        except OSError:
            return None
        with handle:
            for line in handle:
                if b'"tool"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("kind") == "tool":
                    found = next((path for path in media_paths(str(event.get("tool_input") or "")) if media_key(path) == name), None)
                    if found is not None:
                        return found
        return None

    def _editor_history(self, session_id: UUID, keep: int = 40) -> tuple[str, ...]:
        """The editor conversation a copy continues, as stream lines the transcript reads (what the
        owner typed, what it answered): the copy opens where the editor left off."""
        try:
            path = self._editor_file(session_id)
        except PermissionError:
            return ()
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - 1024 * 1024))
            tail = handle.read()
        said: list[str] = []
        for line in tail.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            kind, message = value.get("type"), value.get("message") or {}
            text = redact(self._said_text(message.get("content")).strip(), self.secrets)
            if not text or value.get("isMeta") or (kind == "user" and text.startswith("<")):
                continue
            at = value.get("timestamp")
            try:
                moment = time.mktime(time.strptime(str(at)[:19], "%Y-%m-%dT%H:%M:%S")) if at else 0.0
            except ValueError:
                moment = 0.0
            if kind == "user":
                said.append(json.dumps({"kind": "prompt", "text": text[:8000], "at": moment}))
            elif kind == "assistant":
                said.append(json.dumps({"kind": "text", "text": text[:20000], "at": moment}))
        return tuple(said[-keep:])

    def _run_cli(self, *arguments: str, timeout: float, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """The CLI's own exit and output. Output goes to files, never pipes: the agent it starts
        outlives it and inherits its descriptors, and reading a pipe to its end would wait for the
        AGENT to finish (a start answered only once the run was over)."""
        with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
            done = subprocess.run([*self.cli, *arguments], env=environment or self.environment, stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=timeout, check=False)
            out.seek(0)
            err.seek(0)
            return subprocess.CompletedProcess(done.args, done.returncode, out.read(), err.read())

    @staticmethod
    def _first_line(text: str) -> str:
        return next((line.strip() for line in text.splitlines() if line.strip()), "")[:400]

    @staticmethod
    def _said(text: str) -> str:
        """The CLI's refusal on one line: its first line, then each DISTINCT reason once (a
        ranked-candidates refusal repeats one reason per model: "  codex/x/model: <reason>")."""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        reasons = dict.fromkeys(line.split(": ", 1)[-1] for line in lines[1:])
        return " ".join([*lines[:1], *reasons]).removeprefix("ERROR: ")[:400]

    def _start(self, request: AgentStartRequest) -> MachineAgentAnswer:
        _, runs = self._allowed()
        working = sum(1 for run in runs if run.status in {"running", "waiting"})
        if working >= LIVE_WEB_RUNS:
            raise PermissionError(f"{working} agents started from the web are already working on this computer; stop one first")
        folder = self.folder(request.root, request.path)
        # The launcher's supervisor window (not the terminal's 20 s): the run shows at once; a
        # quota refusal after it is that run's failure, in the list.
        if request.model is not None:
            # The launcher runs a role on what its own rule picks (a per-run model is never
            # honoured): refused, never silently replaced.
            raise PermissionError("an agent runs on the model its rule picks; change the rule on its Agents page, or start a session to choose the model")
        written = self._project_env(request, folder)
        options = ["--agent", str(request.role), "--cwd", str(folder), "--permission-mode", self.scope(request.permission), "--session-id", self.session, "--quota-window", "4",
                   *(["--provider", request.provider] if request.provider is not None else []), *([f"--model={request.model}"] if request.model else [])]
        fence, review = self._fence(request.root, folder) if self.fence_agents else (None, None)
        with tempfile.TemporaryDirectory(prefix="galaius-fence-") as held:
            if fence is not None:
                (Path(held) / "fence.json").write_text(fence.model_dump_json())
                options += ["--fence", str(Path(held) / "fence.json")]
            # "--" ends the options: a brief starting with "-" (a markdown bullet, "--help") is the brief.
            done = self._run_cli("agents", "spawn", *options, "--", request.text, timeout=120, environment=self.environment_in(folder))
        output = done.stdout.strip().splitlines()
        try:
            run_id = UUID(output[-1].strip()) if done.returncode == 0 and output else None
        except ValueError:
            run_id = None
        if run_id is None:
            if review is not None:
                self.reviews.discard(review)
            raise RuntimeError(self._said(done.stderr) or f"the agent did not start (exit {done.returncode})")
        if review is not None:
            self.reviews.attach(review, run_id)
        self.runs.add(WebRun(run_id=run_id, root=request.root, path=request.path, fenced=fence is not None))
        return MachineAgentAnswer(request_id=request.id, run_id=run_id, fenced=fence is not None, detail=f"{written} project secrets written to .env" if written else "")

    def _project_env(self, request: AgentStartRequest, folder: Path) -> int:
        """The project's secrets, when the start brings them: opened with this machine's key, written
        as the .env of the project's own checkout holding `folder` (`ProjectEnv`); how many. Their
        values join the redaction list of this runner's log lines."""
        if request.secrets is None:
            return 0
        if self.seal is None or request.project != request.secrets.project:
            raise PermissionError("this computer cannot take these project secrets")
        try:
            values = self.seal.open(request.secrets, request=request.id)
        except (InvalidTag, ValueError):
            raise PermissionError("the project's secrets could not be opened on this computer") from None
        ProjectEnv.locate(folder, self.roots[request.root], request.secrets.origin).write(values, project=request.secrets.project)
        if self.logs is not None:
            self.logs.secrets = (*self.logs.secrets, *(value for value in values.values() if len(value) >= 8))
        return len(values)

    def _fence(self, root: str, folder: Path) -> tuple[FenceSpec, UUID | None]:
        """What the fence of an agent starting in `folder` is built from, and the review holding its
        staging copy when that folder is write-after-review. The folder needs a level its owner set
        (read or later): an agent folder never opens on its own. Built once here so a start that
        cannot be fenced is refused before anything runs; every turn builds it again."""
        ready, reason = available()
        if not ready:
            raise PermissionError(f"the agent fence is on here but cannot be built: {reason}; its owner switches it off there with `galaius machine fence off` to start agents unfenced")
        if self.places is None or self.reviews is None:
            raise PermissionError("the agent fence is on but this runner holds no levels")
        places, base = self.places, self.places.base
        parts = folder.relative_to(base).parts
        if LEVEL_RANK[places.reach(parts)] < LEVEL_RANK["read"]:
            raise PermissionError(f"no level opens {'/'.join(parts)} to agents; with the fence on, its owner sets one there first (`galaius machine places {root} write`)")
        staging, review = {}, None
        if places.reach(parts) == "write_on_review":
            place, _ = places.place_of(parts)
            review, staging[place] = self.reviews.stage_copy(place, base.joinpath(*split(place)), origin="agent", run_id=None)
        try:
            programs = tuple(Path(found) for found in (*(shutil.which(name) for name in AGENT_PROVIDERS), *self.cli) if found)
            spec = FenceSpec(working_directory=places.working_directory, levels_file=self.levels_file, levels=places.levels, internal=places.internal, start=folder,
                             staging=staging, providers=AGENT_PROVIDERS, programs=programs, state=self.reviews.root.parent / "fence-state" / str(uuid4()), reviews=self.reviews.root,
                             egress=(*(host for provider in AGENT_PROVIDERS for host in EGRESS[provider]), *self.egress))
            spec.build()
            return spec, review
        except BaseException:
            if review is not None:
                self.reviews.discard(review)
            raise

    def _send(self, request: AgentSendRequest) -> MachineAgentAnswer:
        run = self._require_run(request.run_id)
        if self._kind(request.run_id) == "continued":
            return self._continue_turn(run, request.text, request_id=request.id)
        # Answered once the message is durably queued: its run's dispatcher resumes the agent, and
        # a resume refused after this point is that run's failure, in its events.
        delivery = deliver_message(str(request.run_id), request.text, sender="operator", environment=self.environment_in(Path(run.cwd)))
        if delivery.state == "error":
            raise RuntimeError(delivery.text.removeprefix("ERROR: ") or "not delivered")
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail=delivery.text)

    def _stop(self, request: AgentStopRequest) -> MachineAgentAnswer:
        run = self._require_run(request.run_id)
        if run.status not in {"running", "waiting"}:
            return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail="already ended")
        if not reg.stop(str(request.run_id)):
            raise RuntimeError("the run could not be stopped")
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail="stopped")

    # ---- what can start here ---------------------------------------------------------------

    def models(self) -> tuple[MachineAgentModel, ...]:
        """Every model an agent can run on here, best first per the launcher's own ranking, over
        the CLIs that are installed and switched on (cached a minute: ranking reads the catalog)."""
        global _MODELS
        if _MODELS is not None and time.monotonic() - _MODELS[0] < 60:
            return _MODELS[1]
        try:
            policy = load_policy()
            providers = [PROVIDERS[name] for name in AGENT_PROVIDERS if name in PROVIDERS and policy.provider_active(name) and PROVIDERS[name].available()]
            ranked = rank_candidates(MODEL_RANKING, dict(self.environment), providers=providers) if providers else ()
        except (OSError, ValueError, RuntimeError) as error:
            logger.warning("agent models unavailable: %s", error)
            ranked = ()
        # The strongest few per CLI: the tail of the ranking is models nobody would pick by hand.
        # A dated release beside its own alias ("claude-opus-4-7-20260416" next to "claude-opus-4-7")
        # is the same model twice: the alias stays.
        ids = {item.model for item in ranked}
        unique = dict.fromkeys(MachineAgentModel(provider=item.provider, model=item.model) for item in ranked
                               if (base := _DATED.sub("", item.model)) == item.model or base not in ids)
        per_provider: dict[str, list[MachineAgentModel]] = {}
        for item in unique:
            per_provider.setdefault(item.provider, []).append(item)
        found = tuple(item for item in unique if item in per_provider[item.provider][:MODELS_PER_PROVIDER])
        _MODELS = (time.monotonic(), found)
        return found

    def role_models(self, role: str) -> tuple[MachineAgentModel, ...]:
        """What `role`'s own rule picks on each CLI here, best first: the model a start will run on
        (the first), and no row for a CLI that cannot run the role at all."""
        try:
            policy = load_policy()
            criterion = policy.criterion_for(role)
            providers = [PROVIDERS[name] for name in AGENT_PROVIDERS if name in PROVIDERS and policy.provider_active(name) and PROVIDERS[name].available()]
            if not criterion or not providers:
                return ()
            best = []
            for provider in providers:
                try:
                    ranked = rank_candidates(criterion, dict(self.environment), providers=[provider], weights=policy.weights_for(role), role=role)
                    provider.validate_tool_policy(policy.tools_for(role), (), coarse_accepted=policy.accepts_coarse_tool_policy(role, provider.name))
                except (ValueError, RuntimeError):
                    continue  # this CLI cannot run the role (no model clears its rule, or its tool rules)
                best.extend(ranked[:1])
            # In the launcher's own order: the first is what "best available" runs.
            return tuple(MachineAgentModel(provider=item.provider, model=item.model) for item in sorted(best, key=lambda item: item.rank))
        except (OSError, ValueError, RuntimeError) as error:
            logger.warning("role models unavailable for %s: %s", role, error)
            return ()

    async def _options(self, request: AgentOptionsRequest) -> MachineAgentAnswer:
        if request.role is not None:
            return MachineAgentAnswer(request_id=request.id, models=await asyncio.to_thread(self.role_models, request.role), permission=self.permission)
        roles, models = await asyncio.to_thread(lambda: (self.roles(), self.models()))
        if not self.answer_approvals:
            route, session_models, reason = None, (), "A session asks you before it runs a command or changes a file; answering from the web is off on this computer (its owner turns it on there with `galaius machine agents --approvals on`)."
        elif self.sessions is None:
            route, session_models, reason = None, (), "Sessions are not hosted by this connection."
        else:
            route, session_models, reason = await self.sessions.route()
        return MachineAgentAnswer(request_id=request.id, roles=roles, models=models, session_models=session_models if route else (),
                                  session_reason=reason, permission=self.permission)

    # ---- providers and tool models (VS Code: "Providers That Run Agents", "Select Model") ----

    @staticmethod
    def providers() -> tuple[AgentProviderState, ...]:
        policy = load_policy()
        return tuple(AgentProviderState(provider=name, active=policy.provider_active(name), available=PROVIDERS[name].available())
                     for name in AGENT_PROVIDERS if name in PROVIDERS)

    def _settings(self, request: AgentSettingsRequest) -> MachineAgentAnswer:
        """The CLIs agents may run through here, and what each tool's rule resolves to here, asked
        of this installation's own CLI (`galaius config models`): a fresh read of the account's
        rules against this computer's keys, never this process's snapshot."""
        done = self._run_cli("config", "models", "--json-out", timeout=40)
        try:
            payload = json.loads(done.stdout.strip().splitlines()[-1])
            if payload.get("ok") is not True:
                raise ValueError(payload.get("message") or "refused")
            tool_models, detail = tuple(ToolRoleModels.model_validate(role) for role in payload["roles"]), ""
        except (ValueError, IndexError, KeyError, TypeError) as error:
            tool_models, detail = (), f"models not read here: {self._first_line(str(error)) or self._said(done.stderr)}"[:400]
        return MachineAgentAnswer(request_id=request.id, providers=self.providers(), tool_models=tool_models, detail=detail)

    def _switch_provider(self, request: AgentProviderSwitchRequest) -> MachineAgentAnswer:
        """Through the CLI the editor's toggle uses, so both write the one policy file the same way."""
        global _MODELS
        done = self._run_cli("agents", "providers", "--name", request.provider, "--state", "on" if request.active else "off", timeout=30)
        if done.returncode != 0:
            raise RuntimeError(self._said(done.stderr) or f"not switched (exit {done.returncode})")
        _MODELS = None  # the model list follows the switched-on CLIs
        return MachineAgentAnswer(request_id=request.id, providers=self.providers(), detail="on" if request.active else "off")

    # ---- sessions: conversations that ask before acting -------------------------------------

    async def _start_session(self, request: AgentStartRequest) -> MachineAgentAnswer:
        if not self.answer_approvals or self.sessions is None:
            raise PermissionError("answering a session's approvals from the web is off on this computer; its owner turns it on there with `galaius machine agents --approvals on`")
        folder = await asyncio.to_thread(self.folder, request.root, request.path)
        _, runs = await asyncio.to_thread(self._allowed)
        if sum(1 for run in runs if run.status in {"running", "waiting"}) >= LIVE_WEB_RUNS:
            raise PermissionError(f"{LIVE_WEB_RUNS} agents started from the web are already working on this computer; stop one first")
        route, models, reason = await self.sessions.route()
        if route is None:
            raise PermissionError(reason)
        if request.model is not None and request.model not in models:
            raise PermissionError(f"{request.model} is not a model sessions can use here")
        try:
            run = await (await self.sessions.host()).start(request.text, folder, route_id=route, model=request.model)
        except ConversationRefused as error:
            raise RuntimeError(str(error)) from error
        await asyncio.to_thread(self.runs.add, WebRun(run_id=UUID(run.run_id), root=request.root, path=request.path, kind="session"))
        return MachineAgentAnswer(request_id=request.id, run_id=UUID(run.run_id))

    async def _session_action(self, request: AgentSendRequest | AgentStopRequest | AgentAnswerRequest) -> MachineAgentAnswer:
        assert self.sessions is not None
        run = await asyncio.to_thread(self._require_run, request.run_id)
        host = await self.sessions.host()
        try:
            match request:
                case AgentSendRequest():
                    detail = await self.sessions.send(run.run_id, request.text)
                case AgentStopRequest():
                    await host.cancel(run.run_id)
                    detail = "stopped"
                case AgentAnswerRequest():
                    if not self.answer_approvals:
                        raise PermissionError("answering a session's approvals from the web is off on this computer")
                    asked = next((item for item in await asyncio.to_thread(self.pending, run.run_id) if item.id == request.interaction_id), None)
                    if asked is None or asked.digest != request.digest:
                        raise PermissionError("this approval is no longer the one waiting on this computer; read it again")
                    # An approval is answered accept / decline and nothing else (never "for the whole
                    # session", never a policy change); a file change is accepted where its diff shows.
                    if asked.kind != "user_input" and (set(request.values) != {"decision"} or request.values["decision"] not in {"accept", "decline"}):
                        raise PermissionError("an approval is answered accept or decline, nothing else")
                    if asked.kind == "file_change_approval" and request.values.get("decision") != "decline":
                        raise PermissionError("a file change is accepted in the editor on this computer, where its diff shows; from here it can only be declined")
                    await host.answer(run.run_id, request.interaction_id, request.values)
                    detail = str(request.values.get("decision") or "answered")
        except ConversationRefused as error:
            raise RuntimeError(str(error)) from error
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail=detail)

    # ---- the owner's own editor conversations ------------------------------------------------

    def _require_continue(self) -> None:
        if not self.continue_conversations:
            raise PermissionError("continuing your editor conversations from the web is off on this computer; its owner turns it on there with `galaius machine agents --continue on`")

    def _is_claude_conversation(self, session_id: UUID) -> bool:
        return any(path.is_file() for path in self.editor_projects.glob(f"*/{session_id}.jsonl"))

    async def _codex_conversations(self) -> tuple[MachineAgentSession, ...]:
        """The owner's own Codex conversations inside an agent root (never ones galaius made).
        Continuing one opens a session (a copy that asks before acting), so both opt-ins apply;
        without them, or with Codex unavailable here, there are none to offer."""
        if not (self.continue_conversations and self.answer_approvals and self.sessions is not None):
            return ()
        route, _, _ = await self.sessions.route()
        if route is None:
            return ()
        try:
            rows = await (await self.sessions.host()).threads(route)
        except (ConversationRefused, ConnectionError, ValueError) as error:
            logger.warning("codex conversations unavailable: %s", error)
            return ()
        launched = await asyncio.to_thread(reg.session_ids)
        found = []
        for row in rows:
            root, where = self._place(str(row.get("cwd") or ""))
            try:
                thread_id = UUID(str(row["id"]))
            except ValueError:
                continue
            if not root or row.get("originator") == "galaius" or str(thread_id) in launched:
                continue
            found.append(MachineAgentSession(session_id=thread_id, provider="codex", title=redact(str(row.get("preview") or ""), self.secrets)[:400], root=root, path=where,
                                             updated_at=float(row.get("updatedAt") or 0), live=time.time() - float(row.get("updatedAt") or 0) < EDITOR_LIVE_SECONDS))
        return tuple(found)

    async def _continue_codex(self, request: AgentContinueRequest) -> MachineAgentAnswer:
        """A COPY of the owner's Codex conversation continues here as a session (`thread/fork`): it
        keeps the conversation's history, asks before acting like any session, and the original is
        never written. Its folder is read HERE from Codex's own record, inside an agent root."""
        self._require_continue()
        found = next((item for item in await self._codex_conversations() if item.session_id == request.session_id), None)
        if found is None:
            raise PermissionError("this conversation is not one this computer offers to continue (a Codex copy needs the approvals setting on too)")
        assert self.sessions is not None
        folder = await asyncio.to_thread(self.folder, found.root, found.path)
        _, runs = await asyncio.to_thread(self._allowed)
        if sum(1 for run in runs if run.status in {"running", "waiting"}) >= LIVE_WEB_RUNS:
            raise PermissionError(f"{LIVE_WEB_RUNS} agents started from the web are already working on this computer; stop one first")
        route, _, reason = await self.sessions.route()
        if route is None:
            raise PermissionError(reason)
        try:
            run = await (await self.sessions.host()).fork(str(request.session_id), request.text, folder, route_id=route, model=None)
        except ConversationRefused as error:
            raise RuntimeError(str(error)) from error
        await asyncio.to_thread(self.runs.add, WebRun(run_id=UUID(run.run_id), root=found.root, path=found.path, kind="session"))
        return MachineAgentAnswer(request_id=request.id, run_id=UUID(run.run_id))

    def _editor_file(self, session_id: UUID) -> Path:
        found = [path for path in self.editor_projects.glob(f"*/{session_id}.jsonl") if path.is_file() and not path.is_symlink()]
        if len(found) != 1:
            raise PermissionError("this conversation is not on this computer")
        return found[0]

    @staticmethod
    def _said_text(value: object) -> str:
        """The plain text of a Claude transcript message's `content` (a string, or text blocks)."""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(str(block.get("text") or "") for block in value if isinstance(block, dict) and block.get("type") == "text")
        return ""

    def _read_editor(self, path: Path) -> tuple[str, str, str, str]:
        """(its folder, its title — Claude's own, else its first prompt — its first prompt, its last
        reply) from the conversation file's two ends."""
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(256 * 1024)
            handle.seek(max(0, size - 256 * 1024))
            tail = handle.read()
        cwd, first, last, titled = "", "", "", ""
        for line in head.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            titled = titled or (str(value.get("aiTitle") or "") if value.get("type") == "ai-title" else "")
            cwd = cwd or str(value.get("cwd") or "")
            message = value.get("message") or {}
            text = self._said_text(message.get("content")) if value.get("type") == "user" and not value.get("isMeta") else ""
            if not first and text.strip() and not text.lstrip().startswith("<"):
                first = text.strip()
            if cwd and first and titled:
                break
        for line in reversed(tail.splitlines()):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if value.get("type") == "assistant" and (text := self._said_text((value.get("message") or {}).get("content")).strip()):
                last = text
                break
        return cwd, titled or first, first, last

    def _editor_sessions(self, request: AgentSessionsRequest) -> MachineAgentAnswer:
        """The owner's editor conversations written in the last two weeks whose folder lies inside
        an agent root, newest first; none that galaius itself started."""
        self._require_continue()
        since = time.time() - EDITOR_SESSIONS_DAYS * 86400
        files = []
        for path in self.editor_projects.glob("*/*.jsonl"):
            try:
                facts = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(facts.st_mode) and facts.st_mtime >= since:
                files.append((facts.st_mtime, path))
        found = []
        launched = reg.session_ids()
        for updated, path in sorted(files, reverse=True):
            try:
                session_id = UUID(path.stem)
            except ValueError:
                continue
            if path.stem in launched:
                continue  # a session galaius launched (an agent run, a continued copy), not the owner's
            cwd, title, first, last = self._read_editor(path)
            root, where = self._place(cwd)
            if not root or first.startswith(LAUNCH_STAMP):
                continue
            found.append(MachineAgentSession(session_id=session_id, provider="claude", title=redact(title, self.secrets)[:400], last=redact(last, self.secrets)[:400],
                                             root=root, path=where, updated_at=updated, live=time.time() - updated < EDITOR_LIVE_SECONDS))
            if len(found) >= EDITOR_SESSIONS_MAX:
                break
        return MachineAgentAnswer(request_id=request.id, sessions=tuple(found))

    def _continue(self, request: AgentContinueRequest) -> MachineAgentAnswer:
        """A COPY of the editor conversation `session_id` continues here with `text` (Claude's own
        fork: the editor's conversation is never written), in the conversation's own folder — read
        HERE from its file, and only when it lies inside an agent root."""
        self._require_continue()
        _, runs = self._allowed()
        if sum(1 for run in runs if run.status in {"running", "waiting"}) >= LIVE_WEB_RUNS:
            raise PermissionError(f"{LIVE_WEB_RUNS} agents started from the web are already working on this computer; stop one first")
        cwd, first, _, _ = self._read_editor(self._editor_file(request.session_id))
        root, where = self._place(cwd)
        if not root:
            raise PermissionError("this conversation's folder is not one this computer opens to agents")
        folder = self.folder(root, where)
        run = reg.AgentRun(run_id=str(uuid4()), provider="claude", name="Editor conversation", task=redact(first, self.secrets)[:500] or request.text[:500],
                           cwd=str(folder), project=reg.project_for(str(folder)), permission_mode=self.permission, session_id=self.session, started_at=time.time(), status="running")
        reg.save_run(run)
        self.runs.add(WebRun(run_id=UUID(run.run_id), root=root, path=where, kind="continued", source=request.session_id))
        self._continue_turn(run, request.text, request_id=request.id, fork_from=str(request.session_id))
        return MachineAgentAnswer(request_id=request.id, run_id=UUID(run.run_id))

    def _continue_turn(self, run: reg.AgentRun, text: str, *, request_id: UUID, fork_from: str | None = None) -> MachineAgentAnswer:
        """One turn of a continued conversation: the opt-in is checked on EVERY turn (turning it off
        stops the copies too), and one turn at a time (checked and started under the run's lock)."""
        self._require_continue()
        with _TURN_LOCKS.setdefault(run.run_id, threading.Lock()):
            current = reg.get_run(run.run_id) or run
            if fork_from is None and reg.trees(frozenset({run.run_id}))[0].status in {"running", "waiting"}:
                raise PermissionError("it is still answering; send this once it has finished")
            reg.record_message(from_run="operator", to_run=run.run_id, text=text)
            try:
                launch_editor_turn(PROVIDERS["claude"], current, text, environment=self.environment, fork_from=fork_from)
            except BaseException:
                reg.finish(run.run_id, exit_code=1)  # never left "running" by a turn that did not start
                raise
        return MachineAgentAnswer(request_id=request_id, run_id=UUID(run.run_id), detail="sent")

    # ---- logs ------------------------------------------------------------------------------------

    def _logs(self, request: AgentLogsRequest) -> MachineAgentAnswer:
        """This connection's recent log lines, or one web-started run's error output, redacted (the
        ring's lines once more on the way out, with this request's secrets)."""
        if request.run_id is not None:
            self._require_run(request.run_id)
            text = reg.read_stderr(str(request.run_id), limit=16_000)
            return MachineAgentAnswer(request_id=request.id, lines=tuple(redact(line, self.secrets)[:600] for line in text.splitlines()[-400:] if line.strip()))
        return MachineAgentAnswer(request_id=request.id, lines=tuple(redact(line, self.secrets) for line in self.logs.lines) if self.logs is not None else ())


#: One turn at a time per continued conversation (`_continue_turn`).
_TURN_LOCKS: dict[str, threading.Lock] = {}
#: (when, models) — `MachineAgents.models`' one-minute cache.
_MODELS: tuple[float, tuple[MachineAgentModel, ...]] | None = None
