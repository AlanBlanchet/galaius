"""One run described from its record: how it ended and why, the model it got and why, its tree, what its queue holds, what it said last.

The read side of `galaius run <id>`, over the same records as `galaius conversation` (`galaius.agents.run_ledger`): no registry import,
no server asked for the run, no secret read (a login shows as its 8-character digest, a prompt never). The record is read whole for the
one run asked, the trimmed cache for its tree.
"""

import json
import re
import subprocess
import time
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel

from galaius.agents.run_ledger import AT_WORK, Ending, RunLedger, RunQuery, RunRow, _line, alive
from galaius.host.paths import UserPaths

#: Queue states that are done and well; any other (pending, running, failed, uncertain) is shown.
QUEUE_SETTLED = frozenset({"replied", "cancelled"})
QUEUE_SHOWN = 8
TREE_SHOWN = 40
#: Commits named in a report that are checked against the live build, at most.
COMMITS_CHECKED = 4
GIT_SECONDS = 3.0
_COMMIT = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")


class Passed(BaseModel):
    """A model the run's pick passed over, and the vendor's or the system's reason."""

    model: str
    reason: str
    until: float | None = None
    said: str = ""


class Considered(BaseModel):
    """Why the run got its model: what it asked for, the ranked candidates and what was passed over."""

    criterion: str = ""
    reasoning: str = ""
    requested_model: str = ""
    chosen: str = ""
    #: The chosen model's place among the candidates (0 = the best the criterion ranks), None when it is none of them (pinned, moved, relaunched).
    chosen_rank: int | None = None
    candidates: int = 0
    top: list[str] = []
    passed: list[Passed] = []
    #: A turn a limit refused and sent again on another model: the model that refused it, its pool, when the pool reopens.
    fell_back_from: str = ""
    fell_back_pool: str = ""
    fell_back_resets_at: float | None = None


class Branch(BaseModel):
    """A run of the same tree."""

    run_id: str
    role: str
    status: str
    model: str
    depth: int
    started_at: float
    said: str = ""


class QueueEntry(BaseModel):
    """One message or turn queued for the run that is not settled well."""

    sender: str
    state: str
    enqueued_at: float
    error: str = ""


class Queue(BaseModel):
    counts: dict[str, int] = {}
    unsettled: list[QueueEntry] = []
    more: int = 0


class Ended(BaseModel):
    """How it stopped, as the record says, each only when there is something to say."""

    cut: str = ""
    cut_source: str = ""
    cut_at: float | None = None
    stopped_by: str = ""
    relaunched_from: str = ""
    relaunch_refused: str = ""
    successor: str = ""
    resumed_cuts: int = 0
    exit_code: int | None = None


class Slice(BaseModel):
    """A commit its report names, against the commit the app serves now."""

    commit: str
    repository: str
    verdict: Literal["live", "not live", "unknown"]
    why: str


class RunDescription(BaseModel):
    run: RunRow
    kind: str = ""
    cwd: str = ""
    permission_mode: str = ""
    pid: int | None = None
    #: True while its process runs; None where the system cannot say (no /proc) or the run has no pid.
    process_alive: bool | None = None
    unit: str = ""
    context_tokens: int | None = None
    context_window: int | None = None
    why_model: Considered
    ended: Ended
    ancestors: list[Branch] = []
    tree: list[Branch] = []
    queue: Queue = Queue()
    report: Ending
    slices: list[Slice] = []


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _name(candidate: object) -> str:
    entry = _dict(candidate)
    return f"{entry.get('provider') or '?'}/{entry.get('model') or '?'}"


def considered(raw: dict, row: RunRow) -> Considered:
    """Why this model: from the candidates the criterion ranked and the ones the pick passed over."""
    candidates = raw.get("candidates") if isinstance(raw.get("candidates"), list) else []
    chosen = f"{row.provider}/{row.model}".strip("/")
    rank = next((_count(_dict(entry).get("rank")) for entry in candidates if _name(entry) == chosen), None)
    passed = [Passed(model=_name(_dict(entry).get("candidate")), reason=_text(_dict(entry).get("reason")) or "?", until=entry.get("until") if isinstance(entry.get("until"), (int, float)) else None,
                     said=_line(entry.get("message"), 160)) for entry in (raw.get("skipped") if isinstance(raw.get("skipped"), list) else []) if isinstance(entry, dict)]
    fell = _dict(raw.get("fell_back_from"))
    return Considered(criterion=_text(raw.get("requested_criterion")), reasoning=_text(raw.get("reasoning")), requested_model=_text(raw.get("requested_model")), chosen=chosen,
                      chosen_rank=rank, candidates=len(candidates), top=[_name(entry) for entry in candidates[:3]], passed=passed,
                      fell_back_from=_text(fell.get("model")), fell_back_pool=_text(fell.get("pool")),
                      fell_back_resets_at=fell.get("resets_at") if isinstance(fell.get("resets_at"), (int, float)) else None)


def ended(raw: dict, row: RunRow) -> Ended:
    interruption, stopped, relaunched = _dict(raw.get("interruption")), _dict(raw.get("stopped_by")), _dict(raw.get("relaunched_from"))
    replaced = raw.get("replaced_by") or raw.get("successor")
    successor = _text(_dict(replaced).get("run_id")) or _text(replaced)
    who = f"{stopped.get('kind')} {stopped.get('name') or ''} {str(stopped.get('run_id') or '')[:8]}".strip() if stopped else ""
    return Ended(cut=row.cut, cut_source=_text(interruption.get("source")), cut_at=interruption.get("at") if isinstance(interruption.get("at"), (int, float)) else None,
                 stopped_by=who, relaunched_from=_line(f"{relaunched.get('provider')}/{relaunched.get('model')} {relaunched.get('cause') or ''}: {relaunched.get('said') or ''}", 240) if relaunched else "",
                 relaunch_refused=_line(raw.get("relaunch_refused"), 240), successor=successor[:8], resumed_cuts=row.resumed_cuts, exit_code=row.exit_code)


