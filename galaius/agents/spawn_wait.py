"""The lean waiter of a spawn in line (`SpawnLine._hold`): a process of a few MB where `spawn_line`'s costs ~300.

A spawn waiting for a place used to be held by a full galaius process, 258-290 MB each (2026-11-20: 6 queued spawns,
~1.7 GB, on a PC whose agents' memory cap is what the line protects). Only the run that may start next needs the
real thing. This module imports the standard library alone: it reads the line's state (`#spawn-line.state`, written whole
at each change, read here with no lock) and the ready file its head publishes (`#spawn-ready.state`), and starts the
full waiter (`python -m galaius.agents.spawn_line RUN_ID --yields`) as its child once `verdict` says its run may be next.
That child decides as it always did (`SpawnLine._turn`); a wake that proves wrong costs one full process until it
looks, then it hands the place back (`SpawnLine.wait`, exit `YIELDED`) and this process sleeps again.

Who wakes (`verdict`): the first run waiting only for a place (the head: the one full waiter that stays); a run the head
published as startable ahead of the line (a child sharing its parent's place, or whose parent set its place aside); a run
held at launch (`Place.holds`) once the reopening its hold names has passed, or at once when a hold names none; every
run when no head has published for `STALE_SECONDS` (an older galaius's head never does, a dead head neither), which is the
former behaviour, one full waiter each.

Held at launch, a usage-limit hold is read here at its named reopening only: a login that reopened earlier (`quota.lifted_after`)
starts the run when that time comes. Windows and macOS: not exercised.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Literal

#: How often a lean waiter, a full one, or a resume looks whether its turn came (`SpawnLine.wait`, `agent_queue.dispatch`).
POLL_SECONDS = 3.0
#: A ready file (`SpawnLine._publish`) older than this names nobody: its head is gone.
READY_TTL = 15.0
#: With no head publishing for this long since the lean waiter began, or since its last publication, every run wakes.
STALE_SECONDS = 30.0
#: What the full waiter exits with when its run may not start yet and is not the head: its lean parent takes the place back.
YIELDED = 75

Verdict = Literal["gone", "wake", "sleep"]


def _entries(raw: bytes | None) -> list[dict] | None:
    """The line's places as dicts; None when the state is not JSON of a list (the full waiter judges)."""
    try:
        listed = json.loads(raw) if raw else []
    except ValueError:
        return None
    return [entry for entry in listed if isinstance(entry, dict)] if isinstance(listed, list) else None


def published(raw: bytes | None) -> tuple[float, frozenset[str]]:
    """The head's publication: when, and the runs it found startable ahead of the line; (0, none) when absent or unreadable."""
    try:
        said = json.loads(raw) if raw else {}
        at, ids = said.get("at"), said.get("ids")
        return (float(at), frozenset(str(run_id) for run_id in ids)) if isinstance(at, (int, float)) and isinstance(ids, list) else (0.0, frozenset())
    except (ValueError, AttributeError, TypeError):
        return 0.0, frozenset()


def verdict(places_raw: bytes | None, ready_raw: bytes | None, run_id: str, *, now: float, since: float) -> Verdict:
    """What the waiter of `run_id` does now: `gone` (its place is not waiting any more: nothing to hold), `wake` (a full
    waiter judges its turn), or `sleep`. `since`: when this waiter began, the earliest a missing head counts from."""
    places = _entries(places_raw)
    if places is None:
        return "wake"
    mine = next((place for place in places if place.get("run_id") == run_id), None)
    if mine is None or mine.get("state") != "waiting" or not isinstance(mine.get("spawn"), dict):
        return "gone"
    if mine.get("said"):  # held at launch: until its holds lift
        untils = [(hold.get("cause") or {}).get("until") if isinstance(hold, dict) else None for hold in mine.get("holds") or []]
        return "sleep" if untils and all(isinstance(until, (int, float)) and now < until for until in untils) else "wake"
    head = next((place for place in places if place.get("state") == "waiting" and not place.get("said")), None)
    if head is not None and head.get("run_id") == run_id:
        return "wake"
    at, ready = published(ready_raw)
    if run_id in ready and now - at <= READY_TTL:
        return "wake"
    return "wake" if now - max(at, since) > STALE_SECONDS else "sleep"


def _seen(path: str) -> bytes | None:
    try:
        with open(path, "rb") as source:
            return source.read()
    except FileNotFoundError:
        return None


def wait(run_id: str, places_path: str, ready_path: str) -> int:
    """Hold `run_id`'s place until it may start, the full waiter its child meanwhile; the exit code ends the process."""
    since = time.time()
    while True:
        said = verdict(_seen(places_path), _seen(ready_path), run_id, now=time.time(), since=since)
        if said == "gone":
            return 0
        if said == "sleep":
            time.sleep(POLL_SECONDS)
            continue
        child = subprocess.run([sys.executable, "-P", "-m", "galaius.agents.spawn_line", run_id, "--yields"], check=False)
        if child.returncode != YIELDED:
            return child.returncode  # started, gone, or failed: its place says which
        since = time.time()
        time.sleep(POLL_SECONDS)


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: python -m galaius.agents.spawn_wait RUN_ID PLACES_FILE READY_FILE")
    if sys.platform != "linux":  # Linux sees a runtime in use from /proc (`RuntimeStore.in_use`); the others only from this record
        from galaius.upgrade.store import Runtime, RuntimeStore

        RuntimeStore.default().register(Runtime.own(), os.getpid())
    raise SystemExit(wait(*sys.argv[1:]))


if __name__ == "__main__":
    main()
