"""Trait: agent runs started for the server, fenced and given the environment `inventory` allows."""

import asyncio
import os
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

from galaius.machine.connection import inventory
from galaius.machine.connection.config import MachineConfig
from galaius.machine.connection.logs import FORWARDED_EVENT_KINDS
from galaius.machine.connection.shared import Shared

logger = logging.getLogger(__package__)


class Runs(Shared):
    """How the runner starts an agent run and what that run sees of this machine."""

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
