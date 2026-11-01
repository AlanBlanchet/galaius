"""The owner's own editor conversations on this PC, one kind per agent CLI (`EditorConversations`): those the web offers him
to continue, and a COPY of one continued here (the original is never written). `MachineAgents` asks every kind in its
program's order (`EditorConversations.every`); a CLI with editor conversations is one subclass naming its provider."""

from __future__ import annotations

import asyncio
import json
import logging
import stat
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID, uuid4

from galaius_core import AgentContinueRequest, AgentRefused, MachineAgentAnswer, MachineAgentSession

from galaius.agents import registry as reg
from galaius.agents.host import ConversationRefused
from galaius.agents.providers import AgentProvider, ClaudeCodeProvider, CodexProvider, program_providers
from galaius.agents.run import LAUNCH_STAMP
from galaius.diagnostics.redaction import redact
from galaius.machine.web_runs import WebRun

if TYPE_CHECKING:
    from galaius.machine.agents import MachineAgents

logger = logging.getLogger(__name__)

#: Editor conversations offered for continuing: written in the last two weeks, newest first.
EDITOR_SESSIONS_DAYS, EDITOR_SESSIONS_MAX = 14, 40
#: An editor conversation written this recently is open in the editor right now.
EDITOR_LIVE_SECONDS = 120


class EditorConversations(ABC):
    """One CLI's own editor conversations on this PC, as `agents` (the PC's current settings) offers them: those `listed`
    (inside an agent root, never one galaius started), a copy of one `continued` here, and the history a continued copy
    opens with."""

    #: The CLI whose conversations these are.
    provider: ClassVar[type[AgentProvider]]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if any(kind.provider is cls.provider for kind in EditorConversations.__subclasses__() if kind is not cls):
            raise TypeError(f"{cls.__name__}: {cls.provider.name} already has its editor conversations")

    def __init__(self, agents: MachineAgents) -> None:
        self.agents = agents

    @classmethod
    def every(cls, agents: MachineAgents) -> tuple[EditorConversations, ...]:
        """Each CLI's kind, in its program's order (`program_providers`)."""
        kinds = {kind.provider.name: kind for kind in cls.__subclasses__()}
        return tuple(kinds[provider.name](agents) for provider in program_providers() if provider.name in kinds)

    def session(self, session_id: UUID, root: str, path: str, updated_at: float, title: str, last: str = "") -> MachineAgentSession:
        """One of them as the web lists it, its words redacted."""
        secrets = self.agents.secrets
        return MachineAgentSession(session_id=session_id, provider=self.provider.name, title=redact(title, secrets)[:400], last=redact(last, secrets)[:400],
                                   root=root, path=path, updated_at=updated_at, live=time.time() - updated_at < EDITOR_LIVE_SECONDS)

    @abstractmethod
    async def listed(self) -> tuple[MachineAgentSession, ...]:
        """Those offered here, newest first."""

    @abstractmethod
    async def continued(self, request: AgentContinueRequest) -> MachineAgentAnswer | None:
        """A copy of `request.session_id` continued here with its text; None: no conversation of this CLI's offered here."""

    def history(self, session_id: UUID) -> tuple[str, ...]:
        """The conversation a copy continues as stream lines its transcript reads (what he typed, what it answered), the
        copy opening where the editor left off; none when the copy carries its history itself."""
        return ()


