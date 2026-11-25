"""Is a run's time and capability spent on its task? The five ways a run loses either to something other than its task, read from what
the run records and transcripts already hold:

1. `started_with_less`: the process came up with fewer tools, connected MCP servers or skills, without a server its peers had connected, or
   without memory notes, than the role's normal start (the `system init` line of the run's raw stream, against the role's peers over
   `baseline_days` ON THE SAME LOGIN: a login's own config folder decides which account connectors and vendor skills the CLI hands every
   run, so a run is never measured against another login's start, and a fenced run's tools are the fence's);
2. bookkeeping: the share of its tool calls that serve the process around the task (`run_slowness.BOOKKEEPING`);
3. a wait for an agent place (`RunProfile.queued`) or, now, a spawn still waiting for one (`health.agents`);
4. `vendor_end`: it ended by a vendor error (an overload, a rate or usage limit, a lost login) and nothing carried its work on;
5. `model_below_pick`, `effort_below_role`: it ran on a model the role's rule ranks below its first pick, or at a lower effort than its role's usual.

A verdict needs `baseline_peers` other runs of the same role (and provider and login, for a start) to compare with: with fewer it says nothing, it never
guesses. Every threshold is a field of `EfficiencyLimits`. Stdlib JSON over the record and the head of the raw stream; no server, no registry import."""

import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from galaius.agents.run_ledger import AT_WORK, RunLedger, RunQuery, RunRow
from galaius.agents.run_slowness import Slowness, slowness

#: A cut the vendor (or the login it holds) causes, as the run record names it.
VENDOR_CUTS = frozenset({"usage_limit", "login_lost", "network_lost"})
#: What a run's last words say when the vendor ended it without a recorded cut.
VENDOR_SAID = re.compile(r"API Error:\s*(?:429|5\d\d)|\boverloaded\b|rate.?limit|usage limit|Not logged in", re.IGNORECASE)
#: Reasoning levels, lowest first; a level this list does not know is never compared.
EFFORT = ("minimal", "low", "medium", "high", "xhigh", "max")
#: Lines of a raw stream read for its `system init` line (it is the first).
INIT_LINES = 40
#: The status the CLI's `system init` line gives an MCP server that is up.
CONNECTED = "connected"


class EfficiencyLimits(BaseModel):
    """The thresholds of the check, one place to change them."""

    model_config = ConfigDict(frozen=True)

    #: A run whose bookkeeping share of its calls is over this is a finding, once it made `bookkeeping_min_calls` (a run of 5 calls says nothing).
    bookkeeping_share: float = 0.20
    bookkeeping_min_calls: int = 30
    #: A spawn that waited this long for an agent place (seconds).
    place_wait: float = 900.0
    #: A start with fewer than this share of its role's normal tools / MCP servers / skills.
    start_ratio: float = 0.8
    #: …and fewer by at least this many (a read-only role's 13 tools against 17 is no nerf).
    start_min_gap: int = 5
    #: The role's normal is read from its runs of the last `baseline_days`, from at least `baseline_peers` other runs.
    baseline_days: float = 14.0
    baseline_peers: int = 3
    #: The window the periodic check reads (seconds).
    window: float = 6 * 3600.0
    #: Findings listed; the rest is counted.
    listed: int = 12


class StartKit(BaseModel):
    """What a process came up with: the counts its `system init` line holds."""

    model_config = ConfigDict(frozen=True)

    tools: int
    #: MCP servers connected at start (one that failed, waits for a login or was still pending is a server the run does not have).
    mcp: int
    skills: int
    memory: bool
    #: The names of the connected servers: a start without the one its peers all had (the browser's, galaius's) is a loss a count of 1 hides.
    servers: frozenset[str] = frozenset()


def read_fenced(root: Path, run_id: str) -> bool:
    """Whether the run's record names a fence (a community run: its tools and folders are the fence's choice, never the role's normal)."""
    try:
        record = json.loads((root / f"{run_id}.json").read_bytes())
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and bool(record.get("fence"))


