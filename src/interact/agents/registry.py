"""On-disk record of every agent run: what is running, who started it, what it cost.

Cross-process by design — the CLI spawns, the VS Code extension reads, another shell stops. The
default is ``~/.interact/out/agents``; disposable test and harness processes pass
``INTERACT_AGENTS_DIR`` to every participant so they share an isolated registry. It is never
``debug_dir``-relative: two processes disagreeing about where to look is exactly how metering
broke before (0ef5fa4), and ``server_registry`` pins its own path for the same reason.

Status is DERIVED from the pid on every read. A record cannot assert it is still running — a
killed agent reports as crashed instead of spinning forever in the UI.
"""

import json
import os
import re
import secrets
import signal
import stat
import sys
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import BaseModel, Field, PrivateAttr
from interact.agents import quota
from interact_core import AgentRevisionRef, PromptExecutionRef

from interact.agents.events import TOKEN_FIELDS, AgentEvent, TokenUsage, UsageLedger
from interact.agents.catalog_connection import CatalogConnection
from interact.agents.providers import PROVIDERS, DeniedTool
from interact.fence import FenceSpec
from interact.file_lock import exclusive
from interact.models import TokenMix
from interact.pinned_directory import PinnedDirectory
from interact.private_files import PRIVATE_FILES
from interact.processes import end_process_tree, process_started
from interact.server_registry import (
    _alive,  # generic pid liveness (Windows-safe, no signal sent)
)

RunStatus = Literal[
    "starting", "running", "waiting", "done", "failed", "cancelled", "crashed", "stopped",
    "foreign",
]
RunKind = Literal["process", "conversation", "provider_child"]
ConnectionMode = Literal["local_session", "api"]
ChargePath = Literal[
    "subscription_quota", "usage_credit", "metered_api", "local_compute", "unknown"
]
CostCertainty = Literal["known", "unknown"]
AggregateChargePath = ChargePath | Literal["mixed"]
ConversationCapability = Literal[
    "streaming", "resume", "cancel", "approvals", "collaboration"
]
_RUN_ID = re.compile(r"^[A-Za-z0-9._:@+-]{1,160}$")
_SESSION_ID: ContextVar[str | None] = ContextVar("agent_session_id", default=None)
#: Why a ranked candidate was passed over. Every value but one is a fact about this machine or
#: the caller's request, checked BEFORE anything ran: a denial or failure once the child could
#: act is otherwise a run outcome, recorded on that run, never a reason to try the next
#: candidate. "quota_exceeded" is the one exception: a provider refusing "you've reached your
#: <model> limit, switch to another model" is unavailable for THIS candidate the same as a
#: missing CLI, discovered moments after the child starts rather than before — so it falls
#: through too, never relaxing the criterion (#181).
SkipReason = Literal[
    "cli_missing", "provider_off", "unauthenticated", "auth_check_failed", "permission_mode_unsupported",
    "images_unsupported", "tool_policy_unsupported", "model_capability_unsupported",
    "quota_exceeded",
]


class LaunchCandidate(BaseModel):
    """One (provider, model) pair from the ranked list a launch walks in order.

    ``rank`` is the position of the catalog row; two providers able to run the same row share it,
    so the record shows which CLI was preferred for one model and which model over another.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    provider: str = Field(min_length=1)
    #: What the CLI is asked for — bare id for a native model, `provider/id` for a routed one.
    model: str = Field(min_length=1)
    #: `<catalog provider>/<id>`; None when a plain id outside the catalog is passed through.
    catalog_id: str | None = None
    rank: int = Field(ge=0)


class SkippedCandidate(BaseModel):
    """A candidate the launcher did not start.

    ``reason`` stays a stable machine code. ``message`` is the sentence shown to a person; old
    records may have no message and remain readable.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    candidate: LaunchCandidate
    reason: SkipReason
    message: str = ""
    #: When a `quota_exceeded` candidate may be tried again (epoch seconds); None for every other
    #: reason, and on records written before this field.
    until: float | None = None


class AgentRun(BaseModel):
    """One supervised run.

    ``run_id`` is Interact's owned registry identity. ``provider_session_id`` is the vendor
    identity used for continuation; Claude lets us make those equal, Codex does not.
    """

    run_id: str
    kind: RunKind = "process"
    provider: str
    name: str
    task: str = ""
    cwd: str = ""
    #: The repo/package the run belongs to — the grouping unit, derived from `cwd` at registration
    #: so it survives even if the directory is later moved or deleted.
    project: str = ""
    pid: int | None = None
    #: When `pid`'s process started (`process_started`), recorded with it: a pid the system has since
    #: given to another process is not this run's (Windows reuses pids within minutes). None when
    #: unknown (a record from before it was kept, or a system without start times).
    pid_started: int | None = None
    lifecycle_token: str | None = Field(
        default_factory=lambda: secrets.token_hex(16), min_length=1, max_length=80,
    )
    model: str | None = None
    #: The DEFINITION this run is — a name the provider resolves to a file holding its system
    #: prompt and tool set (Claude Code: ``~/.claude/agents/<agent>.md``). None for a plain run.
    #: Without it a run knows its label but not what it actually IS, so nothing can link to it.
    agent: str | None = None
    agent_ref: AgentRevisionRef | None = None
    #: Where that definition's system prompt actually lives, resolved at registration. The name
    #: alone is answerable only by a caller that can import this module and ask the provider —
    #: and the panel reads these records straight off disk, so a name it can't resolve is a link
    #: it can't offer. Recorded, not derived on read, so it survives the file being moved later.
    definition_path: str | None = None
    #: How much autonomy this run was GIVEN, when the choice was made explicitly. Recorded because
    #: it answers "why did that one stop to ask" / "why did that one just do it" — a question
    #: about a run you're watching that nothing else on the record answers. None means nobody
    #: chose, so the CLI's own configured default applied.
    permission_mode: str | None = None
    #: Only an explicitly recorded opt-in may add the launcher mesh on resume.
    mesh_enabled: bool = False
    #: What the OS fence of every turn is built from, at that turn, from the levels as they are then
    #: (`interact.fence`); None: unfenced. Held here, in the runner's store, never inside a fence.
    fence: FenceSpec | None = None
    parent_run_id: str | None = None
    root_run_id: str | None = None
    #: Owning caller conversation, independent of run genealogy and the child's vendor session.
    session_id: str | None = Field(default=None, min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:@+-]+$")
    spawned_by_event_id: str | None = None
    provider_session_id: str | None = None
    provider_turn_id: str | None = None
    connection: ConnectionMode | None = None
    requested_model: str | None = None
    requested_criterion: str | None = None
    reasoning: str | None = None
    #: A policy resolved while this session was already active. The vendor queue keeps the
    #: current session settings, so this is deliberately separate from the effective fields above.
    pending_model: str | None = None
    pending_criterion: str | None = None
    pending_reasoning: str | None = None
    #: The ranked list this launch walked and the entries passed over before the one that ran.
    #: Records written before ranking existed load as empty tuples.
    candidates: tuple[LaunchCandidate, ...] = ()
    skipped: tuple[SkippedCandidate, ...] = ()
    denied_tools: tuple[DeniedTool, ...] = ()
    prompt: PromptExecutionRef | None = None
    cataloged_at: float | None = None
    charge_path: ChargePath = "unknown"
    cost_certainty: CostCertainty = "unknown"
    capabilities: list[ConversationCapability] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    started_at: float = 0.0
    finished_at: float | None = None
    exit_code: int | None = None
    #: True for a session interact did NOT spawn — the user's own editor windows, surfaced so the
    #: supervisor shows the machine's real state rather than only its own children.
    foreign: bool = False

    status: RunStatus = "running"
    #: API-equivalent value of observed usage. The actual account impact is represented separately
    #: by ``charge_path`` and ``cost_certainty`` and remains unknown without provider evidence.
    cost_usd: float | None = None
    #: Cumulative token use — how much CONTEXT this run has consumed, which is invisible from a
    #: cost figure alone (two models at the same price consume very differently). Same meaning
    #: as :class:`interact.agents.events.TokenUsage`; always the sum over the run's events.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    last: str = ""

    def process_running(self) -> bool:
        """Its recorded process still runs and is still that process, not a later one given its pid."""
        return bool(self.pid) and _alive(self.pid) and (
            self.pid_started is None or process_started(self.pid) == self.pid_started
        )

    def handoff_header(self) -> str:
        """Registry-owned origin for an agent message or returned report."""
        identity = self.model_dump(mode="json", include={
            "run_id", "agent", "provider", "model", "reasoning", "requested_criterion",
            "cataloged_at",
        })
        identity["benchmark_evidence"] = "not recorded in this run; criterion is a selection rule, not a score"
        return "[Interact agent provenance]\n" + json.dumps(identity, sort_keys=True) + "\n[Agent content]\n"

    @classmethod
    def from_foreign(cls, raw: dict) -> "AgentRun | None":
        """One of the user's OWN sessions, as reported by a provider's discovery.

        The class owns its own construction because this shape was being built in two places —
        the listing and the CLI's `agents discovered`, which the VS Code panel parses — with the
        same nine fields, the same id fallback and the same millisecond division duplicated. A
        field added here would reach one and not the other, and the panel would never notice the
        difference.

        None when the payload carries no session id: nothing to address it by, so it's not a run
        we can show, resume or stop.
        """
        sid = raw.get("sessionId") or raw.get("id")
        if not sid:
            return None
        cwd = raw.get("cwd") or ""
        return cls(
            run_id=sid,
            provider="claude",
            name=raw.get("name") or sid[:8],
            cwd=cwd,
            # Filed like every registered run is. Without this a foreign session had a working
            # directory and no project — visible in the flat list, invisible to the workspace
            # switcher, which groups by project. That is exactly "i have agents in the sheets
            # folder elsewhere, and i can't change and see how they work".
            project=project_for(cwd),
            pid=raw.get("pid"),
            lifecycle_token=None,
            started_at=(raw.get("startedAt") or 0) / 1000.0,
            last=raw.get("kind") or "",
            status="foreign",
            foreign=True,
        )


