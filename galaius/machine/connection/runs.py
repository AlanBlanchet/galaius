"""Trait: agent runs started for the server and the environment they get."""

import asyncio
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from galaius_core import AgentRevisionRef, MachineAccelerator, MachineRuntime
from galaius.agents.catalog.entries import AgentCatalog
from galaius.agents import providers as agent_providers
from galaius.agents.transport.events import AgentEvent
from galaius.agents.run import run_agent
from galaius.agents import registry as reg
from galaius.agents.catalog.profiles import ALLOWED_ENV
from galaius.isolation.fence import FenceSpec, available
if sys.platform == "win32":
    import win32api
import logging

from galaius.machine.connection.config import MachineConfig
from galaius.machine.connection.logs import FORWARDED_EVENT_KINDS

logger = logging.getLogger(__package__)


class Runs:
    """How the runner starts an agent run and what that run sees of this machine."""

    async def _run_agent(self, agent: AgentRevisionRef, task: str, config: MachineConfig, socket, command_id: UUID) -> str:
        safe_environment = self._safe_environment()
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

    @classmethod
    def _safe_environment(cls) -> dict[str, str]:
        return {**{key: value for key, value in os.environ.items() if key in ALLOWED_ENV}, **cls._system_environment()}

    @staticmethod
    def _system_environment() -> dict[str, str]:
        """What any program needs to start here and nothing else (no secret among them), PATH reaching
        every agent program's own folder; the programs installed from the web run with only this."""
        fixed = {"HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                 # Where a program opens the person's browser (a sign-in) on a Linux desktop.
                 "DISPLAY", "WAYLAND_DISPLAY", "BROWSER",
                 # Windows: what any program needs to start there (Python needs SYSTEMROOT, cmd COMSPEC), no secret among them.
                 "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "USERNAME", "USERDOMAIN", "APPDATA", "LOCALAPPDATA",
                 "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS", "PSMODULEPATH"}
        system = {key: value for key, value in os.environ.items() if key in fixed or key.startswith("LC_")}
        return {**system, "PATH": agent_providers.AgentProvider.search_path(system.get("PATH", os.defpath))}

    @staticmethod
    def _runtimes(config: MachineConfig) -> list[dict[str, str]]:
        """The agent CLIs the server may place agent steps on: none while agent steps are off here."""
        if not config.run_agents:
            return []
        return [MachineRuntime(provider=provider.name).model_dump(mode="json", exclude_none=True) for provider in agent_providers.program_providers() if provider.available()]

    @staticmethod
    def _resources(working_directory: Path | None = None) -> dict[str, int]:
        """CPU count, total RAM and free disk on the machine's own working directory — the other
        half of a placement fit check next to `_accelerators`. POSIX `sysconf`, Windows
        `GlobalMemoryStatusEx` (no `psutil` dependency, no torch import); `cpu_count() or 1`
        matches the stdlib's own documented fallback."""
        ram_mb = 0
        try:
            total = win32api.GlobalMemoryStatusEx()["TotalPhys"] if sys.platform == "win32" else os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            ram_mb = total // (1024 * 1024)
        except (ValueError, OSError, AttributeError):
            pass
        try:
            disk_free_gb = shutil.disk_usage(working_directory or Path.home()).free // (1024 ** 3)
        except OSError:
            disk_free_gb = 0
        return {"cpu_count": os.cpu_count() or 1, "ram_mb": ram_mb, "disk_free_gb": disk_free_gb}

    @classmethod
    def _accelerators(cls) -> list[dict[str, object]]:
        """GPUs on this machine, so a `model` step (detection, segmentation) defaults here.

        Torch-free: `nvidia-smi` is queried directly and Apple's unified GPU is inferred from the
        platform, never by importing torch just to ask "is there a GPU" and dragging its multi-GB
        wheel onto a machine that runs no vision node. `none` is reported explicitly (not an empty
        list) so a runner that checked and found nothing is distinguishable from an older runner
        that never reported accelerators at all."""
        found = cls._cuda_accelerators() or cls._mps_accelerator()
        accelerators = found or (MachineAccelerator(kind="none", name="none", memory_mb=0),)
        return [item.model_dump(mode="json") for item in accelerators]

    @staticmethod
    def _cuda_accelerators() -> tuple[MachineAccelerator, ...]:
        found = shutil.which("nvidia-smi")
        if found is None:
            return ()
        try:
            completed = subprocess.run(
                [found, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        if completed.returncode != 0:
            return ()
        accelerators = []
        for line in completed.stdout.splitlines():
            name, _, memory = line.partition(",")
            name, memory = name.strip(), memory.strip()
            if name and memory.isdigit():
                accelerators.append(MachineAccelerator(kind="cuda", name=name, memory_mb=int(memory)))
        return tuple(accelerators)

    @staticmethod
    def _mps_accelerator() -> tuple[MachineAccelerator, ...]:
        if sys.platform != "darwin" or platform.machine() != "arm64":
            return ()
        return (MachineAccelerator(kind="mps", name="Apple GPU", memory_mb=0),)
