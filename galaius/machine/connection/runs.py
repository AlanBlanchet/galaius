"""Trait: agent runs started for the server, fenced and given the environment `inventory` allows."""

import asyncio
import os
import time
import traceback
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from galaius_core import AgentRevisionRef
from galaius.agents.catalog.entries import AgentCatalog
from galaius.agents import providers as agent_providers
from galaius.agents.transport.events import AgentEvent
from galaius.agents.run import run_agent
from galaius.agents import registry as reg
from galaius.isolation.fence import FenceSpec, available
import logging

from galaius.machine.connection import inventory, logs, problems
from galaius.machine.connection.config import MachineConfig
from galaius.machine.connection.logs import FORWARDED_EVENT_KINDS
from galaius.machine.connection.events import Events
from galaius.machine.connection.store import MachineFile
from galaius_core.identity.sealing import LoginSeal
from galaius.machine.agents import MachineAgents
from galaius.machine.agents import MachineSessions
from galaius.machine.projects.workspaces import MachineWorkspaces
from galaius_core.identity.sealing import SecretsSeal
from galaius.machine.web_runs import WebRuns
from galaius.machine.projects.workspace_copy import WorkspaceTransfers
from galaius.agents.turns.followup import FollowUps
from galaius.agents.limits.warm import WarmStart
from galaius.machine.projects.workspaces import WorkspaceJobs

logger = logging.getLogger(__package__)