class ClaudeEditor(EditorConversations):
    """Claude Code's transcript files (`MachineAgents.editor_projects`); a copy is Claude's own fork, in the
    conversation's folder read HERE from its file."""

    provider = ClaudeCodeProvider

    async def listed(self) -> tuple[MachineAgentSession, ...]:
        return await asyncio.to_thread(self._listed)

    async def continued(self, request: AgentContinueRequest) -> MachineAgentAnswer | None:
        return await asyncio.to_thread(self._continued, request)

    def history(self, session_id: UUID, keep: int = 40) -> tuple[str, ...]:
        try:
            path = self._file(session_id)
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
            text = redact(self.said_text(message.get("content")).strip(), self.agents.secrets)
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

    def _files(self, session_id: UUID) -> list[Path]:
        return [path for path in self.agents.editor_projects.glob(f"*/{session_id}.jsonl") if path.is_file() and not path.is_symlink()]

    def _file(self, session_id: UUID, found: list[Path] | None = None) -> Path:
        found = self._files(session_id) if found is None else found
        if len(found) != 1:
            raise PermissionError("this conversation is not on this computer")
        return found[0]

    @staticmethod
    def said_text(value: object) -> str:
        """The plain text of a Claude transcript message's `content` (a string, or text blocks)."""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(str(block.get("text") or "") for block in value if isinstance(block, dict) and block.get("type") == "text")
        return ""

    def _read(self, path: Path) -> tuple[str, str, str, str]:
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
            text = self.said_text(message.get("content")) if value.get("type") == "user" and not value.get("isMeta") else ""
            if not first and text.strip() and not text.lstrip().startswith("<"):
                first = text.strip()
            if cwd and first and titled:
                break
        for line in reversed(tail.splitlines()):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if value.get("type") == "assistant" and (text := self.said_text((value.get("message") or {}).get("content")).strip()):
                last = text
                break
        return cwd, titled or first, first, last

    def _listed(self) -> tuple[MachineAgentSession, ...]:
        """Its conversations written in the last two weeks whose folder lies inside an agent root, newest first; none that
        galaius itself started."""
        since = time.time() - EDITOR_SESSIONS_DAYS * 86400
        files = []
        for path in self.agents.editor_projects.glob("*/*.jsonl"):
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
            cwd, title, first, last = self._read(path)
            root, where = self.agents.place(cwd)
            if not root or first.startswith(LAUNCH_STAMP):
                continue
            found.append(self.session(session_id, root, where, updated, title, last))
            if len(found) >= EDITOR_SESSIONS_MAX:
                break
        return tuple(found)

    def _continued(self, request: AgentContinueRequest) -> MachineAgentAnswer | None:
        """A COPY of the editor conversation continues here with its text (Claude's own fork: the editor's conversation is
        never written), in the conversation's own folder, and only when it lies inside an agent root."""
        if not (found := self._files(request.session_id)):
            return None
        agents = self.agents
        agents.runs.refuse_full(request)
        cwd, first, _, _ = self._read(self._file(request.session_id, found))
        root, where = agents.place(cwd)
        if not root:
            raise AgentRefused("folder_closed", "this conversation's folder is not one this computer opens to agents")
        folder = agents.folder(root, where)
        run = reg.AgentRun(run_id=str(uuid4()), provider=self.provider.name, name="Editor conversation", task=redact(first, agents.secrets)[:500] or request.text[:500],
                           cwd=str(folder), project=reg.project_for(str(folder)), permission_mode=agents.permission, session_id=agents.session, started_at=time.time(), status="running")
        reg.save_run(run)
        agents.runs.add(WebRun(run_id=UUID(run.run_id), root=root, path=where, kind="continued", source=request.session_id))
        agents.continue_turn(run, request.text, request_id=request.id, fork_from=str(request.session_id), media=reg.RunMedia.keep(run.run_id, request.images))
        return MachineAgentAnswer(request_id=request.id, run_id=UUID(run.run_id))


class CodexEditor(EditorConversations):
    """Codex's threads (its app server, `MachineAgents.sessions`). Continuing one opens a session (a copy that asks before
    acting, `thread/fork`), so both opt-ins apply; without them, or with Codex unavailable here, there are none."""

    provider = CodexProvider

    async def listed(self) -> tuple[MachineAgentSession, ...]:
        agents = self.agents
        if not (agents.continue_conversations and agents.answer_approvals and agents.sessions is not None):
            return ()
        route, _, _ = await agents.sessions.route()
        if route is None:
            return ()
        try:
            rows = await (await agents.sessions.host()).threads(route)
        except (ConversationRefused, ConnectionError, ValueError) as error:
            logger.warning("codex conversations unavailable: %s", error)
            return ()
        launched = await asyncio.to_thread(reg.session_ids)
        found = []
        for row in rows:
            root, where = agents.place(str(row.get("cwd") or ""))
            try:
                thread_id = UUID(str(row["id"]))
            except ValueError:
                continue
            if not root or row.get("originator") == "galaius" or str(thread_id) in launched:
                continue
            found.append(self.session(thread_id, root, where, float(row.get("updatedAt") or 0), str(row.get("preview") or "")))
        return tuple(found)

    async def continued(self, request: AgentContinueRequest) -> MachineAgentAnswer | None:
        """It keeps the conversation's history, asks before acting like any session, and the original is never written.
        Its folder is read HERE from Codex's own record, inside an agent root."""
        found = next((item for item in await self.listed() if item.session_id == request.session_id), None)
        if found is None:
            return None
        agents = self.agents
        assert agents.sessions is not None
        folder = await asyncio.to_thread(agents.folder, found.root, found.path)
        await asyncio.to_thread(agents.runs.refuse_full, request)
        route, _, reason = await agents.sessions.route()
        if route is None:
            raise PermissionError(reason)
        try:
            with agents.staged(request.images) as images:
                run = await (await agents.sessions.host()).fork(str(request.session_id), request.text, folder, route_id=route, model=None, media=images)
        except ConversationRefused as error:
            raise RuntimeError(str(error)) from error
        await asyncio.to_thread(agents.runs.add, WebRun(run_id=UUID(run.run_id), root=found.root, path=found.path, kind="session"))
        return MachineAgentAnswer(request_id=request.id, run_id=UUID(run.run_id))