def read_start(root: Path, run_id: str) -> StartKit | None:
    """The kit the run `run_id` started with, from the head of `<root>/<run_id>.raw.jsonl`; None where no init line is there (another provider's
    stream, a file gone): such a start is not compared."""
    try:
        with (root / f"{run_id}.raw.jsonl").open("rb") as handle:
            for index, raw in enumerate(handle):
                if index >= INIT_LINES:
                    break
                if b'"init"' not in raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
                    count = lambda key: len(event[key]) if isinstance(event.get(key), list) else 0  # noqa: E731
                    memory = event.get("memory_paths")
                    servers = event.get("mcp_servers")
                    up = frozenset(str(item.get("name") or index) for index, item in enumerate(servers if isinstance(servers, list) else [])
                                   if isinstance(item, dict) and item.get("status") == CONNECTED)
                    return StartKit(tools=count("tools"), mcp=len(up), skills=count("skills"), memory=bool(memory), servers=up)
    except OSError:
        pass
    return None


class Pick(BaseModel):
    """How a run's model and effort were picked: its rank among the role's candidates, why the first was passed over, the effort it ran at."""

    model_config = ConfigDict(frozen=True)

    rank: int | None = None
    first: str = ""
    #: Why the first candidate was not used (`quota_exceeded`, `provider_off`, …); "" when the record gives no reason.
    why: str = ""
    effort: str = ""


