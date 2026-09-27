"""One capped systemd slice for every agent interact starts.

Agents fan out: each can start MCP servers, browsers, parallel test workers and dev servers, and
several agents run at once. Uncapped, they share the editor's cgroup, so when memory runs out the
OOM killer takes the editor. Inside :data:`SLICE` they share one budget the desktop sits outside
of: the kernel throttles them past ``MemoryHigh``, stops them at ``MemoryMax`` (no swap, so they
fail fast instead of thrashing the machine), gives the desktop the CPU first, and systemd-oomd
kills an agent before anything else.

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
    """The shares of the machine all agents together may use."""

    model_config = ConfigDict(frozen=True)

    memory_high_percent: int = Field(ge=1, le=100)
    memory_max_percent: int = Field(ge=1, le=100)
    cpu_percent: int = Field(ge=1, le=100)
    cpu_weight: int = Field(ge=1, le=10000)

    @model_validator(mode="after")
    def _throttle_before_kill(self) -> "Ceiling":
        if self.memory_high_percent > self.memory_max_percent:
            raise ValueError("agent memory high must not exceed agent memory max")
        return self

    @classmethod
    def from_config(cls, config: Config) -> "Ceiling":
        return cls(
            memory_high_percent=config.agent_memory_high_percent,
            memory_max_percent=config.agent_memory_max_percent,
            cpu_percent=config.agent_cpu_percent,
            cpu_weight=config.agent_cpu_weight,
        )

    def properties(self, cores: int) -> dict[str, str]:
        """systemd resource-control properties for :data:`SLICE`."""
        return {
            "MemoryHigh": f"{self.memory_high_percent}%",
            "MemoryMax": f"{self.memory_max_percent}%",
            "MemorySwapMax": "0",
            "CPUQuota": f"{max(1, cores) * self.cpu_percent}%",
            "CPUWeight": str(self.cpu_weight),
            "ManagedOOMMemoryPressure": "kill",
        }


def contained(argv: Sequence[str], ceiling: Ceiling | None = None) -> list[str]:
    """``argv`` run inside the capped agent slice, or unchanged where no user systemd exists or
    ``INTERACT_AGENT_CEILING`` is off. An explicit ``ceiling`` always applies."""
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
        log.warning("agent runs uncapped: %s could not be prepared (%s)", SLICE, error)
        return list(argv)
    return [systemd_run, "--user", "--scope", "--quiet", "--collect", f"--slice={SLICE}", "--", *argv]


def _prepare_slice(systemctl: str, ceiling: Ceiling) -> None:
    """Start the slice and apply the ceiling for this boot only; nothing is written to disk."""
    run = dict(check=True, capture_output=True, timeout=_SYSTEMCTL_TIMEOUT)
    subprocess.run([systemctl, "--user", "start", SLICE], **run)
    assignments = [f"{key}={value}" for key, value in ceiling.properties(os.cpu_count() or 1).items()]
    subprocess.run([systemctl, "--user", "set-property", "--runtime", SLICE, *assignments], **run)
