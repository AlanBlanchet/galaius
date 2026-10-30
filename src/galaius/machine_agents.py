"""The machine owner drives coding agents on this computer from the web, through its machine
connection, as the editor panel does: start an agent (a role) or a session (a conversation that
asks before commands) in a folder he allowed, on a model he picks, read what it does, answer what
it asks, message it, stop it; continue one of his own editor conversations; read the logs.

Everything a request may touch is checked HERE, on the machine, against its CURRENT settings: the
folders (`MachineConfig.agent_roots`), the permission agents start with (`agent_permission`; a
start may ask for less, never more; bypass only when set on the PC itself), and the runs a request
may read or act on (only runs started this way and what they launched: `WebRuns`). The owner sets
those settings on the PC or on its web page (`MachineAgentSettings`, while `remote_settings` is on). Starting and messaging run in
this process (`AgentSpawns`, `deliver_message`), each given the scrubbed environment explicitly, so
no request ever changes this process's environment and no request waits for a cold CLI."""

import asyncio
import base64
import functools
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
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from galaius_core import (
    listed_role,
    MODEL_ID_MAX,
    PROJECTS_ROOT, AGENT_TOUCH_SCOPES, AgentCompactRequest, ContextGauge, MACHINE_AGENT_MEDIA, MACHINE_AGENT_READ, MACHINE_AGENT_TAIL, AgentAnswerRequest, AgentMedia, AgentMediaRequest, AgentFile, media_kind, image_type, AgentContinueRequest, AgentFoldersRequest, AgentInteraction, AgentLogsRequest, AgentOptionsRequest,
    AgentAllowanceRequest, AgentMoveRequest, AgentProgramInstallRequest, AgentProgramsRequest, AgentToolInstallRequest, AgentToolsRequest, AgentProviderState, AgentSignInCodeRequest, AgentSignOutRequest, SubscriptionAllowance, Lent, AgentProviderSwitchRequest, AgentRunKind, AgentRunsRequest, AgentSettingsRequest, ToolRoleModels, AgentSendRequest, AgentSessionsRequest, AgentStartRequest, AgentStopRequest, AgentTailRequest, AgentTouchScope,
    MachineAgentAnswer, MachineAgentModel, MachineAgentRequest, MachineAgentRun, PassedOverCandidate, ReadsAt, WebLimited, WebLoad, media_key, media_paths, MachineAgentSession, MachineFileEntry, StopActor, WorkspaceCreateRequest, WorkspacePack, WorkspacePackRequest, WorkspacePrepareRequest, WorkspaceRefresh, WorkspacesRequest, ProjectFilesBase, ProjectFilePart,
)
from galaius.agents import agent_queue, quota
from galaius.agents import registry as reg
from galaius.agents.assembly import _TransportRegistry
from galaius.agents.host import ConversationHost, ConversationRefused
from galaius.agents.session_stops import SessionStops
from galaius.agents.followup import FollowUps
from galaius.agents.messaging import deliver_message
from galaius.agents.providers import PROJECT_SETTINGS_OFF, PROVIDERS, UNTRUSTED_FOLDERS, provider_for
from galaius.agents.subscriptions import RunSubscription, Subscription, SubscriptionRefused
from galaius.agents.run import LAUNCH_STAMP, launch_editor_turn, load_policy, rank_candidates, run_agent
from galaius.agents.warm import WarmStart
from galaius.config import UserConfig
from galaius.fence import EGRESS, FenceSpec, available
from galaius.file_lock import exclusive
from galaius.machine_programs import AgentPrograms
from galaius.machine_tools import MachineTools
from galaius.machine_places import LOG as PLACES_LOG
from galaius.machine_workspaces import MachineWorkspaces, unusable
from galaius.pinned_directory import PinnedDirectory
from galaius.project_files import ProjectFolder
from galaius.redaction import mask, redact, secret_values
from galaius.workspace_copy import WorkspaceRefused
from galaius.workspace_git import AgentRefused
from galaius.place_reviews import PlaceReviews
from galaius.places import LEVEL_RANK, PlaceMap, split
from galaius.project_secrets import ProjectEnv
from cryptography.exceptions import InvalidTag
from galaius_core.sealing import SecretsSeal

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: A run's statuses while it works (`waiting` for its owner is not working).
WORKING = frozenset({"starting", "running"})
#: Seconds a start request waits for its agent to be running; the start itself goes on past it.
START_SECONDS = 120.0
#: What a cold `tail` (no cursor) reads back from the end of a run's stream.
COLD_TAIL = 48 * 1024
#: Lines one `tail` answer carries at most (`MachineAgentAnswer.lines`); the cursor stops after the last.
TAIL_LINES = 4000
#: A message id as the web gives it (`AgentSendRequest.id`, a UUID in its usual spelling).
_WEB_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: The CLIs agents run on here, and the ranking the model list follows: every benchmarked model,
#: most capable first (the order a role's own criterion walks).
AGENT_PROVIDERS = ("claude", "codex")
#: Why a member's run on a lent PC found nothing to run on (`SubscriptionUse.lent`); the server words it for him.
LENT_REFUSAL = "subscriptions are never lent: a member's run on a lent PC uses none of its CLI logins; run it on your own PC or with an API key"
MODEL_RANKING = "aa.intelligence and price.in >= 0"
MODELS_PER_PROVIDER = 12
#: A release date closing a model id: -20260416 or -2026-04-16.
_DATED = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
#: Who each cut's automatic resume is from (`registry.SYSTEM_SENDERS`) and what it came back after, as its failure says it.
#: Every cut has a row: one missing is a `KeyError` in `resume_paused`, which would leave every OTHER cut run of that
#: pass unresumed (a parametrized sweep over `CutCause` pins it). A cause the web has no wording of its own for sends
#: from `CUT_RESUME_SENDER`, the sender it already reads as « reprise automatique après la coupure ».
_RESUMED_AFTER: dict[reg.CutCause, tuple[str, str]] = {
    "usage_limit": (reg.LIMIT_RESUME_SENDER, "the usage limit"),
    "login_lost": (reg.LOGIN_RESUME_SENDER, "signing in again"),
    "network_lost": (reg.CUT_RESUME_SENDER, "its provider's API could not be reached"),
    "process_gone": (reg.CUT_RESUME_SENDER, "its process went away"),
    "killed": (reg.CUT_RESUME_SENDER, "its process was killed"),
}
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
    #: The start request that launched it (`AgentStartRequest.idempotent_feature`): asked again, it answers this run.
    request_id: UUID | None = None




