"""Whether an agent turn starts on this computer now: the one gate a spawn and a resumed turn pass (`SpawnLine`), and
why it holds them (`Hold`), said in the waiting run's line until it starts with no one's click.

2026-10-13 to 10-20: 71 usage-limit cuts in bursts of ~10 runs — once one run hit the session limit, every run
starting or resuming after it was refused too, until the reset. So a login whose usage window refused a turn and named
its reopening takes no new turn here until then (`quota.login_pause`), nor one whose login was lost until it answers or
is signed in again; and no turn starts at all while the agents' memory is past its soft limit (`SoftMemory`), the
kernel otherwise killing runs at the slice's cap.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from galaius.agents import quota
from galaius.agents import registry as reg
from galaius.agents.ceiling import RUN_SCOPE_PREFIX, slice_cgroup
from galaius.config import Config

#: Why a turn waits: its login's usage window is shut, its login was lost, or the agents' memory is past its soft limit.
HoldReason = Literal["usage_limit", "login_lost", "memory"]


class Hold(BaseModel):
    """One reason a turn does not start here now, since `since`; `provider` / `subscription` name the login it holds
    (None: every turn, the memory hold)."""

    model_config = ConfigDict(frozen=True)

    reason: HoldReason
    since: float
    #: `usage_limit`: when its window reopens (it starts then, with no click). None: when its cause clears.
    until: float | None = None
    provider: str | None = None
    subscription: UUID | None = None
    #: `memory`: the working set's share of the slice's cap when it held, and the share it waits under.
    used_percent: float | None = None
    release_percent: int | None = None
    #: `memory`: the slice's largest tenant that is no agent run (a browser venue, a shared server) and its working set
    #: in GB: waiting frees none of it, so the line names it for someone to stop.
    tenant: tuple[str, float] | None = None

    @classmethod
    def of_login(cls, provider: str, subscription: UUID | None, *, now: float | None = None) -> Self | None:
        """This login's hold (`quota.login_pause`), None when it takes turns."""
        pause = quota.login_pause(provider, subscription=subscription, now=now)
        if pause is None:
            return None
        return cls(reason=pause.reason, since=pause.at, until=pause.until, provider=provider, subscription=subscription)

    def holding(self, memory: SoftMemory | None, *, now: float | None = None) -> bool:
        """Still holding at `now`, asked again of its cause (`memory` None: no soft limit)."""
        if self.reason == "memory":
            return memory is not None and memory.hold(now=now) is not None
        assert self.provider is not None
        return Hold.of_login(self.provider, self.subscription, now=now) is not None

    def line(self, *, promise: bool = True) -> str:
        """The waiting run's line (`agents list`, `agents send`): « en pause : limite d'usage claude jusqu'à 17:21, reprise
        sans clic »; `promise` False where nothing waits to start it (a start refused, `Paused` outside the line)."""
        if self.reason == "usage_limit":
            at = datetime.fromtimestamp(self.until).strftime("%H:%M") if self.until is not None else "sa réouverture"
            return f"en pause : limite d'usage {self.provider} jusqu'à {at}" + (", reprise sans clic" if promise else "")
        if self.reason == "login_lost":
            return f"en pause : connexion {self.provider} perdue" + (", reprise sans clic une fois reconnectée sur ce PC" if promise else "")
        held_by = f" (dont {self.tenant[0]} : {self.tenant[1]:.1f} Go, hors agents)" if self.tenant is not None else ""
        return (f"en pause : mémoire des agents à {self.used_percent:.0f} % de leur plafond{held_by}"
                + (f", reprise sous {self.release_percent} %" if promise else ""))


class Paused(Exception):
    """A spawn none of whose candidates may start now (`run.run_agent`): each login it could run on is held (`holds`).
    It waits in line, said why, and starts once one of them lifts (`SpawnLine.launch`)."""

    def __init__(self, holds: tuple[Hold, ...]) -> None:
        super().__init__("; ".join(hold.line(promise=False) for hold in holds))
        self.holds = holds


class SoftMemory(BaseModel):
    """The agents' soft memory limit (`Config.agent_memory_soft_percent`): past that share of the slice's cap no turn
    starts, until it is back under `release_percent`. Measured as the kernel's working set — `memory.current` less the
    file cache it would drop first (`inactive_file`): the slice sits at its cap on page cache alone (15.0 of 15.0 GB,
    8.7 GB of it in use, 2026-10-20). No cgroup (no user manager), or one without a cap: no hold."""

    model_config = ConfigDict(frozen=True)

    soft_percent: int = Field(ge=1, le=100)
    release_percent: int = Field(ge=1, le=100)
    cgroup: Path | None

    @model_validator(mode="after")
    def _release_below_soft(self) -> Self:
        if self.release_percent > self.soft_percent:
            raise ValueError("the memory release share must not exceed the soft limit")
        return self

    @classmethod
    def here(cls, config: Config) -> Self:
        return cls(soft_percent=config.agent_memory_soft_percent, release_percent=config.agent_memory_release_percent,
                   cgroup=slice_cgroup())

    def used_percent(self) -> float | None:
        """The working set's share of the cap now, in percent; None when not measurable."""
        if self.cgroup is None:
            return None
        try:
            cap = (self.cgroup / "memory.max").read_text().strip()
        except OSError:
            return None
        used = _working_set(self.cgroup)
        if used is None or cap == "max" or not cap.isdigit() or int(cap) == 0:
            return None
        return 100.0 * used / int(cap)

    def tenant(self) -> tuple[str, float] | None:
        """The slice's largest child that is no run's own scope, and its working set in GB; None when there is none."""
        if self.cgroup is None:
            return None
        sizes = [(child.name, used) for child in self.cgroup.iterdir()
                 if child.is_dir() and not child.name.startswith(RUN_SCOPE_PREFIX) and (used := _working_set(child))]
        name, used = max(sizes, key=lambda size: size[1], default=(None, 0))
        return (name, used / 1e9) if name is not None else None

    def hold(self, *, now: float | None = None) -> Hold | None:
        """The memory hold now: from crossing `soft_percent` until back under `release_percent` (`#memory-hold.state`
        keeps when it began, for every process asking)."""
        used = self.used_percent()
        moment = time.time() if now is None else now
        with reg.shared_state("memory-hold") as (raw, write, _keep):
            since = _since(raw)
            if used is None or used < self.release_percent or (since is None and used < self.soft_percent):
                if since is not None:
                    write(b"")
                return None
            if since is None:
                since = moment
                write(repr(since).encode())
        return Hold(reason="memory", since=since, used_percent=used, release_percent=self.release_percent, tenant=self.tenant())


def _working_set(cgroup: Path) -> int | None:
    """`cgroup`'s working set in bytes: `memory.current` less the file cache the kernel drops first (`inactive_file`)."""
    try:
        current = int((cgroup / "memory.current").read_text())
        stat = dict(line.split() for line in (cgroup / "memory.stat").read_text().splitlines() if line.count(" ") == 1)
    except (OSError, ValueError):
        return None
    return max(0, current - int(stat.get("inactive_file", 0)))


def _since(raw: bytes | None) -> float | None:
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def turn_hold(provider: str, subscription: UUID | None, memory: SoftMemory | None, *, now: float | None = None) -> Hold | None:
    """Why a turn of this login does not start now: its login held, else the memory hold (`memory` None: no soft limit);
    None: it may start."""
    return Hold.of_login(provider, subscription, now=now) or (memory.hold(now=now) if memory is not None else None)
