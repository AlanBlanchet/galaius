"""This PC's agent state as `galaius conversation stats --live` / `limits` and `galaius doctor` read it: the places agents take against
the limit, who waits, the agents' memory slice, each subscription login's usage and what this PC passes over. Local files only, a login
shown as its 8-character digest."""

import time
from datetime import UTC, datetime

from pydantic import BaseModel


class Live(BaseModel):
    """This PC now: the places agents take against the limit, who waits, the agents' memory slice."""

    places_taken: int | None = None
    places_max: int | None = None
    running: int | None = None
    waiting: list[dict] = []
    #: Spawns that will not start (`said` why): a line entry its holder left, a start cut off.
    failed: list[dict] = []
    memory_current: int | None = None
    memory_max: int | None = None


def live() -> Live:
    """Read through the registry's own line and cgroup (`SpawnLine.standing`, `ceiling.slice_cgroup`): imported only here, the
    one place that pays for them."""
    from galaius.agents.limits.ceiling import slice_cgroup
    from galaius.agents.spawn_line import SpawnLine

    load, queued = SpawnLine.here().standing()
    now, live = time.time(), Live(places_taken=load.places, places_max=load.limit, running=load.runs)
    entry = lambda place: {"run_id": place.run_id[:8], "role": place.label(), "waited_seconds": int(now - place.asked_at), "said": place.said[:120]}
    live.waiting = [entry(place) for place in queued if place.state != "failed"]
    live.failed = [entry(place) for place in queued if place.state == "failed"]
    if (folder := slice_cgroup()) is not None:
        for name, field in (("memory.current", "memory_current"), ("memory.max", "memory_max")):
            try:
                text = (folder / name).read_text().strip()
                setattr(live, field, int(text) if text.isdigit() else None)
            except OSError:
                pass
    return live


class Login(BaseModel):
    provider: str
    subscription: str
    login: str
    windows: list[dict] = []
    pools: list[dict] = []
    paused: list[dict] = []
    blocked: list[dict] = []


def limits() -> list[Login]:
    """Each subscription login here with its usage readings and what this PC remembers the vendor refusing it."""
    from galaius.agents.limits import quota
    from galaius.agents.limits.subscriptions import Subscription

    now = time.time()
    remembered = quota.readings(now)
    found = []
    for sub in Subscription.all():
        try:
            held, usage, pools = sub.state(), sub.usage(), sub.pools()
        except (OSError, ValueError):
            continue
        memory = remembered.get(f"{sub.provider}#{sub.id}")
        found.append(Login(
            provider=str(sub.provider), subscription=str(sub.id)[:8], login=held.identity.login[:8],
            windows=[{"window": w.window, "used_percent": round(w.used_percent, 1), "resets_at": w.resets_at.isoformat() if w.resets_at else None} for w in usage],
            pools=[{"pool": p.pool, "used_percent": round(p.used_percent, 1), "resets_at": p.resets_at.isoformat() if p.resets_at else None} for p in pools],
            paused=[{"reason": p.reason, "until": datetime.fromtimestamp(p.until, UTC).isoformat() if p.until else None, "said": p.said[:160]} for p in (memory.pauses.values() if memory else ())],
            blocked=[{"scope": scope, "until": datetime.fromtimestamp(b.until, UTC).isoformat(), "window": b.window, "refusals": b.count} for scope, b in (memory.blocks.items() if memory else ()) if b.until > now]))
    return found