class WebRuns(BaseModel):
    """The runs this computer started for the web, kept in one 0600 file beside the machine config:
    the only runs (with what they launched) a web request may read, message or stop."""

    model_config = ConfigDict(frozen=True)
    path: Path
    KEEP: ClassVar[int] = 500

    @classmethod
    def in_folder(cls, folder: Path) -> "WebRuns":
        """The file the runner whose machine file is in `folder` keeps them in."""
        return cls(path=folder / "machine-agent-runs.json")

    def read(self) -> tuple[WebRun, ...]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        return tuple(WebRun.model_validate(item) for item in raw)

    def started_by(self, request_id: UUID) -> WebRun | None:
        """The run start request `request_id` launched here, if any."""
        return next((run for run in self.read() if run.request_id == request_id), None)

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

    def live(self) -> int:
        """The conversations started from the web that work now (one of their runs starting or running): one counts
        ONCE, whatever agents it launched (owner 2026-10-10: two conversations and their five sub-agents refused his
        third); one waiting for its owner, with nothing of it working, does not count; nor a run of a tool of the web's own
        UI (`UI_ROLES`: a question to the blob), which is none of his conversations."""
        return sum(1 for root, runs in reg.forest(frozenset(str(item.run_id) for item in self.read())).items()
                   if any(run.status in WORKING for run in runs) and listed_role(next((run.agent for run in runs if run.run_id == root), None)))

    def load(self, request: WebLimited) -> WebLoad:
        """The conversations started from the web working here against how many may at once, as `request` carries it
        from the web (`WebLimited.web_conversations`, each with whatever agents it launched; the web reads it)."""
        return WebLoad(working=self.live(), limit=request.web_conversations)

    def refuse_full(self, request: WebLimited) -> None:
        """Refuses one more start while as many conversations started from the web as `request` allows work here; never a
        run of a tool of the web's own UI (a question to the blob: no conversation of his, `UI_ROLES`). Nothing here caps how
        many of those start at once but the web's start rate limit: the blob role has no tools and runs read_only."""
        if listed_role(request.role if isinstance(request, AgentStartRequest) else None) and (load := self.load(request)).full:
            raise AgentRefused("web_full", f"{load.working} conversations started from the web are already working on this computer (at most {load.limit}); stop one first")

    def working(self) -> bool:
        """One of these runs, or a run it launched, is starting or running now (waiting for its owner is not working)."""
        return any(run.status in WORKING for run in self.runs())

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
                stopped += bool(reg.stop(run.run_id, by=StopActor(kind="pc_switch")))
            except (OSError, RuntimeError, ValueError) as error:
                failed += 1
                logger.warning("could not stop run %s: %s", run.run_id[:8], error)
        return stopped, failed


class AgentSpawns:
    """The event loop web-started agents are launched on: its own thread, for this process's
    lifetime. A launch reads policy and ranks models before its child exists (blocking work that
    must not stall the runner's connection loop), and what watches the child after (its exit, its
    mirrored stream) must outlive the request that started it. The child itself leads its own
    process group and outlives this process, as a CLI-launched one does."""

    _loop: ClassVar[asyncio.AbstractEventLoop | None] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def loop(cls) -> asyncio.AbstractEventLoop:
        with cls._lock:
            if cls._loop is None:
                cls._loop = asyncio.new_event_loop()
                threading.Thread(target=cls._loop.run_forever, name="galaius-agent-spawns", daemon=True).start()
            return cls._loop

    @classmethod
    async def run(cls, launch: Callable[[], Awaitable[T]]) -> T:
        """`launch()` run on the spawn loop; its result (or its exception) here."""
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(launch(), cls.loop()))