_FIXTURE_MODELS = {"openai/example-model", "fixture/local", "fixture-model"}


def _reject_unisolated_fixture_record(raw: dict) -> None:
    """Keep known harness identities out of the user's default run store.

    Checked at the one write point, so a model a fake provider reports AFTER launch is caught too.
    """
    if os.environ.get("INTERACT_AGENTS_DIR"):
        return
    if str(raw.get("provider") or "").startswith("fixture") or raw.get("model") in _FIXTURE_MODELS:
        raise ValueError("fixture agent records require an isolated INTERACT_AGENTS_DIR")


#: Markers that make a directory the root of a PROJECT. Grouping on the working directory's own
#: name splits one repo across several groups the moment an agent runs in a subfolder — `src` and
#: `tests` show up as separate projects. The repo root is the unit people mean by "project".
#: A REPOSITORY boundary. Checked first and on its own, because a repo is the unit people mean by
#: "project": this repo's `vscode-extension/` has its own package.json, and treating that as a root
#: filed one repo's agents under two different projects — the "project detection is folder level"
#: complaint, still true after the first fix because a manifest counted equally with a VCS root.
_REPO_MARKERS = (".git", ".hg", ".svn")
#: A package boundary — only meaningful when nothing above it is a repo at all (a loose tool in a
#: directory nobody version-controls still deserves its own name rather than falling back to $HOME).
_PACKAGE_MARKERS = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod")


def project_for(cwd: str) -> str:
    """The project a working directory belongs to — the enclosing REPOSITORY, by name.

    A repo root wins over any package manifest inside it, so a sub-package (this repo's own
    ``vscode-extension/`` and its package.json) files under the repo rather than as a project of
    its own. Only when nothing above is version-controlled does a package manifest count, so a
    loose tool still gets its own name instead of falling back to a home directory.

    Empty for an unknown directory — a wrong project label is worse than none, because it
    silently merges unrelated work.
    """
    if not cwd:
        return ""
    try:
        here = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        return ""
    chain = (here, *here.parents)
    for markers in (_REPO_MARKERS, _PACKAGE_MARKERS):
        for candidate in chain:
            if any((candidate / marker).exists() for marker in markers):
                return candidate.name
    return here.name


def agents_dir() -> Path:
    """Shared registry path; harnesses set the override before launching every participant."""
    override = os.environ.get("INTERACT_AGENTS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".interact" / "out" / "agents"


def _ensure_registry_directory() -> Path:
    """Create and repair the private directory chain without changing the user's home mode."""
    directory = agents_dir()
    home = Path.home()
    try:
        relative = directory.relative_to(home)
    except ValueError:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        _keep_private(directory)
    else:
        current = home
        for part in relative.parts:
            current /= part
            current.mkdir(mode=0o700, exist_ok=True)
            _keep_private(current)
    return directory


def _keep_private(directory: Path) -> None:
    """`directory` made private to this user when it is not (POSIX: mode 0700; Windows: a protected
    DACL naming only this user, which its files inherit). Refused when it is a link or junction:
    making it private would change whatever it points at."""
    info = directory.lstat()
    if PinnedDirectory.link_like(info) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"unsafe agent registry directory: {directory}")
    try:
        PRIVATE_FILES.check(directory)
    except PermissionError:
        PRIVATE_FILES.restrict(directory)