class Runs(Events, MachineFile):
    """How the runner starts an agent run and what that run sees of this machine."""

    #: How long the beats reuse whether a run started for the web still works (`busy`): read from every run record
    #: (~0.2 s for ~5,000), at each beat it took a tenth of a core (2026-11-03). The server stops an idle cloud
    #: machine only after 15 idle minutes.
    busy_runs_seconds = 30
    #: The sessions this runner hosts for the web (opened on first use).
    _sessions: MachineSessions | None
    #: Why the web runs could not be read at the last beat (`busy`), None when they could.
    _runs_unreadable: str | None
    #: (monotonic time, whether a run started for the web worked then): `busy`, renewed every `busy_runs_seconds`.
    _runs_working: tuple[float, bool] | None
    #: Where a workspace's jobs are kept, and the agent child started ahead of the next web start (`WarmStart`) and of
    #: the next turn of the runs whose turn ended last (`FollowUps`).
    _workspace_jobs: WorkspaceJobs
    warm: WarmStart
    followups: FollowUps

    async def _run_agent(self, agent: AgentRevisionRef, task: str, config: MachineConfig, socket, command_id: UUID) -> str:
        safe_environment = inventory.safe_environment()
        original_environment = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(safe_environment)
            handle = await run_agent(
                None,
                task,
                agent_ref=AgentCatalog.reference(agent.id, agent.revision),
                cwd=str(config.working_directory),
                permission_mode=config.permission_ceiling,
                fence=await asyncio.to_thread(self._agent_fence, config),
            )
            logger.info("agent run %s started", handle.run_id, extra={"machine_id": config.machine_id, "command_id": command_id})
            await self._event(socket, command_id, "progress", {"kind": "log", "level": "info", "logger": __package__, "text": f"agent run {handle.run_id} started", "agent_run_id": handle.run_id})
            observed = 0

            async def forward(events: list[AgentEvent]) -> None:
                """Every event the server's step view reads, as it happens: what the agent said, each
                tool call WITH its arguments and — paired by `tool_id` — what the tool returned, and
                each event's token / cost figures, whose sum is the run's total."""
                nonlocal observed
                for event in events[observed:]:
                    observed += 1
                    if event.kind not in FORWARDED_EVENT_KINDS:
                        continue
                    payload = {"kind": event.kind, "text": event.text[:2000], "tool": event.tool, "tool_input": event.tool_input[:2000], "tool_id": event.tool_id,
                               "usage": event.usage.model_dump(mode="json", exclude_none=True) or None, "cost_usd": event.cost_usd}
                    await self._event(socket, command_id, "progress", {key: value for key, value in payload.items() if value not in (None, "")})

            while handle.process.returncode is None:
                await forward(reg.read_events(handle.run_id))
                await asyncio.sleep(0.3)
            exit_code = await handle.wait()
            events = reg.read_events(handle.run_id)
            # The loop exits before the final events land (the terminal "done" carries the totals).
            await forward(events)
            result = self._final_text(events)
            if exit_code != 0:
                raise RuntimeError(result or f"agent exited {exit_code}")
            if not result:
                raise RuntimeError("agent completed without a text result")
            return result
        finally:
            os.environ.clear()
            os.environ.update(original_environment)

    def _agent_fence(self, config: MachineConfig) -> FenceSpec | None:
        """What a workflow's agent step is fenced by (its levels, starting in the working directory,
        which shows only the folders they open), None when its owner left the fence off."""
        if not config.fence_agents:
            return None
        ready, reason = available()
        if not ready:
            raise PermissionError(f"the agent fence is on here but cannot be built: {reason}; its owner switches it off there with `galaius machine fence off`")
        places = config.place_map()
        spec = FenceSpec.for_clis(agent_providers.program_providers(), egress=(urlsplit(config.server_url).hostname or "",), working_directory=config.working_directory,
                                  levels_file=self.config_path, levels=places.levels, internal=places.internal, start=config.working_directory,
                                  state=self.reviews.root.parent / "fence-state" / str(uuid4()), reviews=self.reviews.root)
        spec.build()
        return spec

    @staticmethod
    def _final_text(events: list[AgentEvent]) -> str:
        terminal = next((event for event in reversed(events) if event.kind == "done" and event.final_text and event.text.strip()), None)
        if terminal is not None:
            return terminal.text
        last = next((event for event in reversed(events) if event.kind == "text" and event.text.strip()), None)
        return "" if last is None else last.text

    @property
    def web_runs(self) -> WebRuns:
        """The agent runs this computer started for the web (`machine-agent-runs.json` beside the machine file)."""
        return WebRuns.in_folder(self.config_path.parent)

    def _agents(self, current: MachineConfig) -> MachineAgents:
        """What answers the web's agent requests, from the owner's current settings `current`."""
        self._sessions = self._sessions or MachineSessions(current.working_directory)
        return MachineAgents(roots=current.agent_roots_by_name(), permission=current.agent_permission, run_agents=current.run_agents,
                             continue_conversations=current.continue_conversations, answer_approvals=current.answer_approvals, session=MachineAgents.web_session(current.machine_id),
                             runs=self.web_runs, environment=inventory.safe_environment(),
                             sessions=self._sessions, logs=self._log_ring, seal=SecretsSeal.for_token(current.token.get_secret_value()), login_seal=LoginSeal.for_token(current.token.get_secret_value()),
                             workspaces=MachineWorkspaces(roots=current.agent_roots_by_name(), origins=current.clone_origins, jobs=self._workspace_jobs,
                                                          working_directory=current.working_directory, register_root=self._register_agent_root,
                                                          transfers=WorkspaceTransfers(endpoint=current.endpoint, headers=current.authorization),
                                                          environment={**inventory.safe_environment(), **{key: os.environ[key] for key in ("SSH_AUTH_SOCK",) if key in os.environ}}),
                             places=current.place_map(), fence_agents=current.fence_agents, reviews=self.reviews,
                             levels_file=self.config_path, egress=(urlsplit(current.server_url).hostname or "",), warm=self.warm, followups=self.followups, audit=self.audit,
                             programs=self.programs)

    def _register_agent_root(self, name: str) -> bool:
        """Adds `name` (relative to the working directory) to the agent roots - an existing checkout
        of a repository the owner allowed; whether it is usable there (never over a file or script
        root, never hidden, never galaius's own folders)."""
        after = self.update(lambda current: current if name in current.agent_roots else current.model_copy(update={"agent_roots": (*current.agent_roots, name)}))
        usable = name in after.agent_roots_by_name()
        if not usable:
            self.update(lambda current: current.model_copy(update={"agent_roots": tuple(root for root in current.agent_roots if root != name)}))
        logger.info("existing checkout %s %s as an agent folder", name, "registered" if usable else "refused")
        return usable

    def busy(self) -> bool:
        """Work of its own is going on here: a step, a web session's turn, a workspace being
        prepared, or an agent run started for the web still working (as read within `busy_runs_seconds`). Said in every beat (`busy`):
        a machine the server stops when idle (a cloud machine) is not stopped under it."""
        if self._executing or (self._sessions is not None and self._sessions.busy) or MachineWorkspaces.busy():
            return True
        if (seen := self._runs_working) is not None and time.monotonic() - seen[0] < self.busy_runs_seconds:
            return seen[1]
        try:
            working, unreadable = self.web_runs.working(), None
        except (OSError, ValueError) as error:
            working, unreadable = False, str(error)
        self._runs_working = time.monotonic(), working
        if unreadable != self._runs_unreadable:  # said once per change, never on every beat
            if unreadable is not None:
                logger.warning("web runs unreadable; reported not busy: %s", unreadable)
            self._runs_unreadable = unreadable
        return working

    def _report_fault(self, config: MachineConfig, what: str, said: str, error: BaseException) -> None:
        """An agent action this computer failed by its own fault: an error report kept here, its owner asked on the PC's
        page whether to send it (`MachineErrorReports.prepare`: nothing leaves before his yes) — this runner's recent log
        and the traceback. Off the event loop (its question is an HTTPS call); never raises."""
        lines = (*self._log_ring.lines, *"".join(traceback.format_exception(error)).splitlines())
        drafts, secrets = problems.drafts(), logs.secrets(config, self._spent_tokens)
        asyncio.get_running_loop().run_in_executor(None, lambda: drafts.prepare(config, "exception", f"{what} failed: {said}", lines, secrets))
