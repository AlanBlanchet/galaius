"""A close saying « RUNNING » tells the person a run is at work and its report will come back to this conversation. Only
a run that reports to the closing one wakes it (`handback.reporting`, a spawn still in line); without one nothing ever
answers him. `refusal` says why such a close may not stand; `galaius.close_hook` refuses it at the end of the turn,
running `python -m galaius.agents.turns.running_claim RUN_ID` with the close on stdin (it prints the reason, if any).

2026-11-10: a coordinator closed « RUNNING: push and install guide; infra-engineer reports back here. »; owner
2026-11-12: « And the coordinator finished, and i never got things done and pushed... There is a real comunication
problem ! ». A named run must be one at work for this conversation; one of that role elsewhere is listed so the closing
run reads what it did before it calls the work undone."""

import re
import sys
import time
from collections.abc import Iterable, Sequence
from datetime import datetime

from galaius.agents import registry as reg
from galaius.agents.catalog import relations
from galaius.agents.spawn_line import SpawnLine
from galaius.close_hook import claimed
from galaius.agents.turns import handback

#: A run's short id (`registry.short_id`) as a close names it.
SHORT_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])")
#: Roles whose name is an everyday word in a close (« the main page », « galaius ships it »), never read as a run named.
UNNAMED_ROLES = frozenset({"main", "galaius", "community", "agent"})
#: How far back, and how many, runs of a named role in other conversations are listed.
ELSEWHERE_SECONDS = 7 * 86_400.0
ELSEWHERE_MAX = 3


def _named_roles(lines: Sequence[str], roles: Iterable[str]) -> set[str]:
    said = "\n".join(lines).lower()
    return {role for role in roles if re.search(rf"(?<![\w-]){re.escape(role.lower())}(?!\w)", said)}


def _when(at: float | None) -> str:
    return datetime.fromtimestamp(at).strftime("%m-%d %H:%M") if at else "-"


def _line(run: reg.AgentRun) -> str:
    state = run.status if run.finished_at is None else f"{run.status} {_when(run.finished_at)}"
    return f"{reg.short_id(run.run_id)} {run.agent or run.name}, {state}: « {relations._first_line(run.task)} »"


def refusal(run_id: str, text: str, *, now: float | None = None) -> str | None:
    """Why the close `text` of run `run_id` may not stand, None when it may: it says RUNNING while no run will report back
    to it, or it names a run or role that is neither reporting to it nor at work in its conversation."""
    lines = claimed(text)
    run = reg.get_run(run_id)
    if not lines or run is None:
        return None
    reporters = handback.reporting(run_id)
    in_line = SpawnLine.here().waiting_for(run_id)
    conversation = reg.conversation_of(run_id)
    at_work = [other for other in reg.stored_tree(conversation) if other.run_id != run_id and relations._working(other)]
    covering_ids = {other.run_id for other in (*reporters, *at_work)} | {place.run_id for place in in_line}
    covering_roles = {other.agent for other in (*reporters, *at_work) if other.agent} | {place.role for place in in_line if place.role}
    records = reg._stored_records()
    roles = {other.agent for other in records if other.agent} - UNNAMED_ROLES - {run.agent}
    missing_roles = sorted(_named_roles(lines, roles) - covering_roles)
    known = {other.run_id[:8]: other for other in records}
    missing_ids = sorted({short for short in SHORT_ID.findall("\n".join(lines))
                          if short in known and not any(run_id.startswith(short) for run_id in covering_ids)})
    if (reporters or in_line) and not missing_roles and not missing_ids:
        return None
    now = time.time() if now is None else now
    why = ("no run of yours is at work or still owes you its report, so nothing wakes this conversation and the person "
           "waits for a report that never comes" if not (reporters or in_line) else
           f"it names {', '.join([*missing_roles, *missing_ids])}, which is not at work for this conversation")
    lines_out = [f"RUNNING CHECK (galaius runtime): this close says « {lines[-1][:160]} », but {why}.",
                 "Reporting back to you: " + ("; ".join([*(_line(other) for other in reporters),
                                                         *(f"{reg.short_id(place.run_id)} {place.label()}, in line" for place in in_line)]) or "none") + "."]
    for role in missing_roles:
        elsewhere = sorted((other for other in records if other.agent == role and other.started_at >= now - ELSEWHERE_SECONDS
                            and reg.conversation_of(other.run_id) != conversation), key=lambda other: other.started_at, reverse=True)
        for other in elsewhere[:ELSEWHERE_MAX]:
            lines_out.append(f"{role} in another conversation ({reg.short_id(reg.conversation_of(other.run_id))}): {_line(other)}")
    if len(lines_out) > 2:
        lines_out.append("Read what those runs did (`galaius agents events <id>`) before calling their work undone.")
    lines_out.append("Dispatch what is still owed (spawn it and end your turn: its report resumes this run), or close on what "
                     "is true now: DONE, PARTIAL or BLOCKED.")
    return "\n".join(lines_out)


if __name__ == "__main__":
    print(refusal(sys.argv[1], sys.stdin.read()) or "", end="")