class MachineSessions:
    """The conversations ("sessions") this machine hosts for the web, for the runner's lifetime:
    the same host the editor's chat uses (`ConversationHost`), opened on first use. A session asks
    before a command or a file change; those questions are what the web answers."""

    def __init__(self, working_directory: Path) -> None:
        self.working_directory = working_directory
        self._host: ConversationHost | None = None
        self._lock = asyncio.Lock()
        #: Messages to a session that was mid-turn (id, text, image files), delivered in order once each turn ends.
        self._queued: dict[str, list[tuple[UUID, str, tuple[Path, ...]]]] = {}
        self._deliveries: dict[str, asyncio.Task] = {}
        #: The messages each session has read (a turn opened on them), the latest last.
        self._read: dict[str, deque[UUID]] = {}
        #: Each message being handed over now, by its id.
        self._sending: dict[UUID, asyncio.Future[str]] = {}

    async def send(self, run_id: str, text: str, message_id: UUID, media: tuple[Path, ...] = ()) -> str:
        """"sent", or "queued" when the session is still working on a turn: it gets the message
        as soon as that turn ends (as in the editor, where you can type while it works). The same
        `message_id` again is told where it is, never sent twice; a caller cut off meanwhile never
        stops the hand-over halfway."""
        if (sending := self._sending.get(message_id)) is None:
            sending = self._sending[message_id] = asyncio.ensure_future(self._send(run_id, text, message_id, media))
            sending.add_done_callback(lambda _: self._sent(run_id, message_id, sending))
        return await asyncio.shield(sending)

    def _sent(self, run_id: str, message_id: UUID, sending: asyncio.Future[str]) -> None:
        """A hand-over ended: its failure is logged here, even when the caller that asked was cut off."""
        self._sending.pop(message_id, None)
        if not sending.cancelled() and (error := sending.exception()) is not None:
            logger.warning("message to session %s not handed over: %s", run_id, error)

    async def _send(self, run_id: str, text: str, message_id: UUID, media: tuple[Path, ...]) -> str:
        if message_id in self._read.get(run_id, ()):
            return "sent"
        if any(queued == message_id for queued, _, _ in self._queued.get(run_id, ())):
            return "queued"
        if self._queued.get(run_id):
            self._queued[run_id].append((message_id, text, media))
            return "queued"
        try:
            await self._hand(run_id, text, message_id, media)
            return "sent"
        except ConversationRefused as error:
            if error.refusal != "conflict":
                raise
        self._queued.setdefault(run_id, []).append((message_id, text, media))
        if run_id not in self._deliveries:
            self._deliveries[run_id] = asyncio.get_running_loop().create_task(self._deliver(run_id))
        return "queued"

    async def _deliver(self, run_id: str, every: float = 0.25) -> None:
        """Hand the queued messages over in order, each as soon as the session refuses it no
        longer (its turn ended): asked every `every` seconds, an in-process call, never the network."""
        try:
            while self._queued.get(run_id):
                await asyncio.sleep(every)
                message_id, text, media = self._queued[run_id][0]
                try:
                    await self._hand(run_id, text, message_id, media)
                except ConversationRefused as error:
                    if error.refusal == "conflict":
                        continue  # still on its turn
                    logger.warning("queued message to session %s not delivered: %s", run_id, error)
                    self._queued.pop(run_id, None)
                    return
                self._queued[run_id].pop(0)
        finally:
            self._deliveries.pop(run_id, None)
            if not self._queued.get(run_id):
                self._queued.pop(run_id, None)

    async def _hand(self, run_id: str, text: str, message_id: UUID, media: tuple[Path, ...] = ()) -> None:
        """A turn of session `run_id` opened on `text` and the images at `media`, and `message_id` recorded as read."""
        await (await self.host()).send(run_id, text, media)
        self._read.setdefault(run_id, deque(maxlen=MACHINE_AGENT_READ)).append(message_id)

    def read(self, run_id: str) -> tuple[UUID, ...]:
        """The messages session `run_id` has read since this runner started, the latest first."""
        return tuple(reversed(self._read.get(run_id, ())))

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
    #: The launcher session every web-started run is recorded under (one per machine, `web_session`).
    session: str
    #: How a web launcher session's id starts.
    web_prefix: ClassVar[str] = "web-"
    #: The starts launching in this process now, by request id (`_start`): a repeat joins its first, whichever answerer it reaches.
    _starting: ClassVar[dict[UUID, asyncio.Future[MachineAgentAnswer]]] = {}

    @classmethod
    def web_session(cls, machine_id: str) -> str:
        """The launcher session of the runs the web starts on machine `machine_id`."""
        return f"{cls.web_prefix}{machine_id}"

    @classmethod
    def is_web_session(cls, session_id: str | None) -> bool:
        """`session_id` is a web launcher session (`web_session`)."""
        return (session_id or "").startswith(cls.web_prefix)
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
    #: The runner's agent child started ahead of the next start (`galaius.agents.warm`).
    warm: WarmStart | None = None
    #: Appends one line to an owner-only log of this PC (`MachineRunner.audit`): log name, entry.
    audit: Callable[[str, dict[str, object]], None] | None = None
    #: The runner's delivery of messages to its runs between their turns (`galaius.agents.followup`).
    followups: FollowUps | None = None
    #: The agent programs installed from the web (`programs` / `program_install`).
    programs: AgentPrograms | None = None

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
        # Programs are not agents: installed and signed in whatever the agent settings say.
        if not isinstance(request, AgentRunsRequest | AgentTailRequest | AgentMediaRequest | AgentStopRequest | AgentLogsRequest | WorkspacesRequest
                          | AgentProgramsRequest | AgentProgramInstallRequest | AgentSignOutRequest | AgentSignInCodeRequest
                          | AgentToolsRequest | AgentToolInstallRequest):
            if not self.run_agents:
                raise PermissionError("agents are off on this computer; its owner turns them on on its page on the web, or there with `galaius machine agents on`")
            if not self.roots:
                raise PermissionError("no agent folders on this computer yet; its owner adds one on its page on the web, or there with `galaius machine agent-roots <folder>`")
        if self.fence_agents and isinstance(request, (AgentStartRequest, AgentContinueRequest)) and (request.kind == "session" if isinstance(request, AgentStartRequest) else True):
            raise PermissionError("sessions and continued conversations are not fenced yet, and the agent fence is on here: start an agent (a role), "
                                  "or its owner switches the fence off there with `galaius machine fence off`")
        if isinstance(request, AgentContinueRequest) or (isinstance(request, AgentStartRequest) and request.kind == "session"):
            self.default_login_only(request)
        match request:
            case AgentFoldersRequest():
                return await asyncio.to_thread(self._folders, request)
            case AgentRunsRequest():
                return await asyncio.to_thread(self._runs, request)
            case AgentTailRequest():
                return await asyncio.to_thread(self._tail, request)
            case AgentMediaRequest():
                return await asyncio.to_thread(self._media, request)
            case ProjectFilesBase():
                return await asyncio.to_thread(self._project_files, request)
            case AgentOptionsRequest():
                return await self._options(request)
            case AgentStartRequest(kind="session"):
                return await self._start_session(request)
            case AgentStartRequest():
                return await self._start(request)
            case AgentCompactRequest():
                return await self._compact(request)
            case AgentSendRequest() | AgentStopRequest() | AgentAnswerRequest() if self._kind(request.run_id) == "session":
                return await self._session_action(request)
            case AgentSendRequest():
                return await self._send(request)
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
            case WorkspaceCreateRequest():
                job = await asyncio.to_thread(self._workspaces().create, request)
                return MachineAgentAnswer(request_id=request.id, workspaces=(job,), detail=f"{'receiving' if job.state == 'running' else 'created'} {job.root}/{job.name}")
            case WorkspacePackRequest():
                pack = await asyncio.to_thread(self._pack, request)
                return MachineAgentAnswer(request_id=request.id, pack=pack, detail="sending" if request.transfer else "measured")
            case WorkspacesRequest():
                return MachineAgentAnswer(request_id=request.id, workspaces=self._workspaces().jobs.read()[:50])
            case AgentProgramsRequest():
                return MachineAgentAnswer(request_id=request.id, providers=await asyncio.to_thread(self._programs().states))
            case AgentProgramInstallRequest():
                state = await asyncio.to_thread(self._programs().install, request.provider, request.subscription_id)
                return MachineAgentAnswer(request_id=request.id, providers=(state,), detail=f"{request.provider}: {state.step}")
            case AgentSignOutRequest():
                state = await asyncio.to_thread(self._programs().sign_out, request.provider, request.subscription_id)
                return MachineAgentAnswer(request_id=request.id, providers=(state,), detail=f"{request.provider}: signed out")
            case AgentSignInCodeRequest():
                state = await asyncio.to_thread(self._programs().paste, request.provider, request.subscription_id, request.attempt, request.sealed)
                return MachineAgentAnswer(request_id=request.id, providers=(state,), detail=f"{request.provider}: code handed to its sign-in")
            case AgentToolsRequest():
                return MachineAgentAnswer(request_id=request.id, tools=await asyncio.to_thread(MachineTools.states))
            case AgentToolInstallRequest():
                tools = await asyncio.to_thread(MachineTools.install, request.tool)
                return MachineAgentAnswer(request_id=request.id, tools=tools, detail=f"{request.tool}: {next(tool.phase for tool in tools if tool.name == request.tool)}")
            case AgentAllowanceRequest():
                return await self._allow(request)
            case AgentMoveRequest():
                return await self._move(request)

    def _programs(self) -> AgentPrograms:
        if self.programs is None:
            raise PermissionError("this computer does not install agent programs")
        return self.programs

    def _workspaces(self) -> MachineWorkspaces:
        if self.workspaces is None:
            raise PermissionError("this computer does not prepare workspaces")
        return self.workspaces

    def _pack(self, request: WorkspacePackRequest) -> WorkspacePack:
        if request.root in self.roots and not self.roots[request.root].joinpath(*filter(None, request.path.split("/"))).exists():
            raise WorkspaceRefused("source_missing", f"{request.root}/{request.path} does not exist on this computer")
        return self._workspaces().pack(request, self.folder(request.root, request.path))

    def _refresh(self, folder: Path) -> WorkspaceRefresh | None:
        """A clone made here from the web, fast-forwarded before a launch in `folder` (`MachineWorkspaces.refresh`)."""
        return self.workspaces.refresh(folder) if self.workspaces is not None else None

    def scope(self, asked: AgentTouchScope | None) -> AgentTouchScope:
        """What a run started now may do: `asked`, capped by this PC's permission."""
        return self.permission if asked is None else min(asked, self.permission, key=AGENT_TOUCH_SCOPES.index)

    def environment_in(self, folder: Path) -> dict[str, str]:
        """The child's environment for an agent working in `folder`: a workspace cloned from the
        web loads none of its own agent settings (its hooks would run code its owner never read),
        and every folder put here from the web is named (`UNTRUSTED_FOLDERS`: no git of galaius's runs there)."""
        placed = [str(place) for _, place in self.workspaces.placed()] if self.workspaces is not None else []
        environment = {**self.environment, UNTRUSTED_FOLDERS: os.pathsep.join(placed)} if placed else self.environment
        untrusted = self.workspaces is not None and self.workspaces.prepared(folder)
        return {**environment, PROJECT_SETTINGS_OFF: "1"} if untrusted else environment

    def launcher_environment_in(self, folder: Path) -> dict[str, str]:
        """What the launcher runs a role's agent in `folder` with: `environment_in` and this PC's
        persisted settings, as a galaius command started in that environment applies them (its
        keys and routes decide which models can run)."""
        return UserConfig.applied(self.environment_in(folder), portable=False)

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
        """`path` beneath agent root `root`, walked part by part from the root itself: every part a
        plain, unhidden folder, never a link. Raises PermissionError otherwise."""
        base = self.roots.get(root)
        if base is None:
            raise PermissionError(unusable(root))
        parts = [part for part in path.split("/") if part]
        if any(part in {".", ".."} or part.startswith(".") or "\\" in part for part in parts):
            raise PermissionError("agent folders never include hidden or relative names")
        for index in range(len(parts) + 1):
            current = base.joinpath(*parts[:index])
            try:
                facts = current.lstat()
            except FileNotFoundError:
                raise PermissionError(f"{root}/{path} does not exist") from None
            if not stat.S_ISDIR(facts.st_mode) or PinnedDirectory.link_like(facts):
                raise PermissionError(f"{root}/{path} is not a plain folder (links and junctions are never followed)")
        return current

    def _folders(self, request: AgentFoldersRequest) -> MachineAgentAnswer:
        permission, roots = self.permission, tuple(sorted(self.roots))
        if not request.root:
            return MachineAgentAnswer(request_id=request.id, roots=roots, permission=permission, roles=self.roles())
        target = self.folder(request.root, request.path)
        origin = MachineWorkspaces.remote_of(target)
        names = []
        with os.scandir(target) as entries:
            for entry in entries:
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False):
                    names.append(entry.name)
        names.sort(key=str.lower)
        folders = tuple(MachineFileEntry(name=name, kind="folder") for name in names[:500])
        return MachineAgentAnswer(request_id=request.id, roots=roots, entries=folders, truncated=len(names) > 500, permission=permission, origin=origin)

    def _project_files(self, request: ProjectFilesBase) -> MachineAgentAnswer:
        """A project folder's files beneath one agent root, read-only (`ProjectFolder`), only where this PC's levels and
        its log are known. Every read, refused ones included, leaves its trace where his browse pages do: this PC's own
        places log, never the server."""
        if self.places is None or self.audit is None:
            raise PermissionError("project files are shown only where this PC's levels and log are read")
        refused = None
        try:
            root = self.roots.get(request.root)
            if root is None:
                raise PermissionError(unusable(request.root))
            placed = tuple(place for _, place in self.workspaces.placed()) if self.workspaces is not None else ()
            return ProjectFolder.of(root, request, secrets=self.secrets, environment=self.environment, places=self.places, untrusted=placed).answer(request)
        except Exception as error:
            refused = str(error)
            raise
        finally:
            # A player, a viewer or a whole text read part by part or page by page: the part opening it alone is logged
            # (`opens`), and every refused one.
            if refused is not None or not isinstance(request, ProjectFilePart) or request.opens:
                self.audit(PLACES_LOG, {"op": "project_files", "read": request.op, "root": request.root, "base": request.base,
                                        **request.model_dump(include={"path", "query", "offset"}), "refused": refused, "initiator_account": str(request.initiator_account)})

    def _allowed(self) -> tuple[dict[str, WebRun], list[reg.AgentRun]]:
        return {str(item.run_id): item for item in self.runs.read()}, self.runs.runs()

    def _runs(self, request: AgentRunsRequest) -> MachineAgentAnswer:
        started, runs = self._allowed()
        # A run launched inside a fenced run is inside the same fence.
        fenced = {run.run_id for run in runs if run.fence is not None}
        found, secrets = [], self.secrets
        for run in runs[:200]:
            placed = started.get(run.run_id)
            root, path = (placed.root, placed.path) if placed else self._place(run.cwd)
            kind = placed.kind if placed else "agent"
            found.append(MachineAgentRun(
                run_id=UUID(run.run_id), name=run.name[:120], role=(run.agent or None) and run.agent[:80], provider=run.provider[:40],
                model=run.model and run.model[:MODEL_ID_MAX], reasoning=run.reasoning, status=run.status, root=root[:240], path=path[:1024], task=run.task[:8000], media=run.media,
                last=redact(run.last, secrets)[:400], started_at=run.started_at, finished_at=run.finished_at, cost_usd=run.cost_usd,
                parent_run_id=UUID(run.parent_run_id) if run.parent_run_id and run.run_id not in started else None,
                kind=kind, pending=self.pending(run.run_id) if kind == "session" and run.status == "waiting" else (),
                fenced=bool({run.run_id, run.root_run_id, run.parent_run_id} & fenced), compacts=self.compact_refusal(run, kind) is None,
                passed_over=tuple(PassedOverCandidate(provider=item.candidate.provider, model=item.candidate.model[:MODEL_ID_MAX], reason=item.reason, until=item.until)
                                  for item in run.skipped[:16]),
                subscription=run.subscription, stopped_by=run.stopped_by if run.status == "stopped" else None, replaced_by=run.replaced_by,
                asked_by=tuple(UUID(asker) for asker in run.asked_by), community=run.community.ref() if run.community else None,
                **{field: getattr(run, field) for field in ContextGauge.model_fields},
            ))
        return MachineAgentAnswer(request_id=request.id, runs=tuple(found), web=self.runs.load(request))

    @staticmethod
    def compact_refusal(run: reg.AgentRun, kind: AgentRunKind) -> str | None:
        """Why `run` cannot be compacted from the web; None when it can. Its row's `compacts` says it before any press,
        `_compact` refuses by it: a session through its transport (Codex's app-server compacts, a completion route
        cannot), a queued run through its CLI's own command (`CompactionTurn`), an editor's conversation in its editor."""
        if kind == "continued":
            return "a continued editor conversation is compacted in its editor"
        if kind == "session":
            return None if _TransportRegistry.transport_type(run).compacts else "this conversation's provider cannot compact it"
        return agent_queue.CompactionTurn.refused_for(run.provider)

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
        """Exact values a run's output never shows: the environment's credentials and the tokens each
        subscription's CLI keeps in its folder now (read again at each call: a refresh changes them)."""
        return (*secret_values(self.environment), *Subscription.tokens())

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
        if run.status in {"running", "waiting"} or not reg.mirror_is_current(str(run.run_id)):
            # The readable copy of a working run's stream is rebuilt on demand; nothing else on a
            # PC with no editor open would rebuild it while the owner watches. An ended run's too once
            # its messages moved: a resume refused before its turn started is read at once, never only
            # when the next turn runs.
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
        tokens = Subscription.tokens()
        lines = tuple(mask(line.decode("utf-8", "replace"), tokens) for line in data[:whole].split(b"\n") if line)
        if cursor is None and (placed := next((item for item in self.runs.read() if item.run_id == request.run_id and item.source), None)):
            lines = (*self._editor_history(placed.source), *lines)[-TAIL_LINES:]
        return MachineAgentAnswer(request_id=request.id, lines=lines, cursor=start + whole, truncated=skipped, read_messages=self._read_messages(request.run_id))

    def _read_messages(self, run_id: UUID) -> tuple[UUID, ...]:
        """The messages from the web (`AgentSendRequest.id`) run `run_id` has read, the latest first:
        a session's since this runner started; a continued copy's, each opening its own turn; an
        agent's, handed to its turn (`agent_queue.READ`)."""
        match self._kind(run_id):
            case "session":
                return self.sessions.read(str(run_id)) if self.sessions is not None else ()
            case "continued":
                ids = [event.event_id for event in reversed(reg.read_messages(str(run_id))) if event.from_run == reg.PERSON_SENDER]
            case _:
                ids = agent_queue.read_ids(str(run_id))
        # Only ids the web gave (a request id, as it writes it): a message sent from this computer has its own.
        return tuple(UUID(value) for value in ids if _WEB_ID.fullmatch(value))[:MACHINE_AGENT_READ]

    def _media(self, request: AgentMediaRequest) -> MachineAgentAnswer:
        """One image of this web run: one it was shown (`RunMedia`), or one a step names (`media_key` of the path it
        wrote: only a path that run's own stream names); only a plain file (never a link, a FIFO or a device: opened
        non-blocking, checked before any read), at most MACHINE_AGENT_MEDIA bytes, its type proven
        by its first bytes. A path a later step overwrote serves its CURRENT bytes. Or the part from `offset` of one file
        a file tool kept for it (`RunFiles`, by the hash of its bytes): that copy alone, never a path any step names."""
        run = self._require_run(request.run_id)
        if media_kind(request.name) == "file":
            data, size = self._plain_file(reg.RunFiles.path(run.run_id, request.name), "file", request.offset)
            return MachineAgentAnswer(request_id=request.id, file=AgentFile.of(data, request.name, request.offset, size))
        path = reg.RunMedia.path(run.run_id, request.name)
        if not path.is_file():
            written = self._named_image(run.run_id, request.name)
            if written is None or not written.startswith(("/", "~/")):
                raise PermissionError("no step of this run names that image")
            path = Path.home() / written[2:] if written.startswith("~/") else Path(written)
        data, _size = self._plain_file(path, "image")
        content_type = image_type(data)
        if content_type is None:
            raise PermissionError("that file is not an image")
        return MachineAgentAnswer(request_id=request.id, media=AgentMedia(content_type=content_type, data=base64.b64encode(data).decode("ascii")))

    @staticmethod
    def _plain_file(path: Path, what: str, offset: int | None = None) -> tuple[bytes, int]:
        """`path`'s bytes when it is a plain file of at most MACHINE_AGENT_MEDIA bytes, or (`offset` given: a kept file)
        its MACHINE_AGENT_MEDIA bytes from `offset`; and its whole size. Never a link, a FIFO or a device (opened
        non-blocking, checked before any read)."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            raise PermissionError(f"that {what} is no longer on this computer") from None
        with os.fdopen(descriptor, "rb") as handle:
            facts = os.fstat(handle.fileno())
            if not stat.S_ISREG(facts.st_mode):
                raise PermissionError(f"that {what} is not a plain file")
            if offset is not None:
                if offset >= max(facts.st_size, 1):
                    raise PermissionError(f"that {what} ends before byte {offset:,}")
                handle.seek(offset)
                return handle.read(MACHINE_AGENT_MEDIA), facts.st_size
            if facts.st_size > MACHINE_AGENT_MEDIA:
                raise PermissionError(f"that {what} is larger than {MACHINE_AGENT_MEDIA // (1024 * 1024)} MiB")
            data = handle.read(MACHINE_AGENT_MEDIA + 1)
        if len(data) > MACHINE_AGENT_MEDIA:
            raise PermissionError(f"that {what} is larger than {MACHINE_AGENT_MEDIA // (1024 * 1024)} MiB")
        return data, len(data)

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

    @staticmethod
    @contextmanager
    def staged(images: tuple[AgentMedia, ...]) -> Iterator[tuple[Path, ...]]:
        """The images a start shows, as files while it starts (before its run exists): the run keeps its own copies."""
        with tempfile.TemporaryDirectory(prefix="galaius-images-") as folder:
            paths = tuple(Path(folder) / image.name for image in images)
            for path, image in zip(paths, images):
                path.write_bytes(image.content)
            yield paths

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

    @staticmethod
    def default_login_only(request: Lent) -> None:
        """Sessions and continued editor conversations run on this PC's default login only (the
        editor's history lives there): one asked on a subscription, or for a member on a lent PC, is refused."""
        if request.lent:
            raise SubscriptionRefused("lent", LENT_REFUSAL)
        if request.names_subscription:
            raise SubscriptionRefused("default_login_only", "sessions and continued editor conversations run on this PC's own login only; start an agent (a role) to use this subscription")

    def pinned(self, request: AgentStartRequest) -> RunSubscription | None:
        """What a start is pinned to before its walk: no CLI login at all on a lent PC — only inside the fence there:
        unfenced, its agent could clear its login variables and run as the owner, the same OS user. Otherwise none:
        the walk pins it to the subscription its candidate starts on (`request.subscriptions`, `run_agent`)."""
        if not request.lent:
            return None
        if not self.fence_agents:
            raise SubscriptionRefused("lent", f"{LENT_REFUSAL} (this PC runs a member's agent only inside its fence, and the fence is off)")
        return RunSubscription(lent=True)

    async def _start(self, request: AgentStartRequest) -> MachineAgentAnswer:
        """A start asked again under its id (`AgentStartRequest.idempotent_feature`: its answer lost with the line, a
        restart between) answers the run that id launched, or joins its start still under way — the request carrying it
        cancelled or past its bound: never a second agent. The bound is this asker's (`START_SECONDS`, `under_way`), never
        the start's: the start stays keyed until its agent is recorded, so one asked again after a PC frozen past the bound
        joins it (visual-critic launch r2: frozen 170 s, the first start unkeyed at its bound while its agent still came up)."""
        if (known := await asyncio.to_thread(self.runs.started_by, request.id)) is not None:
            return await self._answered(request, known.run_id, known.fenced)
        if (starting := self._starting.get(request.id)) is None:
            starting = self._starting[request.id] = asyncio.ensure_future(self._start_once(request))
            starting.add_done_callback(lambda done, key=request.id: (self._starting.pop(key, None), done.cancelled() or done.exception()))
        try:
            return await asyncio.wait_for(asyncio.shield(starting), START_SECONDS)
        except TimeoutError:
            raise AgentRefused("under_way", f"the agent is still starting after {START_SECONDS:.0f} s; it appears in the list once it runs") from None

    async def _answered(self, request: AgentStartRequest, run_id: UUID, fenced: bool, refresh: WorkspaceRefresh | None = None, written: int = 0) -> MachineAgentAnswer:
        started = await asyncio.to_thread(reg.get_run, str(run_id))
        return MachineAgentAnswer(request_id=request.id, run_id=run_id, fenced=fenced, refresh=refresh, detail=f"{written} project secrets written to .env" if written else "",
                                  subscription_id=started.subscription_id if started is not None else None)

    async def _start_once(self, request: AgentStartRequest) -> MachineAgentAnswer:
        pinned = await asyncio.to_thread(self.pinned, request)
        folder, written, fence, review = await asyncio.to_thread(self._prepare_start, request, pinned)
        refresh = await asyncio.to_thread(self._refresh, folder)
        environment = await asyncio.to_thread(self.launcher_environment_in, folder)

        async def launch() -> UUID:
            """The whole start, recorded as a web run even when the request that asked for it is
            gone (cancelled, past its bound): a started agent is never left untracked."""
            try:
                with reg.session_context(self.session, environment=environment), self.staged(request.images) as images:
                    # The launcher's supervisor window (not the terminal's 20 s): the run shows at
                    # once; a quota refusal after it is that run's failure, in the list.
                    handle = await run_agent(
                        provider_for(request.provider) if request.provider is not None else None, request.text,
                        cwd=str(folder), agent=str(request.role), permission_mode=self.scope(request.permission), image_paths=images,
                        quota_window=4.0, fence=fence, environment=environment, warm=self.warm, subscription=pinned, choices=request,
                        reasoning=request.reasoning,
                    )
            except BaseException:
                if review is not None:
                    self.reviews.discard(review)
                raise
            run_id = UUID(handle.run_id)
            if self.followups is not None:
                self.followups.follow(handle.run_id, handle.process, environment=environment)
            if review is not None:
                self.reviews.attach(review, run_id)
            self.runs.add(WebRun(run_id=run_id, root=request.root, path=request.path, fenced=fence is not None, request_id=request.id))
            return run_id

        try:
            run_id = await asyncio.shield(AgentSpawns.run(launch))
        except (ValueError, RuntimeError) as error:
            if request.lent:
                raise SubscriptionRefused("lent", LENT_REFUSAL) from error
            raise RuntimeError(self._said(str(error)) or "the agent did not start") from error
        return await self._answered(request, run_id, fence is not None, refresh, written)

    def _prepare_start(self, request: AgentStartRequest, pinned: RunSubscription | None) -> tuple[Path, int, FenceSpec | None, UUID | None]:
        """What a start is checked and prepared with before anything runs: its folder, how many
        project secrets were written there, and its fence and staging review when the fence is on."""
        self.runs.refuse_full(request)
        folder = self.folder(request.root, request.path)
        if request.model is not None:
            # The launcher runs a role on what its own rule picks (a per-run model is never
            # honoured): refused, never silently replaced.
            raise PermissionError("an agent runs on the model its rule picks; change the rule on its Agents page, or start a session to choose the model")
        written = self._project_env(request, folder)
        # A lent run binds only closed login folders; a start's candidates each get theirs in the walk (`run_agent`).
        homes = pinned.homes() if pinned is not None else {}
        fence, review = self._fence(request.root, folder, homes) if self.fence_agents else (None, None)
        return folder, written, fence, review

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

    def _fence(self, root: str, folder: Path, homes: dict[str, Path]) -> tuple[FenceSpec, UUID | None]:
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
        project = None
        if root == PROJECTS_ROOT:   # one project folder written, no level (`FenceSpec.project`)
            inside = folder.relative_to(self.roots[root]).parts
            if not inside:
                raise PermissionError("with the fence on, an agent starts inside one project folder, never in the projects folder itself")
            project = self.roots[root] / inside[0]
        elif LEVEL_RANK[places.reach(parts)] < LEVEL_RANK["read"]:
            raise PermissionError(f"no level opens {'/'.join(parts)} to agents; with the fence on, its owner sets one there first (`galaius machine places {root} write`)")
        staging, review = {}, None
        if project is None and places.reach(parts) == "write_on_review":
            place, _ = places.place_of(parts)
            review, staging[place] = self.reviews.stage_copy(place, base.joinpath(*split(place)), origin="agent", run_id=None)
        try:
            programs = tuple(Path(found) for found in (*(shutil.which(name) for name in AGENT_PROVIDERS), *self.cli) if found)
            spec = FenceSpec(working_directory=places.working_directory, levels_file=self.levels_file, levels=places.levels, internal=places.internal, start=folder, project=project,
                             staging=staging, providers=AGENT_PROVIDERS, homes=homes, programs=programs, state=self.reviews.root.parent / "fence-state" / str(uuid4()), reviews=self.reviews.root,
                             egress=(*(host for provider in AGENT_PROVIDERS for host in EGRESS[provider]), *self.egress))
            spec.build()
            return spec, review
        except BaseException:
            if review is not None:
                self.reviews.discard(review)
            raise

    async def _send(self, request: AgentSendRequest) -> MachineAgentAnswer:
        run = await asyncio.to_thread(self._pinned_run, request)
        if request.reasoning is not None and self._kind(request.run_id) != "agent":
            raise PermissionError("an editor conversation runs at the effort its editor sets; only an agent's is set here")
        media = await asyncio.to_thread(reg.RunMedia.keep, run.run_id, request.images)
        if self._kind(request.run_id) == "continued":
            return await asyncio.to_thread(self._continue_turn, run, request.text, request_id=request.id, media=media)
        if request.text == reg.RESUME_BRIEF:  # his « Continuer »; a message he types waits for the turn it starts
            run.prepare_continue()
        run = await asyncio.to_thread(reg.carry_held, run.run_id) or run  # a move held for it, its turn ended: made first
        # Answered once the message is durably queued, under the request's id (the same request
        # again finds it there and queues nothing): this runner starts the turn when the run is
        # between turns (`FollowUps`), else its dispatcher resumes the agent; a resume refused after
        # this point is that run's failure, in its events.
        environment = await asyncio.to_thread(self.launcher_environment_in, Path(run.cwd))
        turn = agent_queue.MessageTurn(media=media)
        # Set before the message is queued (the turn it starts reads it), back as it was if the message is not.
        if request.reasoning is not None:
            await asyncio.to_thread(reg.reasoning_set, run.run_id, request.reasoning)
        try:
            if self.followups is not None:
                followups = self.followups
                delivery = await AgentSpawns.run(lambda: followups.send(str(request.run_id), request.text, environment=environment, message_id=str(request.id), turn=turn))
            else:
                delivery = await asyncio.to_thread(deliver_message, str(request.run_id), request.text, sender=reg.PERSON_SENDER, environment=environment, message_id=str(request.id), turn=turn)
            if delivery.state == "error":
                raise RuntimeError(delivery.text.removeprefix("ERROR: ") or "not delivered")
        except BaseException:
            if request.reasoning is not None:
                await asyncio.to_thread(reg.reasoning_set, run.run_id, run.reasoning_set)
            raise
        # The account that turn runs on (a move held for it included): the server's pin follows it.
        current = await asyncio.to_thread(reg.get_run, run.run_id) or run
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail=delivery.text, reads_at=delivery.reads_at,
                                  subscription_id=current.next_subscription_id)

    async def _compact(self, request: AgentCompactRequest) -> MachineAgentAnswer:
        """Refused by `compact_refusal`, the rule its row says; a session compacts through its host, a queued run as a
        turn of its own (`CompactionTurn`) under the request's id, started by its dispatcher once it is between turns."""
        run = await asyncio.to_thread(self._require_run, request.run_id)
        kind = self._kind(request.run_id)
        if (refusal := self.compact_refusal(run, kind)) is not None:
            raise PermissionError(refusal)
        if kind == "session":
            return await self._session_action(request)
        environment = await asyncio.to_thread(self.launcher_environment_in, Path(run.cwd))
        delivery = await asyncio.to_thread(deliver_message, run.run_id, "", sender=reg.PERSON_SENDER, environment=environment,
                                           message_id=str(request.id), turn=agent_queue.CompactionTurn())
        if delivery.state == "error":
            raise RuntimeError(delivery.text.removeprefix("ERROR: ") or "not compacted")
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail="compacting", reads_at=delivery.reads_at)

    def _pinned_run(self, request: AgentSendRequest) -> reg.AgentRun:
        """The run a message goes to, on the login it is pinned to (a move held for it included): one naming its own
        subscription brings its caps as the server has them now; one naming another login runs where the run is, its
        answer naming that login (`AgentRun.next_subscription_id`) so the server's pin follows this PC. A member's message
        on a lent PC to a run of the owner's logins is refused."""
        run = self._require_run(request.run_id)
        if request.lent and (run.subscription is None or not run.subscription.lent):
            raise SubscriptionRefused("lent", LENT_REFUSAL)
        if run.subscription is not None and request.subscription_id == run.subscription_id and request.limits is not None and request.limits != run.subscription.limits:
            run = reg.merge_subscription(run.run_id, limits=request.limits) or run
        return run

    async def _move(self, request: AgentMoveRequest) -> MachineAgentAnswer:
        """`RunMove`: the run pinned to another login from now on (contract A9), a subscription or this PC's own
        (`subscription_id` None), its session carried there (`registry.carry`) and resumed, unless the request does not
        `resume`: his pick in a conversation, the switch before a turn, made at once on a stopped run, at the end of the
        turn at work on one working (`_carry`). An editor's own conversation runs on the login its editor runs: refused typed."""
        run = await asyncio.to_thread(self._require_run, request.run_id)
        if self._kind(request.run_id) != "agent":
            raise SubscriptionRefused("default_login_only", "an editor conversation runs on the login its editor runs: only an agent started here moves")
        moved = await asyncio.to_thread(self._carry, run, request)
        there = f"subscription {request.subscription_id}" if request.subscription_id is not None else "this PC's own login"
        # A run at work is not resumed: it goes on, its next turn there; nor one already there: nothing moved.
        if not request.resume or moved.status in {"running", "waiting"} or request.subscription_id == run.subscription_id:
            said = "its next turn runs there" if moved.pending_move is None else "its next turn runs there, once the one at work ended"
            return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail=f"moved to {there}; {said}", subscription_id=request.subscription_id)
        # Moved from here on, whatever the resume does: the answer names the new pin so the server records it too.
        try:
            environment = await asyncio.to_thread(self.launcher_environment_in, Path(moved.cwd))
            delivery = await asyncio.to_thread(deliver_message, moved.run_id, reg.RESUME_BRIEF, sender=reg.PERSON_SENDER, environment=environment, message_id=str(request.id))
            detail, reads_at = (f"moved to {there}; not resumed: {delivery.text.removeprefix('ERROR: ')}", None) if delivery.state == "error" \
                else (f"continued on {there}", delivery.reads_at)
        except Exception as error:  # the move stands; its resume's failure is said, « Continuer » there resumes it
            detail, reads_at = f"moved to {there}; not resumed: {error}"[:400], None
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail=detail[:400], reads_at=reads_at, subscription_id=request.subscription_id)

    @staticmethod
    def _carry(run: reg.AgentRun, request: AgentMoveRequest) -> reg.AgentRun:
        """The move made on stopped `run`, from and to a subscription or this PC's own login alike; on a run at work, checked
        now (`registry.movable`) and held for that turn's end (`registry.hold_move`). Asked for the login the run is on:
        the move held for it dropped, nothing carried. Never signs any login out."""
        pinned = RunSubscription(subscription_id=request.subscription_id, limits=request.limits) if request.subscription_id is not None else None
        reg.movable(run, pinned if run.status in {"running", "waiting"} else None)
        if request.subscription_id == run.subscription_id:
            return reg.hold_move(run.run_id, None) or run
        if run.status not in {"running", "waiting"}:
            return reg.carry(run, pinned)
        return reg.hold_move(run.run_id, pinned if pinned is not None else reg.OWN_LOGIN) or run

    async def _allow(self, request: AgentAllowanceRequest) -> MachineAgentAnswer:
        """`RunAllowance`: the run may go on its subscription up to `up_to_percent` of the window it is
        held or cut at; one held between turns gets its next turn now (as « Reprendre » does)."""
        run = await asyncio.to_thread(self._require_run, request.run_id)
        if run.subscription is None or run.subscription.subscription_id is None:
            raise PermissionError("this run is on no subscription: it has no cap to go past")
        capped = run.subscription.capped
        if capped is None:
            raise PermissionError("no cap holds this run: an allowance goes past the cap a run was held or cut at")
        if request.up_to_percent <= capped.cap_percent:
            raise PermissionError(f"an allowance goes past the cap ({capped.cap_percent}%): ask for more than that, up to 100%")
        allowance = SubscriptionAllowance(window=capped.window, up_to_percent=request.up_to_percent)
        run = await asyncio.to_thread(reg.merge_subscription, run.run_id, allowance=allowance.model_dump(mode="json"), capped=None) or run
        if run.status in {"running", "waiting"}:
            return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail=f"allowed up to {request.up_to_percent}%")
        environment = await asyncio.to_thread(self.launcher_environment_in, Path(run.cwd))
        delivery = await asyncio.to_thread(deliver_message, run.run_id, reg.RESUME_BRIEF, sender=reg.PERSON_SENDER, environment=environment, message_id=str(request.id))
        if delivery.state == "error":
            raise RuntimeError(delivery.text.removeprefix("ERROR: ") or "not resumed")
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail=f"allowed up to {request.up_to_percent}%; resumed", reads_at=delivery.reads_at)

    async def resume_paused(self) -> list[str]:
        """The runs a cut stopped here (`registry.cut_resume`), each resumed once by itself with `RESUME_BRIEF`, as
        « Continuer » does (its dispatcher, the launcher's environment for its folder): a process cut (gone, the PC
        restarted, killed by the system or a signal nobody it answers to sent) at once, a provider usage limit at its
        reset, a lost login once that login is signed in again here (its status asked once per login). Every run of a
        tree the cut stopped resumes, deepest first (`registry.deepest_first`): its agents are at work again before a
        launcher reads where they stand, and its brief names them (`AGENTS_RESUMED`). A resume refused before its turn
        starts becomes the run's failure, with why (never resumed by itself again). Returns the ids it resumed."""
        claimed: list[tuple[reg.AgentRun, str, str]] = []
        signed_in: dict[tuple[str, UUID | None], bool | None] = {}
        # A lost login pausing this PC's turns (`quota.login_pause`) lifts once signed in again, held turns starting then.
        for login in await asyncio.to_thread(quota.lost_logins):
            if self.programs is not None and login not in signed_in:
                signed_in[login] = await asyncio.to_thread(self.programs.signed_in, *login)
            if signed_in.get(login):
                await asyncio.to_thread(quota.login_back, login[0], subscription=login[1])
        relaunching: list[reg.AgentRun] = []
        for run, cut in await asyncio.to_thread(reg.schedule_resumes):
            if cut.resumes == "relaunch":
                if (taken := await asyncio.to_thread(reg.claim_resume, run.run_id)) is not None:
                    relaunching.append(taken)
                continue
            ready = False
            if cut.resumes == "sign_in":
                login = run.provider, run.subscription_id
                if self.programs is not None and login not in signed_in:
                    signed_in[login] = await asyncio.to_thread(self.programs.signed_in, *login)
                if not (ready := bool(signed_in.get(login))):
                    continue
            if await asyncio.to_thread(reg.claim_resume, run.run_id, signed_in=ready) is not None:
                claimed.append((run, *_RESUMED_AFTER[cut.cause]))
        resumed: list[str] = [run.run_id for run in relaunching if await self._relaunch(run)]
        for run, sender, after in claimed:  # deepest first, as scheduled
            if await self._resume(run, sender, after, agents=await asyncio.to_thread(self._cut_with, run.run_id, resumed)):
                resumed.append(run.run_id)
        return resumed

    async def _relaunch(self, run: reg.AgentRun) -> bool:
        """Claimed run `run`'s brief started again on the next ranked model (`SpawnLine.relaunch`), in the launcher's
        environment for its folder. One that cannot start leaves `run` to its reset (`registry.limit_resume_at`), said in
        the log."""
        # Circular layers: spawn_line imports this module (the web's runs).
        from galaius.agents.spawn_line import SpawnLine

        try:
            environment = await asyncio.to_thread(self.launcher_environment_in, Path(run.cwd or "."))
            new = await SpawnLine.here().relaunch(run, functools.partial(run_agent, environment=environment))
        except Exception as error:  # whatever stops it leaves the run to its reset, saying why on its report
            refused = str(error) or type(error).__name__
            await asyncio.to_thread(reg.record_relaunch_refused, run.run_id, refused)
            logger.warning("run %s not relaunched after its limit refused its first turn: %s", run.run_id[:8], refused)
            return False
        logger.info("run %s refused before it started: relaunched as %s", run.run_id[:8], new[:8])
        return True

    @staticmethod
    def _cut_with(run_id: str, resumed: Sequence[str]) -> list[str]:
        """The agents run `run_id` launched, at any depth, that a cut stopped and are back or will be by themselves: resumed
        in this pass (`resumed`), or still to resume (`registry.self_resuming`: a lost login, a usage limit not reset yet), each
        as `galaius agents list` names it (`role-id8`)."""
        return [f"{run.agent}-{run.run_id[:8]}" if run.agent else run.run_id[:8] for run in reg.stored_tree(run_id)
                if run.run_id != run_id and (run.run_id in resumed or reg.self_resuming(run))]

    async def _resume(self, run: reg.AgentRun, sender: str, after: str, *, agents: Sequence[str] = ()) -> bool:
        """Claimed run `run` resumed from `sender`, its brief naming `agents` of its own cut with it that are back or
        will be by themselves (`AGENTS_RESUMED`); whatever stops it ends as the run's failure (« … after `after` failed: why »),
        never a silent pause."""
        brief = reg.RESUME_BRIEF + (f"\n\n{reg.AGENTS_RESUMED.format(runs=', '.join(agents))}" if agents else "")
        try:
            environment = await asyncio.to_thread(self.launcher_environment_in, Path(run.cwd))
            delivery = await asyncio.to_thread(deliver_message, run.run_id, brief, sender=sender, environment=environment)
            refused = delivery.text.removeprefix("ERROR: ") if delivery.state == "error" else ""
        except Exception as error:  # claimed: never a silent pause
            refused = str(error) or type(error).__name__
        if refused:
            await asyncio.to_thread(reg.fail_turn, run.run_id, f"Automatic resume after {after} failed: {refused}")
            logger.warning("run %s not resumed after %s: %s", run.run_id[:8], after, refused)
            return False
        logger.info("run %s resumed after %s", run.run_id[:8], after)
        return True

    async def follow_up(self) -> None:
        """At the runner's start (`FollowUps`): messages its web runs still had queued go to their
        dispatchers; the runs whose turn ended last get their next turn started ahead."""
        if self.followups is None:
            return
        followups, ids = self.followups, {str(item.run_id) for item in self.runs.read() if item.kind == "agent"}
        runs = [run for run in await asyncio.to_thread(self.runs.runs) if run.run_id in ids]
        environments = {run.run_id: await asyncio.to_thread(self.launcher_environment_in, Path(run.cwd)) for run in runs}
        for run in runs:  # one run's failure never keeps another's message waiting
            try:
                if recovered := await asyncio.to_thread(followups.recover, [run], environment=environments[run.run_id]):
                    logger.info("queued follow-up to %s handed to its dispatcher", recovered[0][:8])
            except (OSError, RuntimeError, ValueError) as error:
                logger.warning("queued follow-up to %s not recovered: %s", run.run_id[:8], error)
        for run_id in followups.recent(runs):
            await AgentSpawns.run(lambda run_id=run_id: followups.ready(run_id, environment=environments[run_id]))

    def _stop(self, request: AgentStopRequest) -> MachineAgentAnswer:
        if (stopping := self._stop_editor_session(request)) is not None:
            return stopping
        run = self._require_run(request.run_id)
        if run.status not in {"running", "waiting"}:
            return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail="already ended")
        if not reg.stop(str(request.run_id), by=request.by):
            raise RuntimeError("the run could not be stopped")
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail="stopped")

    def _stop_editor_session(self, request: AgentStopRequest) -> MachineAgentAnswer | None:
        """`request` for one of HIS live editor sessions (no run of the web's): its turn ends at its next tool call
        (`SessionStops`), never by typing into it. None when the id is not such a session. Refused when this session
        cannot be stopped: `hook_missing` the hook is not installed, `opened_before` the session started before it
        was (Claude Code reads its hooks when a session starts: reopened, it can be)."""
        if any(item.run_id == str(request.run_id) for item in self._allowed()[1]):
            return None
        session = next((run for run in reg.list_runs(include_foreign=True) if run.foreign and run.run_id == str(request.run_id)), None)
        if session is None:
            return None
        stops = SessionStops()
        since = stops.installed_at() if stops.installed() else None
        if since is None:
            raise AgentRefused("hook_missing", "this computer has not installed the hook that stops an editor session")
        if session.started_at and session.started_at < since:
            raise AgentRefused("opened_before", "this editor session was opened before this computer could stop it: it can be stopped once opened again")
        stops.ask(request.run_id, request.by)
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail="stopping")

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
        await asyncio.to_thread(self.runs.refuse_full, request)
        route, models, reason = await self.sessions.route()
        if route is None:
            raise PermissionError(reason)
        if request.model is not None and request.model not in models:
            raise PermissionError(f"{request.model} is not a model sessions can use here")
        refresh = await asyncio.to_thread(self._refresh, folder)
        try:
            with self.staged(request.images) as images:
                run = await (await self.sessions.host()).start(request.text, folder, route_id=route, model=request.model, media=images)
        except ConversationRefused as error:
            raise RuntimeError(str(error)) from error
        await asyncio.to_thread(self.runs.add, WebRun(run_id=UUID(run.run_id), root=request.root, path=request.path, kind="session"))
        return MachineAgentAnswer(request_id=request.id, run_id=UUID(run.run_id), refresh=refresh)

    async def _session_action(self, request: AgentSendRequest | AgentStopRequest | AgentAnswerRequest | AgentCompactRequest) -> MachineAgentAnswer:
        assert self.sessions is not None
        run = await asyncio.to_thread(self._require_run, request.run_id)
        host = await self.sessions.host()
        reads_at: ReadsAt | None = None
        try:
            match request:
                case AgentSendRequest():
                    self.default_login_only(request)
                    media = await asyncio.to_thread(reg.RunMedia.keep, run.run_id, request.images)
                    detail = await self.sessions.send(run.run_id, request.text, request.id, tuple(reg.RunMedia.path(run.run_id, name) for name in media))
                    # A session reads nothing mid-turn: a message to one on its turn waits for that turn's end.
                    reads_at = "now" if detail == "sent" else "turn_end"
                case AgentStopRequest():
                    await host.cancel(run.run_id)
                    detail = "stopped"
                case AgentCompactRequest():
                    await host.compact(run.run_id)  # refused while its turn runs: a session compacts between turns
                    detail, reads_at = "compacting", "now"
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
        return MachineAgentAnswer(request_id=request.id, run_id=request.run_id, detail=detail, reads_at=reads_at)

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
        await asyncio.to_thread(self.runs.refuse_full, request)
        route, _, reason = await self.sessions.route()
        if route is None:
            raise PermissionError(reason)
        try:
            with self.staged(request.images) as images:
                run = await (await self.sessions.host()).fork(str(request.session_id), request.text, folder, route_id=route, model=None, media=images)
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
        self.runs.refuse_full(request)
        cwd, first, _, _ = self._read_editor(self._editor_file(request.session_id))
        root, where = self._place(cwd)
        if not root:
            raise PermissionError("this conversation's folder is not one this computer opens to agents")
        folder = self.folder(root, where)
        run = reg.AgentRun(run_id=str(uuid4()), provider="claude", name="Editor conversation", task=redact(first, self.secrets)[:500] or request.text[:500],
                           cwd=str(folder), project=reg.project_for(str(folder)), permission_mode=self.permission, session_id=self.session, started_at=time.time(), status="running")
        reg.save_run(run)
        self.runs.add(WebRun(run_id=UUID(run.run_id), root=root, path=where, kind="continued", source=request.session_id))
        self._continue_turn(run, request.text, request_id=request.id, fork_from=str(request.session_id), media=reg.RunMedia.keep(run.run_id, request.images))
        return MachineAgentAnswer(request_id=request.id, run_id=UUID(run.run_id))

    def _continue_turn(self, run: reg.AgentRun, text: str, *, request_id: UUID, fork_from: str | None = None, media: tuple[str, ...] = ()) -> MachineAgentAnswer:
        """One turn of a continued conversation: the opt-in is checked on EVERY turn (turning it off
        stops the copies too), and one turn at a time (checked and started under the run's lock). The images it shows
        (`media`, the run's own) are named to it by their files: its turn takes its message on the command line."""
        self._require_continue()
        with _TURN_LOCKS.setdefault(run.run_id, threading.Lock()):
            if reg.message_for(run.run_id, str(request_id)) is not None:  # the same request again: its turn opened already
                return MachineAgentAnswer(request_id=request_id, run_id=UUID(run.run_id), detail="sent", reads_at="now")
            current = reg.get_run(run.run_id) or run
            if fork_from is None and reg.trees(frozenset({run.run_id}))[0].status in {"running", "waiting"}:
                raise PermissionError("it is still answering; send this once it has finished")
            reg.record_message_event(from_run=reg.PERSON_SENDER, to_run=run.run_id, text=text, event_id=str(request_id), media=media)
            said = reg.RunMedia.mentioned(text, (reg.RunMedia.path(run.run_id, name) for name in media))
            try:
                launch_editor_turn(PROVIDERS["claude"], current, said, environment=self.environment_in(Path(current.cwd)), fork_from=fork_from)
            except BaseException:
                reg.finish(run.run_id, exit_code=1, first=reg.raw_line_count(run.run_id))  # never left "running" by a turn that did not start
                raise
        return MachineAgentAnswer(request_id=request_id, run_id=UUID(run.run_id), detail="sent", reads_at="now")

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