def _open_private(path: Path, flags: int, *, create: bool = True) -> int:
    directory = _ensure_registry_directory()
    if path.parent != directory:
        raise OSError("agent registry file escaped its private directory")
    with _registry_directory() as folder:
        descriptor = folder.file(path.name, flags | (os.O_CREAT if create else 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("agent registry path is not a regular file")
        folder.chmod(descriptor, 0o600)
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _registry_directory():
    """The registry folder pinned from the user home: no link on the way, the home included."""
    directory = _ensure_registry_directory()
    home = Path.home()
    try:
        relative = directory.relative_to(home)
    except ValueError as error:
        raise OSError("agent registry directory must be anchored beneath the user home") from error
    return PinnedDirectory.open(home, *relative.parts)


def _read_private(path: Path) -> bytes | None:
    try:
        descriptor = _open_private(path, os.O_RDONLY, create=False)
    except OSError:
        return None
    try:
        stream = os.fdopen(descriptor, "rb")
    except OSError:
        os.close(descriptor)
        raise
    with stream:
        return stream.read()


def _private_mtime(path: Path) -> float | None:
    descriptor = _open_private(path, os.O_RDONLY, create=False)
    try:
        return os.fstat(descriptor).st_mtime
    finally:
        os.close(descriptor)


def _append_private(path: Path, payload: bytes) -> None:
    descriptor = _open_private(path, os.O_APPEND | os.O_WRONLY)
    try:
        if os.write(descriptor, payload) != len(payload):
            raise OSError("short agent-registry append")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_leaf_matches(name: str, folder: PinnedDirectory, identity: tuple[int, int]) -> bool:
    """Check identity already visible before publication, without promising atomic same-UID
    defence for the final check-to-rename window. Directory mode 0700 excludes other OS users;
    another process under the same account remains inside this bounded trust boundary."""
    try:
        comparison = folder.file(name)
    except OSError:
        return False
    matches = False
    try:
        opened = os.fstat(comparison)
        matches = stat.S_ISREG(opened.st_mode) and (
            opened.st_dev, opened.st_ino
        ) == identity
    except OSError:
        pass
    try:
        os.close(comparison)
    except OSError:
        return False
    return matches


def _replace_private(path: Path, payload: bytes) -> None:
    if path.suffix == ".json":
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError:
            raw = None
        if isinstance(raw, dict) and "run_id" in raw:
            _reject_unisolated_fixture_record(raw)
    directory = _ensure_registry_directory()
    if path.parent != directory:
        raise OSError("agent registry file escaped its private directory")
    with _registry_directory() as folder:
        _replace_in(folder, path.name, payload)


def _replace_in(folder: PinnedDirectory, name: str, payload: bytes) -> None:
    descriptor = -1
    replacement_name: str | None = None
    owned_identity: tuple[int, int] | None = None
    try:
        candidate_name = f".{name}.{secrets.token_hex(8)}.new"
        descriptor = folder.file(candidate_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        replacement_name = candidate_name
        created = os.fstat(descriptor)
        owned_identity = (created.st_dev, created.st_ino)
        folder.chmod(descriptor, 0o600)
        if os.write(descriptor, payload) != len(payload):
            raise OSError("short agent-registry write")
        os.fsync(descriptor)
        written = os.fstat(descriptor)
        owned_identity = (written.st_dev, written.st_ino)
        # Closed before the rename: Windows refuses to rename a file this process holds open.
        closing, descriptor = descriptor, -1
        os.close(closing)
        if not _private_leaf_matches(replacement_name, folder, owned_identity):
            raise OSError("agent registry replacement identity changed before publication")
        folder.replace(replacement_name, folder, name)
    finally:
        primary_error = sys.exception()
        cleanup_error: OSError | None = None
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as error:
                cleanup_error = error
        if replacement_name is not None and owned_identity is not None and _private_leaf_matches(
            replacement_name, folder, owned_identity,
        ):
            try:
                folder.unlink(replacement_name)
            except FileNotFoundError:
                pass
            except OSError as error:
                cleanup_error = cleanup_error or error
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


def _safe_run_id(run_id: str) -> str:
    if run_id in (".", "..") or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("invalid agent run id")
    return run_id


def _record_path(run_id: str) -> Path:
    return agents_dir() / f"{_safe_run_id(run_id)}.json"


def messages_path(run_id: str) -> Path:
    """Messages live BESIDE the vendor stream, never inside it. The stream is the vendor's own
    file and the mirror is rewritten from it, so anything appended there is clobbered on the next
    read — a message must outlive that."""
    return agents_dir() / f"{_safe_run_id(run_id)}.messages.jsonl"


def events_path(run_id: str) -> Path:
    return agents_dir() / f"{_safe_run_id(run_id)}.jsonl"


def raw_events_path(run_id: str) -> Path:
    """Where the child writes its OWN stream, verbatim. Owned by the OS, not by any interact
    process — so the record of what an agent did survives interact restarting or dying."""
    return agents_dir() / f"{_safe_run_id(run_id)}.raw.jsonl"


def lock_path(run_id: str) -> Path:
    """The cross-process lock for one run's record and delivery state."""
    return agents_dir() / f"{_safe_run_id(run_id)}.lock"


@contextmanager
def record_lock(run_id: str):
    """Serialize registry writers across CLI, MCP, dispatcher and provider processes."""
    with exclusive(_open_private(lock_path(run_id), os.O_RDWR)):
        yield


def stderr_path(run_id: str) -> Path:
    """Private bounded-diagnostic source beside a run's raw provider stream."""
    return agents_dir() / f"{_safe_run_id(run_id)}.stderr"


def open_stderr(run_id: str, *, append: bool) -> BinaryIO:
    """Open private stderr capture; readers must clip it before showing it to a person."""
    flags = os.O_APPEND if append else os.O_TRUNC
    descriptor = _open_private(stderr_path(run_id), flags | os.O_WRONLY)
    return os.fdopen(descriptor, "ab" if append else "wb")


def read_stderr(run_id: str, limit: int = 2000) -> str:
    """Read only a bounded, private diagnostic tail."""
    try:
        payload = _read_private(stderr_path(run_id)) or b""
    except OSError:
        return ""
    return payload[-max(0, limit):].decode(errors="replace")


def open_raw_events(run_id: str, *, append: bool) -> BinaryIO:
    """Open a provider-owned stream while preserving registry privacy under any umask."""
    flags = os.O_APPEND if append else os.O_TRUNC
    descriptor = _open_private(raw_events_path(run_id), flags | os.O_WRONLY)
    return os.fdopen(descriptor, "ab" if append else "wb")


def _terminate(pid: int) -> bool:
    """Stop a run's whole process tree, best effort. Agent CLIs spawn children (their own tools),
    so ending the tree is what actually stops the work."""
    return end_process_tree(pid)


#: Where each agent CLI tells a command it runs which conversation it is in: a run launched from
#: that shell (a script, a background command) belongs to that conversation, so the reader can show
#: the prompt it answered (runs launched by a script from Claude Code recorded no owner at all).
CALLER_THREAD_ENV = ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID")


def resolve_session_id(session_id: str | None = None, *, parent_run_id: str | None = None, cli_harness: bool = False):
    """Resolve only explicit caller context or a recorded parent's owner, never cwd or PID.

    Vendor thread environment is read only for an explicit CLI caller: an MCP server may
    be shared. This is a listing scope, not a new authorization boundary.
    """
    parent_id = parent_run_id or os.environ.get("INTERACT_PARENT_RUN_ID")
    parent = _read_record(parent_id) if parent_id else None
    supplied = session_id if session_id is not None else _SESSION_ID.get()
    if supplied is not None:
        supplied = _safe_run_id(supplied)
    if parent is not None and parent.session_id is not None:
        if supplied is not None and supplied != parent.session_id:
            raise ValueError("Session identity conflicts with the recorded parent conversation")
        return parent.session_id
    if supplied is None:
        inherited = os.environ.get("INTERACT_SESSION_ID")
        supplied = _safe_run_id(inherited) if inherited is not None else None
    # A child's vendor thread cannot establish its unknown parent's owning conversation.
    if supplied is None and cli_harness and parent_id is None:
        supplied = next((os.environ[name] for name in CALLER_THREAD_ENV if os.environ.get(name)), None)
        if supplied is not None:
            supplied = _safe_run_id(supplied)
    return supplied


@contextmanager
def session_context(session_id: str | None = None, *, parent_run_id: str | None = None, cli_harness: bool = False):
    """Bind one asynchronous spawn to its caller without mutating process-global environment."""
    identity = resolve_session_id(session_id, parent_run_id=parent_run_id, cli_harness=cli_harness)
    token = _SESSION_ID.set(identity)
    try:
        yield identity
    finally:
        _SESSION_ID.reset(token)


def session_runs(*, session_id: str | None = None, all_sessions: bool = False, include_foreign: bool = False):
    """Launched runs in one conversation; machine-wide inventory requires explicit opt-in."""
    if all_sessions:
        if session_id is not None:
            raise ValueError("Choose session_id or all_sessions, not both")
        return list_runs(include_foreign=include_foreign)
    identity = resolve_session_id(session_id)
    if identity is None:
        raise ValueError("Session identity required: pass session_id / --session-id or set INTERACT_SESSION_ID; use all_sessions / --all-sessions explicitly for all runs")
    # Filter before deriving status: viewing one session must not refresh unrelated histories.
    return list_runs(session_id=identity)


def _process_identity(pid: int | None) -> dict:
    """A run's process as recorded: its pid and when that process started (see `AgentRun.pid_started`)."""
    return {"pid": pid, "pid_started": process_started(pid) if pid else None}


def register(*, run_id: str, pid: int | None, provider: str, name: str, task: str = "",
             cwd: str = "", model: str | None = None, parent_run_id: str | None = None,
             agent: str | None = None, permission_mode: str | None = None,
             mesh_enabled: bool = False,
             requested_criterion: str | None = None, reasoning: str | None = None,
             provider_session_id: str | None = None,
             agent_ref: AgentRevisionRef | None = None,
             definition_path: Path | None = None, session_id: str | None = None,
             candidates: tuple[LaunchCandidate, ...] = (),
             skipped: tuple[SkippedCandidate, ...] = (),
             denied_tools: tuple[DeniedTool, ...] = (), fence: FenceSpec | None = None) -> AgentRun:
    provider_impl = PROVIDERS.get(provider)
    definition = definition_path
    if definition is None and agent_ref is None:
        definition = provider_impl.definition_path(agent) if (provider_impl and agent) else None
    run = AgentRun(run_id=run_id, **_process_identity(pid), provider=provider, name=name, task=task, cwd=cwd,
                   project=project_for(cwd), model=model, parent_run_id=parent_run_id,
                   session_id=resolve_session_id(session_id, parent_run_id=parent_run_id),
                   agent=agent, agent_ref=agent_ref, definition_path=str(definition) if definition else None,
                   permission_mode=permission_mode, requested_criterion=requested_criterion,
                   mesh_enabled=mesh_enabled,
                   reasoning=reasoning,
                   candidates=candidates, skipped=skipped, denied_tools=denied_tools, fence=fence,
                   provider_session_id=provider_session_id,
                   lifecycle_token=secrets.token_hex(16),
                   started_at=time.time())
    _write(run)
    try:
        descriptor = _open_private(
            raw_events_path(run_id), os.O_APPEND | os.O_WRONLY, create=False
        )
    except FileNotFoundError:
        pass
    else:
        os.close(descriptor)
    return run


def finish(
    run_id: str, *, exit_code: int | None, expected_pid: int | None = None,
    expected_lifecycle_token: str | None = None,
) -> bool:
    """Record how a run ended, only if this caller still owns its recorded process.

    Also where a LATE quota/rate-limit refusal is caught — the one the post-spawn probe
    (`run.py::_quota_probe`) cannot see because the run had already been working for minutes.
    Such a refusal arrives only in the run's own event stream; the live mirror polls it through
    `read_events`'s REPARSE path (`_mirror_while_alive`), which never calls `append_event` (that
    function's own quota catch fires only for the interactive local_session protocol, unreachable
    from an ordinary `-p`/`exec` process run) — so nothing ever recorded a cooldown for it, and the
    next dispatch picked the same cooled model and died again (#reopened). `finish` is the one
    place both a fresh spawn (`run.py::_reap`) and a RESUMED turn (`run.py::_reap_continuation`,
    `agent_queue.dispatch`) converge on exactly once per real termination, so checking here covers
    both without a second poller and without double-recording a run already finished.

    Events are read BEFORE the lock: `read_events` takes `record_lock` itself (session/turn-id
    backfill), and nesting the same advisory flock from this function's own `with` block below
    would deadlock — a single-fd flock is not reentrant, even within one process.
    """
    events = read_events(run_id)
    terminal = events[-1] if events else None
    with record_lock(run_id):
        stored = _read_record(run_id)
        if stored is None:
            return False
        if expected_lifecycle_token is not None and stored.lifecycle_token is None:
            return False
        if expected_pid is not None and expected_lifecycle_token is None:
            return False
        if expected_pid is not None and stored.pid != expected_pid:
            return False
        if (expected_lifecycle_token is not None
                and stored.lifecycle_token != expected_lifecycle_token):
            return False
        if (terminal is not None and terminal.kind == "error" and terminal.text
                and quota.REFUSAL.search(terminal.text)):
            quota.record_refusal(stored.provider, stored.model, said=terminal.text)
        finished_at = time.time()
        # A stop is the owner's decision and stays the ending: the killed process then exits with
        # SIGTERM's code (-15, or 143 through a shell) and its reaper must not turn it "failed".
        if stored.status == "stopped":
            exit_code = stored.exit_code
        status = (
            "stopped" if exit_code in (-signal.SIGTERM, 128 + signal.SIGTERM)
            else "done" if exit_code == 0
            else "failed" if exit_code is not None
            else _status_for(stored)
        )
        if status == "done" and (stored.output_tokens or 0) > 0:
            quota.served(stored.provider, stored.model)  # it answered: nothing holds against it
        finished = _merge_record_locked(run_id, {
            "finished_at": stored.finished_at if stored.status == "stopped" and stored.finished_at else finished_at,
            "exit_code": exit_code,
            "status": status,
        })
    if finished is None:
        return False
    # Fold the run's usage into its record the moment it ends. Outside the lock: `_derive`
    # re-reads the stream and merges through `_update_fields`, each taking `record_lock` itself.
    _derive(finished)
    return True


def fail_turn_locked(
    run_id: str, reason: str, *, exit_code: int = 1, now: float | None = None,
) -> AgentRun | None:
    """Record that a RESUMED turn of `run_id` failed before, or without, its provider saying so,
    while the caller owns ``record_lock(run_id)``.

    An `error` event carrying `reason` lands in the run's messages file — the one side channel the
    raw-stream re-parse keeps (`_read_messages`) — anchored at the end of the stream, and the
    record reads failed (status, exit code, end time, `last`), so every reader (the panel, a PC's
    run list for the web) sees the failure instead of the ended run the message found. A refused
    resume used to fail only its queue item: the run stayed "done" and the web showed the message
    as delivered forever.
    """
    if _read_record(run_id) is None:
        return None
    moment = time.time() if now is None else now
    event = AgentEvent(kind="error", event_id=secrets.token_hex(16), text=reason, at=moment,
                       raw_index=raw_line_count(run_id))
    _append_private(messages_path(run_id), (event.model_dump_json() + "\n").encode())
    return _merge_record_locked(run_id, {
        "status": "failed", "exit_code": exit_code, "finished_at": moment,
        "last": event.summary(viewer=run_id),
    })


def fail_turn(run_id: str, reason: str, *, exit_code: int = 1, now: float | None = None) -> AgentRun | None:
    """:func:`fail_turn_locked` for a caller not holding the run's lock."""
    with record_lock(run_id):
        return fail_turn_locked(run_id, reason, exit_code=exit_code, now=now)


def stop(run_id: str, *, expected_lifecycle_token: str | None = None) -> bool:
    """Ask a run to stop. Returns False for an unknown run rather than raising."""
    with record_lock(run_id):
        stored = _read_record(run_id)
        if stored is None:
            return False
        if stored.lifecycle_token is None:
            return False
        token = (
            expected_lifecycle_token
            if expected_lifecycle_token is not None
            else stored.lifecycle_token
        )
        if token != stored.lifecycle_token:
            return False
        # Cancel first. A failed queue write must not leave a stopped run resumable with old work.
        try:
            from interact.agents import agent_queue
            agent_queue.cancel_pending_locked(run_id)
        except (ImportError, OSError, ValueError, RuntimeError):
            return False
        if stored.process_running():
            _terminate(stored.pid)
        updated = _merge_record_locked(run_id, {
            "finished_at": time.time(),
            "exit_code": -signal.SIGTERM,
            "status": "stopped",
        })
        return updated is not None


def _read_record(run_id: str) -> AgentRun | None:
    try:
        payload = _read_private(_record_path(run_id))
        if payload is None:
            return None
        run = AgentRun.model_validate_json(payload)
    except (OSError, ValueError):
        return None
    if run.run_id != run_id:
        return None
    return _backfill_definition(run)


def _backfill_definition(run: AgentRun) -> AgentRun:
    """Offer legacy local links without consulting today's server roster for historical runs."""
    if run.definition_path is not None or not run.agent or run.agent_ref is not None:
        return run
    # A retired role may be absent from today's catalog. Listing history is a local
    # read, and must neither fetch current instructions nor substitute them for history.
    if CatalogConnection.path().exists():
        return run
    provider = PROVIDERS.get(run.provider)
    resolved = provider.definition_path(run.agent) if provider is not None else None
    if resolved is None:
        return run
    run.definition_path = str(resolved)
    return run


def _merge_record_locked(run_id: str, updates: dict) -> AgentRun | None:
    """Merge owned fields into the current JSON record, retaining unknown future fields."""
    payload = _read_private(_record_path(run_id))
    if payload is None:
        return None
    try:
        raw = json.loads(payload)
        if not isinstance(raw, dict) or raw.get("run_id") != run_id:
            return None
        merged = {**raw, **updates}
        validated = AgentRun.model_validate(merged)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    _replace_private(_record_path(run_id), json.dumps(merged, ensure_ascii=False).encode())
    return validated


def _update_fields(
    run_id: str, updates: dict, *, expected: dict | None = None,
) -> AgentRun | None:
    """Atomically merge only fields owned by one writer.

    ``expected`` lets a derived read avoid publishing a stale value after a lifecycle writer has
    changed the same field. Unknown JSON keys are copied through unchanged for forward readers.
    """
    with record_lock(run_id):
        payload = _read_private(_record_path(run_id))
        if payload is None:
            return None
        try:
            raw = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("run_id") != run_id:
            return None
        if expected and any(raw.get(key) != value for key, value in expected.items()):
            return AgentRun.model_validate(raw)
        return _merge_record_locked(run_id, updates)


def _write(run: AgentRun) -> None:
    """Write a complete caller-owned snapshot, preserving unknown fields on disk."""
    with record_lock(run.run_id):
        payload = _read_private(_record_path(run.run_id))
        if payload is None:
            _replace_private(_record_path(run.run_id), run.model_dump_json().encode())
            return
        try:
            raw = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
        merged = {**raw, **run.model_dump(mode="json")}
        _replace_private(_record_path(run.run_id), json.dumps(merged, ensure_ascii=False).encode())


def save_run(run: AgentRun) -> None:
    """Persist a typed run after its owning lifecycle changed it."""
    _write(run)


def begin_turn(
    run_id: str, *, pid: int, model: str | None = None,
    requested_criterion: str | None = None, reasoning: str | None = None,
) -> AgentRun | None:
    """Move one stopped run back to active state for a newly spawned provider turn."""
    updates = {
        **_process_identity(pid), "lifecycle_token": secrets.token_hex(16),
        "exit_code": None, "finished_at": None, "status": "running",
        "pending_model": None, "pending_criterion": None, "pending_reasoning": None,
    }
    if model is not None:
        updates["model"] = model
    if requested_criterion is not None:
        updates["requested_criterion"] = requested_criterion
    if reasoning is not None:
        updates["reasoning"] = reasoning
    return _update_fields(run_id, updates)


def begin_turn_locked(
    run_id: str, *, pid: int, model: str | None = None,
    requested_criterion: str | None = None, reasoning: str | None = None,
) -> AgentRun | None:
    """Same transition for a caller already holding ``record_lock(run_id)``."""
    updates = {
        **_process_identity(pid), "lifecycle_token": secrets.token_hex(16),
        "exit_code": None, "finished_at": None, "status": "running",
        "pending_model": None, "pending_criterion": None, "pending_reasoning": None,
    }
    if model is not None:
        updates["model"] = model
    if requested_criterion is not None:
        updates["requested_criterion"] = requested_criterion
    if reasoning is not None:
        updates["reasoning"] = reasoning
    return _merge_record_locked(run_id, updates)


def record_pending_policy(
    run_id: str, *, model: str | None, criterion: str | None, reasoning: str | None,
) -> AgentRun | None:
    """Record a fresh policy without pretending an already-running vendor applied it."""
    return _update_fields(run_id, {
        "pending_model": model,
        "pending_criterion": criterion,
        "pending_reasoning": reasoning,
    })


def get_run(run_id: str) -> AgentRun | None:
    """Read one canonical run record without deriving the whole registry."""
    return _read_record(run_id)


def append_event(run_id: str, event: AgentEvent) -> None:
    """Persist an event AND fold it into the run record.

    The record has to carry the running totals, not just the event log: the VS Code panel reads
    these files directly and cannot replay a whole JSONL per row, so a cost or activity line
    computed only on the Python read path would show up there as blank and free. Folding here
    keeps it O(1) per event and makes the record authoritative for every reader.
    """
    with record_lock(run_id):
        stored = _read_record(run_id)
        if stored is None:
            return
        if event.kind in ("error", "rate_limit") and event.text \
                and quota.REFUSAL.search(event.text):
            # The vendor can take longer to answer "you've reached your limit" than the launch
            # waits, so this is where a late refusal is heard at all. Remembering it here is what
            # stops the NEXT launch spending a child on the same dead model.
            #
            # The `rate_limit` line counts as well as the `error` one, and is the more useful of
            # the two: it is the line that NAMES the exhausted window and when it reopens, while
            # the error that follows it a moment later says only "switch to another model". The
            # error can no longer shorten what the rate-limit line already wrote (`record_refusal`
            # never lowers a live block), so hearing both is strictly better than hearing one.
            quota.record_refusal(stored.provider, stored.model, said=event.text)
        elif event.kind in ("text", "tool", "thinking") and (event.input_tokens or 0) > 0:
            # The MODEL answering — a turn that read tokens; the CLI's own notice reads none:
            # whatever an earlier refusal left against it is over, whatever reset it named. An
            # `allowed` rate-limit line proves nothing: one pool can allow while another refuses.
            quota.served(stored.provider, stored.model)
        terminal = stored.status in ("done", "failed", "cancelled", "crashed", "stopped")
        same_turn = event.turn_id is None or event.turn_id == stored.provider_turn_id
        newer_root_turn = (
            stored.kind == "conversation" and event.kind in ("started", "prompt")
            and event.turn_id is not None and event.turn_id != stored.provider_turn_id
        )
        if stored.kind == "conversation" and event.turn_id is not None and not same_turn \
                and not newer_root_turn:
            return
        if terminal and same_turn:
            return

        _append_private(events_path(run_id), (event.model_dump_json() + "\n").encode())
        updates: dict[str, object] = {}
        if event.cost_usd is not None:
            updates["cost_usd"] = (stored.cost_usd or 0.0) + event.cost_usd
        if event.kind == "tool" and event.tool is not None and event.tool not in stored.tools:
            updates["tools"] = [*stored.tools, event.tool]
        for field in TOKEN_FIELDS:
            used = getattr(event, field)
            if used is not None:
                updates[field] = (getattr(stored, field) or 0) + used
        summary = event.summary(viewer=run_id)
        if summary:  # system/hook events summarise to nothing; they must not blank the row
            updates["last"] = summary
        if stored.kind == "conversation":
            if newer_root_turn:
                updates.update(provider_turn_id=event.turn_id, status="running", finished_at=None)
            if event.kind == "done":
                updates.update(status="done", finished_at=event.at or time.time())
            elif event.kind == "cancelled":
                updates.update(status="cancelled", finished_at=event.at or time.time())
            elif event.kind == "error":
                updates.update(status="failed", finished_at=event.at or time.time())
            elif event.kind == "interaction":
                updates["status"] = "waiting"
            elif not terminal and event.kind in ("started", "prompt", "text", "thinking", "tool"):
                updates["status"] = "running"
        elif stored.kind == "provider_child":
            was_terminal = stored.status in ("done", "failed", "cancelled", "crashed", "stopped")
            if event.kind == "done":
                updates["status"] = "done"
            elif event.kind == "cancelled":
                updates["status"] = "cancelled"
            elif event.kind == "error":
                updates["status"] = "failed"
            elif event.kind == "interaction" and not was_terminal:
                updates["status"] = "waiting"
            elif not was_terminal and event.kind in ("started", "prompt", "text", "thinking", "tool"):
                updates["status"] = "running"
            if event.kind in ("done", "error", "cancelled") and stored.finished_at is None:
                updates["finished_at"] = event.at or time.time()
        _merge_record_locked(run_id, updates)


class _ConversationProjector(BaseModel):
    """The conversation event cursor, persistence, and snapshot boundary."""

    _seen: dict[str, set[str]] = PrivateAttr(default_factory=dict)
    _sequences: dict[str, int] = PrivateAttr(default_factory=dict)

    def apply_child(self, child: AgentRun, event: AgentEvent) -> AgentRun | None:
        parent_run_id = child.parent_run_id
        root_run_id = child.root_run_id
        spawned_by_event_id = child.spawned_by_event_id
        if parent_run_id is None or root_run_id is None or spawned_by_event_id is None:
            raise ValueError("provider child metadata is incomplete")
        upsert_provider_child(
            run_id=child.run_id,
            provider=child.provider,
            parent_run_id=parent_run_id,
            root_run_id=root_run_id,
            spawned_by_event_id=spawned_by_event_id,
            cwd=child.cwd,
            task=child.task or None,
            requested_model=child.requested_model,
            status=child.status,
            started_at=child.started_at,
            finished_at=child.finished_at,
        )
        return self.apply(parent_run_id, event)

    def apply(self, run_id: str, event: AgentEvent) -> AgentRun | None:
        if run_id not in self._seen:
            persisted = read_events(run_id)
            self._seen[run_id] = {item.event_id for item in persisted if item.event_id}
            self._sequences[run_id] = max(
                (item.sequence or 0 for item in persisted), default=0
            )
        if event.event_id and event.event_id in self._seen[run_id]:
            return None
        self._sequences[run_id] += 1
        event.sequence = self._sequences[run_id]
        if event.event_id:
            self._seen[run_id].add(event.event_id)
        if event.at is None:
            event.at = time.time()
        if event.kind == "text" and ":delta:" in event.event_id:
            return get_run(run_id)
        append_event(run_id, event)
        return get_run(run_id)


class _BillingSummary(BaseModel):
    charge_path: AggregateChargePath
    paths: list[ChargePath]
    cost_certainty: CostCertainty


def billing_summary(runs: list[AgentRun]) -> _BillingSummary:
    paths: list[ChargePath] = list(dict.fromkeys(run.charge_path for run in runs))
    aggregate: AggregateChargePath = paths[0] if len(paths) == 1 else "mixed"
    certainty: CostCertainty = (
        "known" if paths and all(run.cost_certainty == "known" for run in runs) else "unknown"
    )
    return _BillingSummary(charge_path=aggregate, paths=paths, cost_certainty=certainty)


def upsert_provider_child(
    *, run_id: str, provider: str, parent_run_id: str, root_run_id: str,
    spawned_by_event_id: str, cwd: str, task: str | None, requested_model: str | None,
    status: RunStatus, started_at: float | None = None, finished_at: float | None = None,
) -> AgentRun:
    """Create or enrich one provider-native child without replay or order regressions."""
    with record_lock(run_id):
        run = _read_record(run_id)
        if run is not None and (
            run.kind != "provider_child"
            or run.provider != provider
            or run.root_run_id != root_run_id
            or run.parent_run_id != parent_run_id
        ):
            raise ValueError("provider child identity collision")
        if run is None:
            observed_at = time.time()
            parent = _read_record(parent_run_id)
            root = _read_record(root_run_id)
            run = AgentRun(
                run_id=run_id,
                kind="provider_child",
                provider=provider,
                name="subagent",
                task=task or "",
                cwd=cwd,
                project=project_for(cwd),
                pid=None,
                model=requested_model,
                parent_run_id=parent_run_id,
                root_run_id=root_run_id,
                session_id=parent.session_id if parent is not None and parent.session_id is not None else root.session_id if root is not None else None,
                spawned_by_event_id=spawned_by_event_id,
                provider_session_id=run_id,
                connection="local_session",
                requested_model=requested_model,
                charge_path="unknown",
                cost_certainty="unknown",
                capabilities=["collaboration"],
                started_at=started_at if started_at is not None else observed_at,
                finished_at=finished_at,
                status=status,
            )
            _replace_private(_record_path(run_id), run.model_dump_json().encode())
        rank = {"starting": 0, "running": 1, "waiting": 2, "cancelled": 3,
                "done": 4, "failed": 4, "crashed": 4, "stopped": 4, "foreign": 4}
        updates: dict[str, object] = {}
        if task:
            updates["task"] = task
        if requested_model:
            updates.update(requested_model=requested_model, model=requested_model)
        if status in ("starting", "running") or run.spawned_by_event_id is None:
            updates["spawned_by_event_id"] = spawned_by_event_id
        if rank[status] >= rank[run.status]:
            updates["status"] = status
        if started_at is not None:
            updates["started_at"] = min(run.started_at, started_at)
        if finished_at is not None:
            updates["finished_at"] = finished_at
        elif status in ("done", "failed", "cancelled") and run.finished_at is None:
            updates["finished_at"] = time.time()
        current = _merge_record_locked(run_id, updates)
        return current or run


def _record_message_event_locked(
    *, from_run: str, to_run: str, text: str, event_id: str | None = None,
) -> str | None:
    if _read_record(to_run) is None:
        return None
    message_id = event_id or secrets.token_hex(16)
    for side in (from_run, to_run):
        event = AgentEvent(kind="message", event_id=message_id, text=text,
                           from_run=from_run, to_run=to_run, at=time.time(),
                           raw_index=raw_line_count(side))
        _append_private(messages_path(side), (event.model_dump_json() + "\n").encode())
        if _read_record(side) is not None:
            _merge_record_locked(side, {"last": event.summary(viewer=side)})
    return message_id


def record_message_event(
    *, from_run: str, to_run: str, text: str, event_id: str | None = None,
) -> str | None:
    """Record one exchange and return its stable id for delivery metadata."""
    with ExitStack() as stack:
        for side in sorted({from_run, to_run}):
            stack.enter_context(record_lock(side))
        return _record_message_event_locked(
            from_run=from_run, to_run=to_run, text=text, event_id=event_id,
        )


def record_message_event_locked(
    *, from_run: str, to_run: str, text: str, event_id: str | None = None,
) -> str | None:
    """Record an exchange while the caller owns every needed run lock."""
    return _record_message_event_locked(
        from_run=from_run, to_run=to_run, text=text, event_id=event_id,
    )


def record_message(*, from_run: str, to_run: str, text: str) -> bool:
    """Record one agent addressing another, on BOTH transcripts.

    Written to each side because an exchange visible to only one of them is not an exchange: the
    sender's history should show what it asked for, and the recipient's should show the request
    directly above whatever it did next. Returns False for an unknown recipient rather than
    writing a message nobody can receive.
    """
    return record_message_event(from_run=from_run, to_run=to_run, text=text) is not None


def raw_line_count(run_id: str) -> int:
    """How many lines the vendor stream holds right now — where a message lands in it."""
    try:
        payload = _read_private(raw_events_path(run_id))
        return 0 if payload is None else len(payload.splitlines())
    except OSError:
        return 0


def message_for(run_id: str, message_id: str) -> AgentEvent | None:
    """Find one recipient-side message without copying its text into queue metadata."""
    return next((event for event in _read_messages(run_id) if event.event_id == message_id), None)


def _interleave(parsed: list[AgentEvent], messages: list[AgentEvent],
                total_raw: int) -> list[AgentEvent]:
    """Both streams in the order they happened.

    The vendor writes no timestamps, so a message carries the raw-line count at the moment it was
    sent: it belongs before every parsed event that came from a later line. Appending messages at
    the end instead showed each question after the answer it prompted.
    """
    def anchor(event: AgentEvent) -> int:
        # An event with no anchor is placed at the END: we do not know when it happened, and
        # reading "unknown" as position zero put every such message above the run's first turn.
        return event.raw_index if event.raw_index is not None else total_raw

    out: list[AgentEvent] = []
    pending = sorted(messages, key=anchor)
    for event in parsed:
        while pending and anchor(pending[0]) <= anchor(event):
            out.append(pending.pop(0))
        out.append(event)
    return out + pending


def stream_signature(run_id: str) -> tuple:
    """What a run's normalised events are built from: its raw stream and its messages. Equal
    signatures mean an identical rebuild, so a poller can skip the re-parse."""
    def _stat(path: Path):
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    return _stat(raw_events_path(run_id)), _stat(messages_path(run_id))


def _read_messages(run_id: str) -> list[AgentEvent]:
    try:
        payload = _read_private(messages_path(run_id))
        lines = [] if payload is None else payload.decode().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(AgentEvent.model_validate_json(line))
        except ValueError:
            continue
    return out


def messages() -> list[AgentEvent]:
    """Every recorded exchange, in order — the edges a sequence or graph view draws. Deduped,
    since each message is stored on both sides."""
    seen: set[tuple] = set()
    out: list[AgentEvent] = []
    for run in list_runs():
        for event in read_events(run.run_id):
            if event.kind != "message":
                continue
            key = (event.from_run, event.to_run, event.text)
            if key in seen:
                continue
            seen.add(key)
            out.append(event)
    return out


def read_events(run_id: str) -> list[AgentEvent]:
    """Every event of a run, normalised.

    Reads the RAW vendor stream the child wrote itself and translates it through that provider's
    parser. A corrupt or half-written line is skipped, never fatal — a truncated write during a
    crash must not make the whole run unreadable.
    """
    stored = _read_record(run_id)
    provider = PROVIDERS.get(stored.provider) if stored else None
    try:
        payload = _read_private(raw_events_path(run_id))
        raw_lines = [] if payload is None else payload.decode(errors="replace").splitlines()
    except OSError:
        raw_lines = []

    # A raw stream is AUTHORITATIVE when present: the child wrote it itself, so it cannot be out
    # of date. The mirror below is derived from it, and re-reading both would double-count.
    if provider is not None and raw_lines:
        parsed: list[AgentEvent] = []
        ledger = UsageLedger()
        for index, line in enumerate(raw_lines):
            try:
                event = provider.parse(line, ledger)
            except (KeyError, TypeError, ValueError):
                continue
            if event is not None:
                parsed.append(event.model_copy(update={"raw_index": index}))
        if stored is not None:
            session_id = next((event.session_id for event in parsed if event.session_id), None)
            turn_id = next((event.turn_id for event in reversed(parsed) if event.turn_id), None)
            changed = False
            if session_id and stored.provider_session_id != session_id:
                stored.provider_session_id = session_id
                changed = True
            if turn_id and stored.provider_turn_id != turn_id:
                stored.provider_turn_id = turn_id
                changed = True
            if changed:
                updates = {}
                if session_id:
                    updates["provider_session_id"] = session_id
                if turn_id:
                    updates["provider_turn_id"] = turn_id
                current = _update_fields(run_id, updates)
                if current is not None:
                    stored = current
        merged = _interleave(parsed, _read_messages(run_id), len(raw_lines))
        # The mirror is what the VS Code panel reads, so it must carry the WHOLE conversation.
        # Written without messages, the chat showed the agent talking to nobody.
        _mirror_normalised(run_id, merged)
        return merged

    # No raw stream — events were appended directly (a test, or a provider that has none).
    out: list[AgentEvent] = []
    try:
        payload = _read_private(events_path(run_id))
        for line in ([] if payload is None else payload.decode().splitlines()):
            try:
                out.append(AgentEvent.model_validate_json(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out + _read_messages(run_id)


def _carry_observed_at(path: Path, events: list[AgentEvent]) -> None:
    """Stamp when each event was first seen, keeping the stamps already on record.

    The mirror is rebuilt wholesale from a re-parse on every pass, so stamping the clock of the
    moment would march every event's time forward and mean nothing. The stream is append-only, so
    an event keeps whatever time position ``i`` already carried and only genuinely new lines take
    the current clock. Carrying them forward is also what keeps the content comparison below
    stable — otherwise every pass would differ and a settled run would be rewritten forever.
    """
    seen: list[float | None] = []
    try:
        payload = _read_private(path)
        for line in ([] if payload is None else payload.decode().splitlines()):
            if line.strip():
                seen.append(json.loads(line).get("at"))
    except (OSError, ValueError):
        seen = []
    now = time.time()
    for i, event in enumerate(events):
        prior = seen[i] if i < len(seen) else None
        event.at = prior if isinstance(prior, (int, float)) else now


def _mirror_normalised(run_id: str, events: list[AgentEvent]) -> None:
    """Keep a provider-AGNOSTIC copy of the stream beside the raw one.

    The VS Code panel cannot parse a vendor dialect — teaching it every provider's JSON would
    duplicate the adapters in a second language. Translating once here means the panel reads one
    stable shape whatever produced it. Rewritten only when the CONTENT changed, so a settled run
    costs nothing to re-read — comparing the LENGTH alone was not enough, because a change to how
    events are ordered or rendered leaves the count identical, so the panel kept serving the old
    shape forever and the only symptom was the UI quietly disagreeing with the CLI.
    """
    path = events_path(run_id)
    _carry_observed_at(path, events)
    payload = "".join(e.model_dump_json() + "\n" for e in events)
    try:
        existing = _read_private(path)
        if existing is not None and existing.decode() == payload:
            return
    except OSError:
        pass
    try:
        _replace_private(path, payload.encode())
    except OSError:
        pass


def last_event(run_id: str) -> AgentEvent | None:
    events = read_events(run_id)
    return events[-1] if events else None


def _status_for(run: AgentRun) -> RunStatus:
    """The one thing a file cannot assert about itself: whether it is still running."""
    if run.kind == "provider_child":
        return run.status
    if run.kind == "conversation":
        if run.status in ("waiting", "done", "failed", "cancelled", "stopped"):
            return run.status
        if run.process_running():
            return run.status
        return "crashed"
    if run.exit_code is not None:
        return ("stopped" if run.exit_code == -signal.SIGTERM
                else "done" if run.exit_code == 0 else "failed")
    if run.process_running():
        return "running"
    # It never recorded an ending and its process is gone — it died without saying so.
    return "crashed"


def _derive(run: AgentRun) -> AgentRun:
    original = run.model_dump()
    if not run.project and run.cwd:
        run.project = project_for(run.cwd)
    """Re-check liveness, and SELF-HEAL the record from the child's own stream.

    The panel reads these records directly and cannot parse a vendor dialect, so cost and the
    activity line have to be ON the record. Recomputing them here from the raw stream means a run
    whose supervising process died still reports what it actually did, instead of looking healthy
    and empty."""
    run.status = _status_for(run)
    events = read_events(run.run_id)
    # The child's OWN stream is the authority on how it ended. If it reported a terminal event,
    # the run finished — even if nothing was watching to record an exit code. Without this, a run
    # whose supervisor died reports "crashed" while its transcript plainly says it completed.
    terminal = None
    if run.exit_code is None and events:
        terminal = next((e for e in reversed(events) if e.kind in ("done", "error")), None)
        latest_activity = events[-1]
        if latest_activity.kind not in ("done", "error"):
            terminal = None
        if terminal is not None:
            run.status = (
                "waiting" if run.kind == "conversation" and terminal.kind == "done"
                else "done" if terminal.kind == "done"
                else "failed"
            )
    # A process that dies never calls finish(), so an ended run routinely has no end TIME — and a
    # timeline cannot draw an interval without one. The last byte the child wrote is an OBSERVED
    # end: not when it died, but the last moment we know it was alive, which is honest and
    # drawable. Only ever stamped for a run that is no longer running.
    if run.finished_at is None and run.status != "running":
        try:
            run.finished_at = _private_mtime(raw_events_path(run.run_id))
        except OSError:
            pass
    if events:
        costs = [e.cost_usd for e in events if e.cost_usd is not None]
        cost = sum(costs) if costs else None
        # Tokens the same way: cost alone hides how much CONTEXT a run consumed, and two models at
        # the same price consume very differently.
        for field in TOKEN_FIELDS:
            used = [getattr(e, field) for e in events if getattr(e, field) is not None]
            if used:
                setattr(run, field, sum(used))
        last = next((e.summary(viewer=run.run_id) for e in reversed(events)
                     if e.summary(viewer=run.run_id)), run.last)
        run.cost_usd, run.last = cost, last
    # Persist whatever we healed — status included. The panel reads these files directly and does
    # its own (downgrade-only) liveness check, so a status left stale on disk reappears there as
    # "crashed" no matter what Python worked out in memory.
    owned = ("project", "status", "finished_at", "cost_usd", *TOKEN_FIELDS, "last")
    updates = {
        field: getattr(run, field) for field in owned
        if getattr(run, field) != original.get(field)
    }
    if updates:
        current = _update_fields(
            run.run_id, updates,
            expected={
                field: original.get(field)
                for field in ("pid", "exit_code", "status", "lifecycle_token")
            },
        )
        if current is not None:
            run = current
            # The OTHER half of the late-refusal gap: a run `agents spawn` detached (its own
            # `_reap` task died with the spawning CLI process) never reaches `finish()` at all —
            # exit_code stays None forever, and this self-heal is the only place its ending is
            # EVER noticed. Guarded by `_update_fields`'s compare-and-swap succeeding on a genuine
            # first transition into "failed" (`original` is the PRE-heal snapshot), so a later,
            # repeated `list_runs()` read of an already-healed run never re-extends the cooldown.
            if (terminal is not None and terminal.kind == "error" and terminal.text
                    and original.get("status") != "failed" and run.status == "failed"
                    and quota.REFUSAL.search(terminal.text)):
                quota.record_refusal(run.provider, run.model, said=terminal.text)
    return run


def _discover_foreign() -> list[dict]:
    """Sessions the providers can see that we did not spawn."""
    found: list[dict] = []
    for provider in PROVIDERS.values():
        try:
            found.extend(provider.discover())
        except (OSError, ValueError):  # one unavailable provider must not break listing
            continue
    return found


def list_runs(*, include_foreign: bool = False, session_id: str | None = None) -> list[AgentRun]:
    """Low-level lifecycle/history inventory; interactive lists use ``session_runs``.

    Every known run, newest last. With ``include_foreign``, also the sessions a provider can
    see that interact did not start — deduped against our own by id, since we deliberately reuse
    the vendor's session id as our run id."""
    if session_id is not None:
        _safe_run_id(session_id)
    d = agents_dir()
    runs: list[AgentRun] = []
    known: set[str] = set()
    if d.exists():
        for path in sorted(d.glob("*.json")):
            run = _read_record(path.stem)
            if run is None or run.run_id in known or (session_id is not None and run.session_id != session_id):
                continue  # a corrupt or aliased record must not hide every other run
            known.add(run.run_id)
            runs.append(run)
    runs = [_derive(r) for r in runs]

    if include_foreign and session_id is None:
        known = {r.run_id for r in runs}
        for raw in _discover_foreign():
            found = AgentRun.from_foreign(raw)
            if found is None or found.run_id in known:
                continue
            known.add(found.run_id)
            runs.append(found)
    return sorted(runs, key=lambda r: r.started_at)


def running_runs() -> list[AgentRun]:
    """Stored runs whose process is alive, decided from the record and its pid alone.

    `list_runs` derives every run from its transcript; a caller polling for live work must not
    pay for the whole history on each tick."""
    d = agents_dir()
    if not d.exists():
        return []
    runs = (_stat_cached_record(path) for path in sorted(d.glob("*.json")))
    return [run for run in runs if run is not None and _status_for(run) == "running"]


def trees(root_run_ids: frozenset[str]) -> list[AgentRun]:
    """The runs `root_run_ids` name and every run they launched, at any depth (parent links, or a
    provider child's recorded root), newest first, status re-checked from the record and pid alone
    (never the stream): a poller watching a few trees pays one `stat()` per record."""
    d = agents_dir()
    if not d.exists() or not root_run_ids:
        return []
    records = [run for run in (_stat_cached_record(path) for path in d.glob("*.json")) if run is not None]
    members, grew = set(root_run_ids), True
    while grew:
        grew = False
        for run in records:
            if run.run_id not in members and (run.parent_run_id in members or run.root_run_id in members):
                members.add(run.run_id)
                grew = True
    def current(run: AgentRun) -> AgentRun:
        status = _status_for(run)
        # A process gone with no ending recorded is healed from its own stream (it may have
        # finished while nothing watched it) — the only case that pays for reading the stream.
        # `_derive` compare-and-swaps against the STORED record, so it gets that record as read.
        return _derive(run) if status == "crashed" else run.model_copy(update={"status": status})
    return sorted((current(run) for run in records if run.run_id in members), key=lambda run: run.started_at, reverse=True)


def session_ids() -> frozenset[str]:
    """Every provider session interact launched or continued: its runs' ids and the vendor session
    ids they recorded (one `stat()` per record, cached)."""
    d = agents_dir()
    if not d.exists():
        return frozenset()
    records = [run for run in (_stat_cached_record(path) for path in d.glob("*.json")) if run is not None]
    return frozenset(value for run in records for value in (run.run_id, run.provider_session_id) if value)


#: path -> (mtime_ns, size, record). A settled record never changes, so a poller re-reads only
#: the files written since its last tick.
_RECORD_CACHE: dict[str, tuple[int, int, AgentRun | None]] = {}


def _stat_cached_record(path: Path) -> AgentRun | None:
    try:
        stat = path.stat()
    except OSError:
        _RECORD_CACHE.pop(str(path), None)
        return None
    hit = _RECORD_CACHE.get(str(path))
    if hit is not None and hit[:2] == (stat.st_mtime_ns, stat.st_size):
        return hit[2]
    run = _read_record(path.stem)
    _RECORD_CACHE[str(path)] = (stat.st_mtime_ns, stat.st_size, run)
    return run



def _listed_mtime(path: Path) -> float:
    """When a listed record last changed, for ORDERING only (its content is still read through the
    private-file guard); one removed meanwhile sorts last."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


#: How many of the newest run records a role's token mix is read from: its RECENT workload, at a
#: cost that stays flat however long the history grows (a full scan cost seconds per process at
#: 2 700 records).
TOKEN_MIX_WINDOW = 300


def token_mix(agent: str | None) -> TokenMix:
    """How `agent`'s recent runs on this machine spent their tokens, summed — the workload its model
    prices are read at. The default mix for no agent, or one with no counted token in the window."""
    folder = agents_dir()
    total = TokenUsage()
    if agent and folder.exists():
        newest = sorted(((_listed_mtime(path), path) for path in folder.glob("*.json")), reverse=True)
        for _mtime, path in newest[:TOKEN_MIX_WINDOW]:
            run = _stat_cached_record(path)
            if run is not None and run.agent == agent:
                total += TokenUsage(**{name: getattr(run, name) or 0 for name in TOKEN_FIELDS})
    return total.mix() or TokenMix()

def forget(run_id: str) -> bool:
    """Remove a finished run and everything it wrote. False if it is still running, or unknown.

    Nothing could remove a run before, so the panel grew forever — every agent ever spawned stayed
    listed, with its transcript and raw stream on disk. A live run is never forgotten: the record
    is the only handle on the process, so dropping it would leave an agent still working and
    invisible.
    """
    resolved = resolve_run_id(run_id)
    if resolved is None:
        return False
    run = _read_record(resolved)
    if run is None or _status_for(run) == "running":
        return False
    for path in (_record_path(resolved), events_path(resolved),
                 messages_path(resolved), raw_events_path(resolved), stderr_path(resolved),
                 agents_dir() / f"{_safe_run_id(resolved)}.queue.json"):
        try:
            path.unlink()
        except OSError:
            pass  # already gone, or not ours to delete — never fail a cleanup over one file
    return True


def clear_finished() -> list[str]:
    """Forget every run that has stopped; returns the ids removed."""
    return [r.run_id for r in list_runs() if not r.foreign and forget(r.run_id)]


def resolve_run_id(prefix: str) -> str | None:
    """The full run id a prefix names, or None if it names none or more than one.

    `interact agents list` prints 8-character ids, so an id copied off the tool's own output has
    to work everywhere an id is taken. Ambiguity resolves to None rather than to a guess: picking
    one at random would stop or message the WRONG agent.
    """
    if not prefix or not _RUN_ID.fullmatch(prefix):
        return None
    if _read_record(prefix) is not None:
        return prefix
    # A stored run's record IS its filename, so a prefix is a directory lookup. Deriving every run
    # instead re-parses every transcript — minutes at a real history's size. Only a prefix naming no
    # stored record (a foreign session interact did not start) needs the full listing.
    stored = {path.stem for path in agents_dir().glob(f"{prefix}*.json")
              if _RUN_ID.fullmatch(path.stem) and _read_record(path.stem) is not None}
    if prefix in stored:
        return prefix
    if stored:
        return next(iter(stored)) if len(stored) == 1 else None
    ids = [r.run_id for r in list_runs(include_foreign=True)]
    if prefix in ids:
        return prefix  # an exact id is never ambiguous, even if it prefixes another
    matches = [rid for rid in ids if rid.startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


def children_of(run_id: str) -> list[AgentRun]:
    """The runs a given run spawned — the cross-provider team tree."""
    return [r for r in list_runs() if r.parent_run_id == run_id]
