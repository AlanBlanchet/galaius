"""One capped systemd slice for every agent interact starts, one capped scope per run inside it.

Agents fan out: each can start MCP servers, browsers, parallel test workers and dev servers, and
several agents run at once. Uncapped, they share the editor's cgroup, so when memory runs out the
OOM killer takes the editor. Inside :data:`SLICE` they share one budget the desktop sits outside
of, and the desktop gets the CPU first.

How a memory shortage ends decides how many runs it costs. The slice has a hard ``MemoryMax`` and
no ``MemoryHigh`` throttle: a throttle below the cap makes the kernel reclaim the agents' own pages
long before the machine is short, every agent stalls on it, and systemd-oomd reads that stall as
pressure and kills a whole run scope with every child in it. At the cap the kernel instead kills
ONE process. Each run's scope carries its own ``MemoryMax`` (a runaway test suite or browser in one
run is killed inside that run) and ``OOMPolicy=continue`` (losing that process does not stop the
rest of the run). systemd-oomd stays as the last resort for a slice that truly thrashes, and it
passes over run scopes (``ManagedOOMPreference=avoid``) while anything else in the slice — a
fixture server, a build — can be ended instead.

``systemd-run --scope`` registers its own pid in a new scope and then execs the command, so the
pid a caller records, signals and waits on is the agent's own.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interact.config import Config

SLICE = "interact-agents.slice"
_SYSTEMCTL_TIMEOUT = 5

log = logging.getLogger(__name__)


class Ceiling(BaseModel):
    """The shares of the machine all agents together, and each run alone, may use."""

    model_config = ConfigDict(frozen=True)

    memory_max_percent: int = Field(ge=1, le=100)
    run_memory_max_percent: int = Field(ge=1, le=100)
    oomd_pressure_percent: int = Field(ge=1, le=100)
    cpu_percent: int = Field(ge=1, le=100)
    cpu_weight: int = Field(ge=1, le=10000)

    @model_validator(mode="after")
    def _run_fits_in_slice(self) -> "Ceiling":
        if self.run_memory_max_percent > self.memory_max_percent:
            raise ValueError("one run's memory cap must not exceed all agents' cap")
        return self

    @classmethod
    def from_config(cls, config: Config) -> "Ceiling":
        return cls(
            memory_max_percent=config.agent_memory_max_percent,
            run_memory_max_percent=config.agent_run_memory_max_percent,
            oomd_pressure_percent=config.agent_oomd_pressure_percent,
            cpu_percent=config.agent_cpu_percent,
            cpu_weight=config.agent_cpu_weight,
        )

    def properties(self, cores: int) -> dict[str, str]:
        """systemd resource-control properties for :data:`SLICE`. ``MemoryHigh`` and
        ``MemorySwapMax`` are named to clear what an earlier ceiling set on this boot."""
        return {
            "MemoryHigh": "infinity",
            "MemoryMax": f"{self.memory_max_percent}%",
            "MemorySwapMax": "infinity",
            "CPUQuota": f"{max(1, cores) * self.cpu_percent}%",
            "CPUWeight": str(self.cpu_weight),
            "ManagedOOMMemoryPressure": "kill",
            "ManagedOOMMemoryPressureLimit": f"{self.oomd_pressure_percent}%",
        }

    def run_properties(self) -> dict[str, str]:
        """systemd properties for one run's own scope."""
        return {
            "MemoryMax": f"{self.run_memory_max_percent}%",
            "OOMPolicy": "continue",
            "ManagedOOMPreference": "avoid",
        }


def contained(argv: Sequence[str], ceiling: Ceiling | None = None) -> list[str]:
    """``argv`` run in its own capped scope inside the agent slice, or unchanged where no user
    systemd exists or ``INTERACT_AGENT_CEILING`` is off. An explicit ``ceiling`` always applies.

    A slice whose limits cannot be applied right now (systemctl timing out under load) still gets
    the run its own scope: run unscoped, it would live and die inside its launcher's scope."""
    if ceiling is None:
        config = Config()
        if not config.agent_ceiling:
            return list(argv)
        ceiling = Ceiling.from_config(config)
    systemd_run = shutil.which("systemd-run")
    systemctl = shutil.which("systemctl")
    if not sys.platform.startswith("linux") or systemd_run is None or systemctl is None:
        return list(argv)
    try:
        _prepare_slice(systemctl, ceiling)
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("%s limits not applied this time (%s); the run still gets its own scope", SLICE, error)
    scope = [f"--property={key}={value}" for key, value in ceiling.run_properties().items()]
    return [systemd_run, "--user", "--scope", "--quiet", "--collect", f"--slice={SLICE}", *scope, "--", *argv]


def _prepare_slice(systemctl: str, ceiling: Ceiling) -> None:
    """Start the slice and apply the ceiling for this boot only; nothing is written to disk."""
    run = dict(check=True, capture_output=True, timeout=_SYSTEMCTL_TIMEOUT)
    subprocess.run([systemctl, "--user", "start", SLICE], **run)
    assignments = [f"{key}={value}" for key, value in ceiling.properties(os.cpu_count() or 1).items()]
    subprocess.run([systemctl, "--user", "set-property", "--runtime", SLICE, *assignments], **run)
