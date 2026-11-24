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

from galaius.agents.limits import quota
from galaius.agents import registry as reg
from galaius.agents.limits.ceiling import RUN_SCOPE_PREFIX, slice_cgroup
from galaius.config.settings import Config

#: Why a turn waits: its login's usage window is shut, its login was lost, or the agents' memory is past its soft limit.
HoldReason = Literal["usage_limit", "login_lost", "memory"]


class MemoryReading(BaseModel):
    """The agents' memory past its soft limit (`SoftMemory.hold`): since when, the working set's share of the slice's cap,
    the share it waits under, and the slice's largest tenants that are no agent run (a browser venue, a shared server)
    with their working sets in GB — waiting frees none of it, so the line names them for someone to stop."""

    model_config = ConfigDict(frozen=True)

    reason: Literal["memory"] = "memory"
    at: float
    used_percent: float | None
    release_percent: int
    holders: tuple[tuple[str, float], ...] = ()
    #: The PC's free memory (`MemAvailable`) in GB and the amount it waits for, when the PC as a whole is short: the
    #: agents' own share can be low while other programs (a container stack, the browser) hold the rest.
    pc_available_gb: float | None = None
    pc_release_gb: float | None = None
    #: The share of time the whole PC stalled on memory (PSI full avg10) and the share it waits under, when that holds.
    pc_stall_percent: float | None = None
    pc_stall_release_percent: float | None = None


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
        if memory.pc_stall_percent is not None and memory.pc_stall_release_percent is not None:
            return (f"en pause : le PC attend sa mémoire {memory.pc_stall_percent:.0f} % du temps"
                    + (f", reprise sous {memory.pc_stall_release_percent:g} %" if promise else ""))
        if memory.pc_available_gb is not None and memory.pc_release_gb is not None:
            return (f"en pause : mémoire du PC à {memory.pc_available_gb:.1f} Go libres"
                    + (f", reprise au-dessus de {memory.pc_release_gb:.1f} Go" if promise else ""))
        held_by = (" (dont " + ", ".join(f"{name} : {gb:.1f} Go" for name, gb in memory.holders) + ", hors agents)") if memory.holders else ""
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
    """The agents' soft memory limits: past `soft_percent` of the slice's cap (`Config.agent_memory_soft_percent`), or
    with the PC's free memory under `pc_min_percent` of its RAM (`Config.agent_pc_available_min_percent`), no turn
    starts, until the slice is back under `release_percent` and the PC's free memory over `pc_release_percent`; nor while
    every task of the PC stalls on memory `stall_percent` of the time (`Config.agent_pc_memory_stall_percent`, PSI
    full avg10), until its last minute is under `stall_release_percent`.

    The slice is measured as the kernel's working set — `memory.current` less the file cache it would drop first
    (`inactive_file`): the slice sits at its cap on page cache alone (15.0 of 15.0 GB, 8.7 GB of it in use, 2026-10-20).
    The PC as `MemAvailable`: on 2026-11-09 the slice stood at 10 of its 18.4 GB while a container stack held 8.4 GB and
    the full swap, and the kernel's OOM killer took the person's own browser. The stall: on 2026-11-16 his browser froze
    (full avg300 17 %) while `MemAvailable` still read ~3 GB, the compressed swap full. No cgroup (no user manager), one
    without a cap, no `meminfo` or no `pressure` file (not Linux, a kernel without PSI): that limit holds nothing.

    The cap is the slice's own `min(MemoryHigh, MemoryMax)`: the kernel throttles every task of the slice at `MemoryHigh`,
    long before 85 % of `MemoryMax`. And a stall the slice's own tasks suffer (its `memory.pressure`, over the same limit)
    is no pressure of the PC: waiting for it to clear never ends, the throttle being what keeps it up (2026-11-24 20:00 to
    22:05: three spawns queued 2.1 h, the PC with 8 GB free, the slice at 9.7 GB over its 9.35 GB `MemoryHigh`). Past the
    cap the hold names the slice's tenants instead, for someone to stop."""

    model_config = ConfigDict(frozen=True)

    soft_percent: int = Field(ge=1, le=100)
    release_percent: int = Field(ge=1, le=100)
    cgroup: Path | None
    pc_min_percent: int = Field(default=0, ge=0, le=100)
    pc_release_percent: int = Field(default=0, ge=0, le=100)
    meminfo: Path | None = None
    stall_percent: float = Field(default=0, ge=0, le=100)
    stall_release_percent: float = Field(default=0, ge=0, le=100)
    pressure: Path | None = None

    @model_validator(mode="after")
    def _release_past_hold(self) -> Self:
        if self.release_percent > self.soft_percent:
            raise ValueError("the memory release share must not exceed the soft limit")
        if self.pc_release_percent < self.pc_min_percent:
            raise ValueError("the PC's free memory to resume at must not be under the share it holds at")
        if self.stall_release_percent > self.stall_percent:
            raise ValueError("the memory stall to resume under must not exceed the stall it holds at")
        return self

    @classmethod
    def here(cls, config: Config) -> Self:
        meminfo, pressure = Path("/proc/meminfo"), Path("/proc/pressure/memory")
        return cls(soft_percent=config.agent_memory_soft_percent, release_percent=config.agent_memory_release_percent,
                   cgroup=slice_cgroup(), pc_min_percent=config.agent_pc_available_min_percent,
                   pc_release_percent=config.agent_pc_available_release_percent,
                   meminfo=meminfo if meminfo.is_file() else None,
                   stall_percent=config.agent_pc_memory_stall_percent,
                   stall_release_percent=config.agent_pc_memory_stall_release_percent,
                   pressure=pressure if pressure.is_file() else None)

    def pc_available(self) -> tuple[int, int] | None:
        """The PC's free memory (`MemAvailable`) and its RAM, in bytes; None when not measurable."""
        if self.meminfo is None:
            return None
        try:
            fields = {name: int(rest.split()[0]) * 1024 for name, _, rest in
                      (line.partition(":") for line in self.meminfo.read_text().splitlines()) if rest.strip()}
        except (OSError, ValueError, IndexError):
            return None
        total = fields.get("MemTotal")
        available = fields.get("MemAvailable")
        return (available, total) if available is not None and total else None

    def pc_stall(self) -> tuple[float, float] | None:
        """The share of time every task of the PC stalled on memory over the last 10 s and 60 s (PSI `full`), in percent;
        None when not measurable."""
        if self.pressure is None:
            return None
        try:
            line = next(line for line in self.pressure.read_text().splitlines() if line.startswith("full "))
            fields = dict(field.split("=") for field in line.split()[1:])
            return float(fields["avg10"]), float(fields["avg60"])
        except (OSError, ValueError, StopIteration, KeyError):
            return None

    def used_percent(self) -> float | None:
        """The working set's share of the cap now (`cap`), in percent; None when not measurable."""
        cap = self.cap()
        used = _working_set(self.cgroup) if cap is not None else None
        return None if used is None or cap is None else 100.0 * used / cap

    def cap(self) -> int | None:
        """The slice's cap in bytes: the lower of `memory.max` and `memory.high`; None when it has none or is not measurable."""
        if self.cgroup is None:
            return None
        caps = []
        for name in ("memory.max", "memory.high"):
            try:
                value = (self.cgroup / name).read_text().strip()
            except OSError:
                continue
            if value.isdigit() and int(value) > 0:
                caps.append(int(value))
        return min(caps) if caps else None

    def group_stall(self) -> float | None:
        """The share of time every task of the slice stalled on memory over the last 10 s (its own PSI `full`), in percent;
        None when not measurable."""
        if self.cgroup is None:
            return None
        try:
            line = next(line for line in (self.cgroup / "memory.pressure").read_text().splitlines() if line.startswith("full "))
            return float(dict(field.split("=") for field in line.split()[1:])["avg10"])
        except (OSError, ValueError, StopIteration, KeyError):
            return None

    def holders(self, count: int = 3) -> tuple[tuple[str, float], ...]:
        """The slice's `count` largest children that are no run's own scope, each with its working set in GB (those holding
        0.1 GB or more); none when there is none."""
        if self.cgroup is None:
            return ()
        sizes = sorted(((child.name, used) for child in self.cgroup.iterdir()
                        if child.is_dir() and not child.name.startswith(RUN_SCOPE_PREFIX) and (used := _working_set(child)) and used >= 1e8),
                       key=lambda size: size[1], reverse=True)
        return tuple((name, used / 1e9) for name, used in sizes[:count])

    def hold(self, *, now: float | None = None) -> Hold | None:
        """The memory hold now: from crossing `soft_percent` until back under `release_percent` (`#memory-hold.state`
        keeps when it began, for every process asking)."""
        used = self.used_percent()
        pc = self.pc_available() if self.pc_min_percent else None
        pc_percent = None if pc is None else 100.0 * pc[0] / pc[1]
        slice_over = used is not None and used >= self.soft_percent
        pc_short = pc_percent is not None and pc_percent < self.pc_min_percent
        slice_clear = used is None or used < self.release_percent
        pc_clear = pc_percent is None or pc_percent >= self.pc_release_percent
        stall = self.pc_stall() if self.stall_percent else None
        own = self.group_stall() if stall is not None else None
        # The slice's own stall is the PC's too, and only freeing the slice ends it: it holds through `used` above, never here.
        ours = own is not None and own >= self.stall_percent
        stalled = stall is not None and stall[0] >= self.stall_percent and not ours
        stall_clear = stall is None or stall[1] < self.stall_release_percent or ours
        moment = time.time() if now is None else now
        with reg.shared_state("memory-hold") as (raw, write, _keep):
            since = _since(raw)
            if (slice_clear and pc_clear and stall_clear) or (since is None and not slice_over and not pc_short and not stalled):
                if since is not None:
                    write(b"")
                return None
            if since is None:
                since = moment
                write(repr(since).encode())
        # The PC stalling names the stall, the PC short its free memory; else the hold is the slice's own.
        pc_said = (dict(pc_available_gb=pc[0] / 1e9, pc_release_gb=pc[1] * self.pc_release_percent / 100 / 1e9)
                   if pc is not None and not pc_clear else {})
        if stall is not None and not stall_clear:
            pc_said |= dict(pc_stall_percent=max(stall), pc_stall_release_percent=self.stall_release_percent)
        return Hold(cause=MemoryReading(at=since, used_percent=used, release_percent=self.release_percent, holders=self.holders(),
                                        **pc_said))


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