def queue_of(run_id: str, root=None) -> Queue:
    """What the run's message queue holds: counts by state, and the entries not settled well, newest last, with their errors."""
    try:
        items = json.loads(((root or UserPaths.agents()) / f"{run_id}.queue.json").read_text(encoding="utf-8")).get("items", [])
    except (OSError, ValueError, AttributeError):
        return Queue()
    items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    counts: dict[str, int] = {}
    for item in items:
        counts[_text(item.get("state")) or "?"] = counts.get(_text(item.get("state")) or "?", 0) + 1
    open_ = [item for item in items if item.get("state") not in QUEUE_SETTLED]
    shown = [QueueEntry(sender=_text(item.get("sender")) or "?", state=_text(item.get("state")) or "?", enqueued_at=float(item.get("enqueued_at") or 0.0), error=_line(item.get("error"), 200))
             for item in open_[-QUEUE_SHOWN:]]
    return Queue(counts=counts, unsettled=shown, more=max(0, len(open_) - len(shown)))


def _branch(row: RunRow, depth: int) -> Branch:
    return Branch(run_id=row.run_id, role=row.role, status=row.status, model=f"{row.provider}/{row.model}".strip("/"), depth=depth, started_at=row.started_at, said=_line(row.cut and f"cut: {row.cut}" or row.said, 100))


def describe(ref: str, ledger: RunLedger | None = None, *, report_chars: int | None = 1500, live_build: Callable[[], str | None] | None = None) -> RunDescription:
    """The run `ref` (its full id or a prefix) as its record tells it. `LookupError` when no run, or several, are named. `live_build` gives
    the app's build id, asked only when its report names a commit its folder holds (None: not asked, so no slice is judged)."""
    ledger = ledger or RunLedger()
    ending = ledger.last_words(ref, text_chars=report_chars)
    row = ending.run
    try:
        raw = json.loads((ledger.root / f"{row.run_id}.json").read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    everyone = ledger.rows(RunQuery(conversation=row.conversation))
    by_id = {each.run_id: each for each in everyone}
    chain, parent = [], row.parent
    while parent in by_id and len(chain) < 16:
        chain.append(by_id[parent])
        parent = by_id[parent].parent
    kids: dict[str | None, list[RunRow]] = {}
    for each in everyone:
        kids.setdefault(each.parent, []).append(each)
    tree: list[Branch] = []

    def walk(of: str, depth: int) -> None:
        for child in kids.get(of, ()):
            if len(tree) < TREE_SHOWN:
                tree.append(_branch(child, depth))
                walk(child.run_id, depth + 1)

    walk(row.run_id, 0)
    cwd = _text(raw.get("cwd"))
    pid = _count(raw.get("pid"))
    return RunDescription(
        run=row, kind=_text(raw.get("kind")), cwd=cwd, permission_mode=_text(raw.get("permission_mode")), pid=pid,
        process_alive=alive(raw, strict=False) if pid and row.status in AT_WORK | {"gone"} else None, unit=_text(raw.get("unit")),
        context_tokens=_count(raw.get("context_tokens")), context_window=_count(raw.get("context_window")),
        why_model=considered(raw, row), ended=ended(raw, row), ancestors=[_branch(each, depth) for depth, each in enumerate(reversed(chain))], tree=tree,
        queue=queue_of(row.run_id, ledger.root), report=ending, slices=slices_of(ending.text, cwd, live_build) if live_build else [],
    )


# --- is its slice live -----------------------------------------------------------------------------------------------


def _git(cwd: str, *args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(["git", "-C", cwd, *args], check=False, capture_output=True, text=True, timeout=GIT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return 2, ""
    return done.returncode, done.stdout.strip()


def slices_of(report: str, cwd: str, live_build: Callable[[], str | None]) -> list[Slice]:
    """Each commit the report names that the run's folder knows, against the commit of the app's build id (`<date>-<short commit>`): live
    when it is that commit or an ancestor of it. `unknown` says what could not be checked, never « live »."""
    known = []
    for name in dict.fromkeys(_COMMIT.findall(report)) if cwd else ():
        if len(known) >= COMMITS_CHECKED:
            break
        code, full = _git(cwd, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        if code == 0 and full:
            known.append(full)
    if not known:
        return []
    code, top = _git(cwd, "rev-parse", "--show-toplevel")
    where = top.rsplit("/", 1)[-1] if code == 0 else cwd
    build = live_build()
    if not build:
        return [Slice(commit=full[:8], repository=where, verdict="unknown", why="the app's build could not be read") for full in known]
    code, head = _git(cwd, "rev-parse", "--verify", "--quiet", f"{build.rsplit('-', 1)[-1]}^{{commit}}")
    if code != 0 or not head:
        return [Slice(commit=full[:8], repository=where, verdict="unknown", why=f"the app's build {build} names a commit this checkout does not hold") for full in known]
    found = []
    for full in known:
        code, _ = _git(cwd, "merge-base", "--is-ancestor", full, head)
        found.append(Slice(commit=full[:8], repository=where, verdict="live" if code == 0 else "not live" if code == 1 else "unknown",
                           why=f"{'reachable from' if code == 0 else 'not reachable from' if code == 1 else 'could not be compared with'} the app's build {build}"))
    return found


def seconds_since(instant: float | None, now: float | None = None) -> float | None:
    return None if not instant else (time.time() if now is None else now) - instant