def read_pick(root: Path, row: RunRow) -> Pick | None:
    """The `Pick` of `row` from its record file; None where the record is unreadable."""
    try:
        record = json.loads((root / f"{row.run_id}.json").read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    candidates = [c for c in record.get("candidates") or [] if isinstance(c, dict)]
    rank = next((i for i, c in enumerate(candidates) if c.get("model") == row.model and c.get("provider") == row.provider), None)
    first = candidates[0] if candidates else {}
    why = next((str(s.get("reason") or "") for s in record.get("skipped") or []
                if isinstance(s, dict) and isinstance(s.get("candidate"), dict) and s["candidate"].get("model") == first.get("model")), "")
    effort = record.get("reasoning")
    return Pick(rank=rank, first=str(first.get("model") or ""), why=why, effort=effort if isinstance(effort, str) else "")


class RunEfficiency(BaseModel):
    """What one run lost, by member of the class (a field empty or None where it lost nothing or the data does not say)."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    role: str
    start: StartKit | None = None
    #: « tools 27 of 141 », « no memory notes »: each way the start was below the role's normal.
    started_with_less: list[str] = []
    #: Why there is no verdict on its start: "" when there is one, else « no start line », « 2 peers of 3 ».
    start_unjudged: str = ""
    vendor_end: str = ""
    #: The model ran below the role's first pick, with the reason the record gives ("" when it gives none).
    model_below_pick: str = ""
    #: The reason is recorded (a quota, a provider switched off): a routine fallback, shown but never raised.
    model_below_explained: bool = False
    effort_below_role: str = ""


class RoleEfficiency(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str
    runs: int
    started_with_less: int
    unjudged_starts: int
    vendor_ends: int
    model_below_pick: int
    effort_below_role: int


class Efficiency(BaseModel):
    model_config = ConfigDict(frozen=True)

    limits: EfficiencyLimits
    runs: list[RunEfficiency]
    by_role: list[RoleEfficiency]


def _normal(values: list[int]) -> int:
    """The role's normal of a count: its 75th percentile, so that a few nerfed peers do not lower it."""
    ordered = sorted(values)
    return ordered[int(0.75 * (len(ordered) - 1) + 0.5)]


def _less(kit: StartKit, peers: list[StartKit], limits: EfficiencyLimits) -> list[str]:
    said = []
    for name, label in (("tools", "tools"), ("mcp", "MCP servers"), ("skills", "skills")):
        normal = _normal([getattr(peer, name) for peer in peers])
        if getattr(kit, name) < limits.start_ratio * normal and normal - getattr(kit, name) >= limits.start_min_gap:
            said.append(f"{label} {getattr(kit, name)} of {normal}")
    for server in sorted(name for name in set().union(*(peer.servers for peer in peers)) - kit.servers):
        if sum(server in peer.servers for peer in peers) * 2 >= len(peers):
            said.append(f"MCP server {server} not connected")
    if not kit.memory and sum(peer.memory for peer in peers) * 2 >= len(peers):
        said.append("no memory notes")
    return said


def vendor_end(row: RunRow) -> str:
    """The vendor's words when `row` ended by a vendor error and nothing carried its work on (no successor, no relaunch), else ""."""
    if row.status in AT_WORK or row.status == "done" or row.successor:
        return ""
    if row.cut in VENDOR_CUTS:
        return row.said or row.cut
    return row.said if VENDOR_SAID.search(row.said or "") and row.status == "failed" else ""


def efficiency(rows: list[RunRow], baseline: Iterable[RunRow], root: Path, limits: EfficiencyLimits = EfficiencyLimits(),
               *, kit: Callable[[Path, str], StartKit | None] = read_start, pick: Callable[[Path, RunRow], Pick | None] = read_pick,
               fenced: Callable[[Path, str], bool] = read_fenced) -> Efficiency:
    """The efficiency of `rows`, each judged against the runs of the same role in `baseline` (itself excluded); its start against the peers
    that came up under the same provider and login."""
    roles = {row.role for row in rows}
    peers_of = [row for row in baseline if row.role in roles]
    kits: dict[str, StartKit | None] = {}
    picks: dict[str, Pick | None] = {}

    def start_of(row: RunRow) -> StartKit | None:
        if row.run_id not in kits:
            kits[row.run_id] = kit(root, row.run_id)
        return kits[row.run_id]

    def pick_of(row: RunRow) -> Pick | None:
        if row.run_id not in picks:
            picks[row.run_id] = pick(root, row)
        return picks[row.run_id]

    judged: list[RunEfficiency] = []
    for row in rows:
        mine = start_of(row)
        same_login = [start_of(p) for p in peers_of if p.role == row.role and p.provider == row.provider and p.login == row.login and p.run_id != row.run_id]
        known = [p for p in same_login if p is not None]
        less, unjudged = [], ""
        if mine is None:
            unjudged = "no start line"
        elif fenced(root, row.run_id):
            unjudged = "fenced run"
        elif len(known) < limits.baseline_peers:
            unjudged = f"{len(known)} peers of {limits.baseline_peers}"
        else:
            less = _less(mine, known, limits)
        chosen = pick_of(row)
        below, explained, effort = "", False, ""
        if chosen is not None and chosen.rank:
            below = f"{row.model} is the rule's pick #{chosen.rank + 1}, first pick {chosen.first}" + (f" passed over: {chosen.why}" if chosen.why else " (no reason recorded)")
            explained = bool(chosen.why)
        if chosen is not None and chosen.effort in EFFORT:
            levels = [p.effort for p in (pick_of(peer) for peer in peers_of if peer.role == row.role and peer.run_id != row.run_id) if p is not None and p.effort in EFFORT]
            if len(levels) >= limits.baseline_peers:
                usual = max(set(levels), key=lambda level: (levels.count(level), EFFORT.index(level)))
                if EFFORT.index(chosen.effort) < EFFORT.index(usual):
                    effort = f"effort {chosen.effort}, the role's usual is {usual}"
        judged.append(RunEfficiency(run_id=row.run_id, role=row.role, start=mine, started_with_less=less, start_unjudged=unjudged,
                                    vendor_end=vendor_end(row), model_below_pick=below, model_below_explained=explained, effort_below_role=effort))
    groups: dict[str, list[RunEfficiency]] = defaultdict(list)
    for item in judged:
        groups[item.role].append(item)
    by_role = [RoleEfficiency(role=role, runs=len(items), started_with_less=sum(bool(i.started_with_less) for i in items),
                              unjudged_starts=sum(bool(i.start_unjudged) for i in items), vendor_ends=sum(bool(i.vendor_end) for i in items),
                              model_below_pick=sum(bool(i.model_below_pick) for i in items), effort_below_role=sum(bool(i.effort_below_role) for i in items))
               for role, items in sorted(groups.items())]
    return Efficiency(limits=limits, runs=judged, by_role=by_role)


def assess(ledger: RunLedger, rows: list[RunRow], *, now: float, limits: EfficiencyLimits = EfficiencyLimits()) -> Efficiency:
    """`efficiency` of `rows` against the same roles' runs of the last `limits.baseline_days`, read from `ledger`."""
    baseline = ledger.rows(RunQuery(conversation=None, since=now - limits.baseline_days * 86400.0, roles=frozenset(row.role for row in rows)))
    return efficiency(rows, baseline, ledger.root, limits)


def _span(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds}s" if seconds < 90 else f"{seconds // 60}m" if seconds < 5400 else f"{seconds / 3600:.1f}h"


def findings(report: Slowness, found: Efficiency) -> list[str]:
    """What broke in the window, one plain sentence each, worst kinds first: a start below its role's normal, a vendor-error end, a wait for a
    place, a bookkeeping share over the limit, an unexplained fall below the role's first pick or its usual effort. A fallback whose reason the
    record gives (a quota, a provider switched off) is routine: it shows in `galaius conversation slow`, it is no finding."""
    limits, judged = found.limits, {item.run_id: item for item in found.runs}
    said: list[str] = []
    short: dict[str, list[tuple[str, list[str]]]] = {}   # the runs of a role that started with less are one sentence
    for profile in report.runs:
        if (item := judged.get(profile.run_id)) is not None and item.started_with_less:
            short.setdefault(profile.role, []).append((profile.run_id[:8], item.started_with_less))
    for role, runs in short.items():
        worst: dict[str, tuple[int, int]] = {}   # what was short -> the lowest count seen, and the normal
        for _, parts in runs:
            for part in parts:
                label, _, counts = part.rpartition(" of ")
                name, _, value = label.rpartition(" ")
                if counts.isdigit() and value.isdigit():
                    worst[name] = (min(int(value), worst.get(name, (10**9, 0))[0]), int(counts))
                else:
                    worst[part] = (0, 0)
        what = ", ".join(f"{name} {low} of {normal}" if normal else name for name, (low, normal) in worst.items())
        ids = [run for run, _ in runs]
        said.append(f"{role}: {len(ids)} run{'s' if len(ids) > 1 else ''} ({', '.join(ids[:4])}{', …' if len(ids) > 4 else ''}) started with less than the role's normal: {what}"
                    + (" (the lowest seen)." if len(ids) > 1 else "."))
    for profile in report.runs:
        if (item := judged.get(profile.run_id)) is not None and item.vendor_end:
            said.append(f"{profile.role} run {profile.run_id[:8]} ended by a vendor error ({profile.status}): {item.vendor_end[:140]}")
    for profile in report.runs:
        if profile.queued is not None and profile.queued > limits.place_wait:
            said.append(f"{profile.role} run {profile.run_id[:8]} waited {_span(profile.queued)} for an agent place before it started.")
    for profile in report.runs:
        if profile.calls >= limits.bookkeeping_min_calls and profile.bookkeeping_calls() / profile.calls > limits.bookkeeping_share:
            parts = ", ".join(f"{label} {count}" for label, count in sorted(profile.bookkeeping.items(), key=lambda kv: -kv[1]))
            said.append(f"{profile.role} run {profile.run_id[:8]} spent {profile.bookkeeping_calls()} of its {profile.calls} tool calls "
                        f"({profile.bookkeeping_calls() / profile.calls:.0%}) on bookkeeping: {parts}.")
    for profile in report.runs:
        item = judged.get(profile.run_id)
        if item is not None and item.model_below_pick and not item.model_below_explained:
            said.append(f"{profile.role} run {profile.run_id[:8]} ran below its role's first pick: {item.model_below_pick}.")
        if item is not None and item.effort_below_role:
            said.append(f"{profile.role} run {profile.run_id[:8]} ran at a lower effort than its role's usual: {item.effort_below_role}.")
    return said


def look(ledger: RunLedger, *, now: float, limits: EfficiencyLimits = EfficiencyLimits()) -> tuple[Slowness, Efficiency, list[str]]:
    """The window `[now - limits.window, now]` over every conversation on this computer: its slowness, its efficiency, and what broke."""
    since = now - limits.window
    rows = ledger.rows(RunQuery(conversation=None, since=since))
    # a run that began before the window and still works (a coordinator for days) is read too: its start and its calls in the window count
    present = {row.run_id for row in rows}
    rows += [row for row in ledger.rows(RunQuery(conversation=None, statuses=AT_WORK)) if row.run_id not in present]
    report = slowness(ledger, rows, since=since, now=now)
    found = assess(ledger, rows, now=now, limits=limits)
    return report, found, findings(report, found)
