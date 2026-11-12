"""A close saying « RUNNING » tells the person a run is at work and its report will come back to this conversation. Only
a run that reports to the closing one wakes it (`handback.reporting`, a spawn still in line); without one nothing ever
answers him. `refusal` says why such a close may not stand; `galaius.close_hook` refuses it at the end of the turn,
running `python -m galaius.agents.turns.running_claim RUN_ID` with the close on stdin (it prints the reason, if any).

2026-11-10: a coordinator closed « RUNNING: push and install guide; infra-engineer reports back here. »; owner
2026-11-12: « And the coordinator finished, and i never got things done and pushed... There is a real comunication
problem ! ». Each run a RUNNING line says does its work (`promised`) must be at work for this conversation; one of that role elsewhere is listed so the closing
run reads what it did before it calls the work undone."""

import re
import sys
import time
from collections.abc import Collection, Iterable, Sequence
from datetime import datetime

from galaius.agents import registry as reg
from galaius.agents.catalog import relations
from galaius.agents.spawn_line import SpawnLine
from galaius.close_hook import claimed
from galaius.agents.turns import handback

#: Roles whose name is an everyday word in a close (« the main page », « galaius ships it »), never read as a run named.
UNNAMED_ROLES = frozenset({"main", "galaius", "community", "agent"})
#: Where a clause of a RUNNING line starts, and what may open it before the name doing its work.
CLAUSE = re.compile(r"[;:|().\u2014\n]|\bthen\b")
LEAD = re.compile(r"^[\s*_`>-]*(?:(?:the|a|an|our|its)\s+)?")
#: A second name joined to the first that does the work with it.
JOINED = re.compile(r"^\s*(?:,|and|&)\s*(?:(?:the|a|an)\s+)?")
#: How far back, and how many, runs of a named role in other conversations are listed.
ELSEWHERE_SECONDS = 7 * 86_400.0
ELSEWHERE_MAX = 3


def _role_pattern(role: str) -> str:
    """`role` as a close writes it, with hyphens or spaces (« infra engineer »)."""
    return r"[-\s]".join(map(re.escape, role.lower().split("-")))


def promised(lines: Sequence[str], roles: Iterable[str], known: Collection[str]) -> set[str]:
    """The runs a RUNNING line says are doing its work: each role or known short id that opens a clause (« visual-critic
    judges the page; infra-engineer pushes »), with those joined to it (« tester and visual-critic judge »). A name
    inside a clause is what that work is on (« fixes the tester's rows »), one followed by « 's » never does it."""
    names = "|".join(sorted(map(_role_pattern, roles), key=len, reverse=True)) or r"(?!)"
    name = re.compile(rf"(?P<role>{names})(?:[-\s]+(?P<run>[0-9a-f]{{8}}))?(?![\w'’])|(?P<id>[0-9a-f]{{8}})(?![\w'’])")
    found: set[str] = set()
    for clause in CLAUSE.split("\n".join(lines).lower()):
        rest = LEAD.sub("", clause, count=1)
        while (hit := name.match(rest)) is not None:
            run = hit.group("run") or hit.group("id")
            if hit.group("role"):
                found.add(next(role for role in roles if re.fullmatch(_role_pattern(role), hit.group("role"))))
            if run in known:
                found.add(run)
            rest = JOINED.sub("", rest[hit.end():], count=1) if JOINED.match(rest[hit.end():]) else ""
    return found


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
    known = {other.run_id[:8] for other in records}
    named = promised(lines, roles, known)
    missing_roles = sorted(name for name in named if name not in known and name not in covering_roles)
    missing_ids = sorted(name for name in named if name in known and not any(other.startswith(name) for other in covering_ids))
    if (reporters or in_line) and not missing_roles and not missing_ids:
        return None
    now = time.time() if now is None else now
    why = ("no run of yours is at work or still owes you its report, so nothing wakes this conversation and the person "
           "waits for a report that never comes" if not (reporters or in_line) else
           f"it says {', '.join([*missing_roles, *missing_ids])} does that work, and none is at work for this conversation")
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
