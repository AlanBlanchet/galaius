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


class MemoryReading(BaseModel):
    """The agents' memory past its soft limit (`SoftMemory.hold`): since when, the working set's share of the slice's cap,
    the share it waits under, and the slice's largest tenant that is no agent run (a browser venue, a shared server)
    with its working set in GB — waiting frees none of it, so the line names it for someone to stop."""

    model_config = ConfigDict(frozen=True)

    reason: Literal["memory"] = "memory"
    at: float
    used_percent: float
    release_percent: int
    tenant: tuple[str, float] | None = None


def _moment(at: datetime) -> str:
    """`at` as the line says it: its hour today, « demain 20:01 », else its date (a weekly window reopens days later:
    « jusqu'à 20:01 » read as tonight, 2026-10-31)."""
    days = (at.date() - datetime.now().date()).days
    return at.strftime("%H:%M") if days <= 0 else f"demain {at:%H:%M}" if days == 1 else at.strftime("%d/%m %H:%M")


class Hold(BaseModel):
    """One reason a turn does not start here now: its cause, a login's pause (`quota.Pause`, for `provider` /
    `subscription`) or the agents' memory (`MemoryReading`, every turn)."""

    model_config = ConfigDict(frozen=True)

    cause: quota.Pause | MemoryReading = Field(discriminator="reason")
    provider: str | None = None
    subscription: UUID | None = None

    @model_validator(mode="after")
    def _login_of_its_cause(self) -> Self:
        """A login's pause names that login; the memory hold, every turn's, none."""
        if isinstance(self.cause, quota.Pause) != (self.provider is not None) or (self.provider is None and self.subscription is not None):
            raise ValueError("a login's pause names its provider; the memory hold names no login")
        return self

    @property
    def reason(self) -> HoldReason:
        return self.cause.reason

    @property
    def until(self) -> float | None:
        """`usage_limit`: when its window reopens (it starts then, with no click); None: when its cause clears."""
        return self.cause.until if isinstance(self.cause, quota.Pause) else None

    @classmethod
    def of_login(cls, provider: str, subscription: UUID | None, *, now: float | None = None) -> Self | None:
        """This login's hold (`quota.login_pause`), None when it takes turns."""
        pause = quota.login_pause(provider, subscription=subscription, now=now)
        return None if pause is None else cls(cause=pause, provider=provider, subscription=subscription)

    @classmethod
    def usage(cls, provider: str, subscription: UUID | None, *, now: float | None = None) -> Self | None:
        """This login's usage-limit hold, None while its window is open (a lost login is asked again at its start)."""
        hold = cls.of_login(provider, subscription, now=now)
        return hold if hold is not None and hold.reason == "usage_limit" else None

    def holding(self, memory: SoftMemory | None, *, now: float | None = None) -> bool:
        """Still holding at `now`, asked again of its cause (`memory` None: no soft limit)."""
        if self.reason == "memory":
            return memory is not None and memory.hold(now=now) is not None
        return Hold.of_login(self.provider or "", self.subscription, now=now) is not None

    def line(self, *, promise: bool = True) -> str:
        """The waiting run's line (`agents list`, `agents send`): « en pause : limite d'usage claude jusqu'à 17:21, reprise
        sans clic »; `promise` False where nothing waits to start it (a start refused, `Paused` outside the line)."""
        if self.reason == "usage_limit":
            at = _moment(datetime.fromtimestamp(self.until)) if self.until is not None else "sa réouverture"
            return f"en pause : limite d'usage {self.provider} jusqu'à {at}" + (", reprise sans clic" if promise else "")
        if self.reason == "login_lost":
            return f"en pause : connexion {self.provider} perdue" + (", reprise sans clic une fois reconnectée sur ce PC" if promise else "")
        memory = self.cause
        assert isinstance(memory, MemoryReading)
        held_by = f" (dont {memory.tenant[0]} : {memory.tenant[1]:.1f} Go, hors agents)" if memory.tenant is not None else ""
        return (f"en pause : mémoire des agents à {memory.used_percent:.0f} % de leur plafond{held_by}"
                + (f", reprise sous {memory.release_percent} %" if promise else ""))


class Paused(PermissionError):
    """A spawn none of whose candidates may start now (`run.run_agent`): each login it could run on is held (`holds`).
    It waits in line, said why, and starts once one of them lifts (`SpawnLine.launch`).

    A refusal of the request for now, so a `PermissionError` like every other refusal the machine
    channel answers the web with (`machines.ANSWERED_REFUSALS`): raised out of a start nobody queued,
    bare `Exception` left the server waiting and recording « the PC never confirmed the launch »
    instead of saying which login is held until when (2026-10-23 16:29)."""

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
        return Hold(cause=MemoryReading(at=since, used_percent=used, release_percent=self.release_percent, tenant=self.tenant()))


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
