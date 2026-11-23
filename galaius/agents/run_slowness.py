"""Why a conversation is slow: where each run's wall-clock time went, read from its transcript (`<run id>.jsonl`) and its record.

A run's time between its start and its end (or now), over a window `[since, now]`, is split into disjoint parts:

* `parked`: the run ended a turn and nobody had started the next (a coordinator waiting for his next message, a reused agent
  between two briefs), or it was silent for over `IDLE_GAP` before a turn opened (a resume after a cut): neither work nor delay;
* `waits`: a tool call that only waits: on an agent it commissioned (`galaius agents wait`), on a deploy (`deploy_queue.py wait`),
  on a CI run (`gh run watch`), on a timer or a polling loop (`sleep`, `until ...; do sleep`);
* `tools`: any other tool call, from the call to its result (a build, a test run, a read);
* `dark`: no run in the window wrote anything for more than `DARK_GAP` seconds (the computer off or asleep, or every agent waiting on him):
  nobody was slow, so it is told apart from idle;
* `idle`: this run alone wrote nothing for more than `IDLE_GAP` seconds outside a tool call (a vendor limit pause, a stalled stream);
* `model`: the rest, the model thinking and answering between its events.

Before it starts, a spawned run may have `queued` seconds: from the spawn call's result in its parent's transcript (the call prints
the child's id at once) to the child's start. History holds no other record of a queue, so a run whose parent's transcript is gone
has none (`None`, never 0).

The transcript clips a tool call's input to ~300 characters: a wait whose telltale words come after that reads as an ordinary tool.
Stdlib JSON and regexes over the files, a byte search to the window's first line: no registry import, no server."""

import json
import os
import re
import statistics
import time
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from galaius.agents.run_ledger import AT_WORK, RunLedger, RunRow

#: A silence longer than this, outside any tool call, is `idle`; shorter, the model's own time.
IDLE_GAP = 120.0
#: A stretch in which no run of the window wrote anything for this long is `dark`.
DARK_GAP = 900.0
#: How far before its window a transcript is read back for the turn event that tells whether the run began it parked.
LEAD_BYTES = 1 << 20
#: How long before the window a parent's transcript is read for the spawn call of a child that started in it.
SPAWN_LOOKBACK = 3600.0
#: An ended turn counts as parked only past this many seconds (the stop hook's feedback answers within a few).
PARKED_AFTER = 5.0
#: The events that open a turn after the previous one ended.
TURN_OPENERS = frozenset({"message", "started", "injected"})

_WAITS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("agent", re.compile(r"\bgalaius\s+agents\s+wait\b|\bagent_wait\b")),
    ("deploy", re.compile(r"deploy_queue\.py['\"]?\s+wait\b|\bdeploy_queue\b[^\n]{0,80}\bwait\b")),
    ("ci", re.compile(r"\bgh\s+run\s+(?:watch|view)\b|\bgh\s+pr\s+checks\b")),
    ("timer", re.compile(r"(?:^|[;&|({]\s*|\b(?:then|do)\s+|['\"=]\s*)sleep\s+\d|\buntil\b[^\n]*\bsleep\b|\bwhile\b[^\n]*\bsleep\b")),
)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_SPAWNS = re.compile(r"\bgalaius\s+agents\s+spawn\b|\bagent_spawn\b")
_KIND = '"kind":"'
_AT = '"at":'


class Waits(BaseModel):
    """Seconds spent in tool calls that only wait, by what they wait on."""

    model_config = ConfigDict(frozen=True)

    agent: float = 0.0
    deploy: float = 0.0
    ci: float = 0.0
    timer: float = 0.0

    def total(self) -> float:
        return self.agent + self.deploy + self.ci + self.timer

    def __add__(self, other: "Waits") -> "Waits":
        return Waits(**{name: getattr(self, name) + getattr(other, name) for name in type(self).model_fields})


class RunProfile(BaseModel):
    """Where one run's time in the window went (see the module doc). `wall` = parked + dark + waits + tools + model + idle."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    role: str
    status: str
    conversation: str
    wall: float
    parked: float
    tools: float
    model: float
    idle: float
    dark: float = 0.0
    waits: Waits
    #: Seconds from the spawn call's result to the run's start; None where the parent's transcript does not say.
    queued: float | None = None

    def active(self) -> float:
        """Everything but parked and dark time: what the run spent being a run."""
        return self.wall - self.parked - self.dark


class Rollup(BaseModel):
    """What a group of runs (a role, a conversation) adds up to."""

    model_config = ConfigDict(frozen=True)

    name: str
    runs: int
    active: float
    tools: float
    model: float
    idle: float
    dark: float
    waits: Waits
    queued_runs: int
    queued_total: float
    queued_median: float | None
    queued_max: float | None


class Slowness(BaseModel):
    """`galaius conversation slow`: the window, every run's profile, the longest, and the rollups."""

    since: float
    until: float
    runs: list[RunProfile]
    longest: list[RunProfile]
    by_role: list[Rollup]
    by_conversation: list[Rollup]
    total: Rollup


# --- reading a transcript --------------------------------------------------------------------------------------------------


def _first_line_at(path: Path, since: float) -> int:
    """A line start at or before the first line whose `at` is `since` or later (the file is time ordered), within 64 KB of it: a byte
    search that only ever moves past a line it has read to be older than `since`."""
    with path.open("rb") as handle:
        low, high = 0, os.fstat(handle.fileno()).st_size
        while high - low > 65536:
            middle = (low + high) // 2
            handle.seek(middle)
            handle.readline()   # the rest of the line the offset fell in
            line = handle.readline()
            at = _line_at(line.decode("utf-8", "replace")) if line else None
            if at is None or at >= since:
                high = middle
            else:
                low = handle.tell()
        return low


def _line_at(line: str) -> float | None:
    index = line.rfind(_AT)
    if index < 0:
        return None
    end = index + len(_AT)
    stop = end
    while stop < len(line) and (line[stop].isdigit() or line[stop] == "."):
        stop += 1
    try:
        return float(line[end:stop])
    except ValueError:
        return None


def _line_kind(line: str) -> str:
    index = line.find(_KIND)
    if index < 0:
        return ""
    start = index + len(_KIND)
    return line[start:line.find('"', start)]


class Transcript(BaseModel):
    """What one run's transcript says, over a window: its events' times and kinds, its tool spans, and the children its spawn calls named."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    events: list[tuple[float, str]]
    #: (start, end, wait kind or "") of each tool call with a result.
    spans: list[tuple[float, float, str]]
    #: Child run id -> when the spawn call that named it returned.
    spawned: dict[str, float]
    #: The last turn event (a turn's end or its opening) before the window, which says whether the run began the window parked.
    lead: tuple[float, str] | None = None


def _lead(path: Path, offset: int) -> tuple[float, str] | None:
    """The last `done` / `error` / turn-opening event of `path` before byte `offset`, looking back at most `LEAD_BYTES`."""
    if offset <= 0:
        return None
    try:
        with path.open("rb") as handle:
            begin = max(0, offset - LEAD_BYTES)
            handle.seek(begin)
            chunk = handle.read(offset - begin).decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(chunk[1:] if begin else chunk):
        if (kind := _line_kind(line)) in TURN_OPENERS or kind in ("done", "error"):
            at = _line_at(line)
            return None if at is None else (at, kind)
    return None


def read_transcript(path: Path, since: float) -> Transcript:
    """The events of `path` from `since` on. A missing or unreadable file reads empty."""
    offset = 0
    events: list[tuple[float, str]] = []
    opened: dict[str, tuple[float, str, bool]] = {}
    spans: list[tuple[float, float, str]] = []
    spawned: dict[str, float] = {}
    try:
        offset = _first_line_at(path, since) if since > 0 else 0
        with path.open("rb") as handle:
            handle.seek(offset)
            for raw in handle:
                line = raw.decode("utf-8", "replace")
                at, kind = _line_at(line), _line_kind(line)
                if at is None or not kind:
                    continue
                if at < since:
                    continue
                events.append((at, kind))
                if kind == "tool":
                    try:
                        call = json.loads(line)
                    except ValueError:
                        continue
                    text = f"{call.get('tool') or ''} {call.get('tool_input') or ''}"
                    wait = next((name for name, pattern in _WAITS if pattern.search(text)), "")
                    opened[str(call.get("tool_id") or "")] = (at, wait, _SPAWNS.search(text) is not None)
                elif kind == "tool_result":
                    try:
                        result = json.loads(line)
                    except ValueError:
                        continue
                    if (call := opened.pop(str(result.get("tool_id") or ""), None)) is not None:
                        spans.append((call[0], at, call[1]))
                        if call[2]:
                            for child in _UUID.findall(str(result.get("text") or "")):
                                spawned.setdefault(child, at)
    except OSError:
        pass
    return Transcript(events=events, spans=spans, spawned=spawned, lead=_lead(path, offset))


# --- one run's profile -----------------------------------------------------------------------------------------------------


def _disjoint(spans: Iterable[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """`spans` as disjoint intervals in time order: a span starting inside the previous one (a parallel call) is cut to what it adds."""
    merged: list[tuple[float, float, str]] = []
    for start, end, kind in sorted(spans):
        if merged and start < merged[-1][1]:
            if end <= merged[-1][1]:
                continue
            start = merged[-1][1]
        if end > start:
            merged.append((start, end, kind))
    return merged


def _parked(events: list[tuple[float, str]], end: float) -> list[tuple[float, float, str]]:
    """The stretches after a turn's end and before the next turn opens (or the run's end, for the last one)."""
    stretches, ended = [], None
    for at, kind in events:
        if ended is not None and kind in TURN_OPENERS:
            if at - ended > PARKED_AFTER:
                stretches.append((ended, at, "parked"))
            ended = None
        elif kind in ("done", "error") and ended is None:
            ended = at
    if ended is not None and end - ended > PARKED_AFTER:
        stretches.append((ended, end, "parked"))
    return stretches


def profile_run(row: RunRow, transcript: Transcript, *, since: float, now: float, queued: float | None,
                dark: list[tuple[float, float, str]] = ()) -> RunProfile:
    """`row`'s time in `[since, now]` split as the module doc says; `dark` the stretches no run wrote in (`dark_stretches`)."""
    start = max(row.started_at, since)
    end = now if row.status in AT_WORK or not row.finished_at else min(row.finished_at, now)
    end = max(end, start)
    if row.status not in AT_WORK:   # an ended run's record time says nothing of the stretch after its last event (a cut, a rewrite)
        end = max(start, min(end, max((at for at, _ in transcript.events if at <= end), default=start)))
    events = [(max(at, start), kind) for at, kind in transcript.events if at <= end]
    if transcript.lead is not None and transcript.lead[0] < start:
        events.insert(0, (start, transcript.lead[1]))   # the window opens inside the turn that event began or ended
    marks = sorted({start, end, *(at for at, _ in events)})
    blocked = _disjoint([*((max(a, start), min(b, end), kind or "tool") for a, b, kind in transcript.spans if b > start and a < end)])
    stretches = _parked(events, end)
    if row.status == "waiting" and end - max((at for at, _ in events), default=start) > PARKED_AFTER:   # waiting for its next message, by its own record
        stretches.append((max((at for at, _ in events), default=start), end, "parked"))
    parked = _disjoint(stretches)
    totals: dict[str, float] = defaultdict(float)
    openers = {at for at, kind in events if kind in TURN_OPENERS}
    starts_blocked, starts_parked, starts_dark = [b[0] for b in blocked], [p[0] for p in parked], [d[0] for d in dark]

    def overlap(a: float, b: float, intervals: list[tuple[float, float, str]], starts: list[float]) -> list[tuple[float, float, str]]:
        found, index = [], max(0, bisect_right(starts, a) - 1)
        while index < len(intervals) and intervals[index][0] < b:
            s, e, kind = intervals[index]
            if e > a:
                found.append((max(a, s), min(b, e), kind))
            index += 1
        return found

    for a, b in zip(marks, marks[1:]):
        if b <= a:
            continue
        taken = 0.0
        for s, e, kind in overlap(a, b, parked, starts_parked):
            totals["parked"] += e - s
            taken += e - s
        for s, e, kind in overlap(a, b, blocked, starts_blocked):
            length = e - s
            # a tool call inside a parked stretch is the parked stretch's (nothing waits there)
            inside = sum(min(e, pe) - max(s, ps) for ps, pe, _ in overlap(s, e, parked, starts_parked))
            length -= max(0.0, inside)
            if length > 0:
                totals[kind] += length
                taken += length
        rest = (b - a) - taken
        if rest > 0:
            for s, e, kind in overlap(a, b, dark, starts_dark):
                totals["dark"] += min(e - s, rest)
                rest -= min(e - s, rest)
        if rest > 0:
            # a long silence that ends in a turn opening was the run waiting for its next turn (his message, a resume after a cut): parked
            totals["parked" if b - a > IDLE_GAP and b in openers else "idle" if b - a > IDLE_GAP else "model"] += rest
    wait = Waits(**{name: totals[name] for name in Waits.model_fields})
    return RunProfile(run_id=row.run_id, role=row.role, status=row.status, conversation=row.conversation, wall=end - start,
                      parked=totals["parked"], tools=totals["tool"], model=totals["model"], idle=totals["idle"], dark=totals["dark"], waits=wait, queued=queued)


# --- the window ------------------------------------------------------------------------------------------------------------


def rollup(name: str, profiles: list[RunProfile]) -> Rollup:
    queued = [p.queued for p in profiles if p.queued is not None]
    return Rollup(
        name=name, runs=len(profiles), active=sum(p.active() for p in profiles), tools=sum(p.tools for p in profiles),
        model=sum(p.model for p in profiles), idle=sum(p.idle for p in profiles), dark=sum(p.dark for p in profiles), waits=sum((p.waits for p in profiles), Waits()),
        queued_runs=len(queued), queued_total=sum(queued), queued_median=statistics.median(queued) if queued else None,
        queued_max=max(queued) if queued else None)


def dark_stretches(transcripts: Iterable[Transcript], *, since: float, now: float) -> list[tuple[float, float, str]]:
    """The stretches of `[since, now]` longer than `DARK_GAP` in which none of `transcripts` has an event."""
    marks = sorted({since, now, *(at for t in transcripts for at, _ in t.events if since <= at <= now)})
    return [(a, b, "dark") for a, b in zip(marks, marks[1:]) if b - a > DARK_GAP]


def slowness(ledger: RunLedger, rows: list[RunRow], *, since: float, now: float | None = None, top: int = 10) -> Slowness:
    """The profiles of `rows` over `[since, now]` and their rollups. Each run's transcript is read from `since` on, each parent's from
    `SPAWN_LOOKBACK` earlier (its spawn calls name the children that started in the window)."""
    moment = time.time() if now is None else now
    transcripts: dict[str, Transcript] = {}

    def transcript(run_id: str, from_at: float) -> Transcript:
        if run_id not in transcripts:
            transcripts[run_id] = read_transcript(ledger.root / f"{run_id}.jsonl", from_at)
        return transcripts[run_id]

    dark = dark_stretches((transcript(row.run_id, since - SPAWN_LOOKBACK) for row in rows), since=since, now=moment)
    profiles = []
    for row in rows:
        own = transcript(row.run_id, since - SPAWN_LOOKBACK)
        queued = None
        if row.parent is not None and row.started_at >= since:
            returned = transcript(row.parent, since - SPAWN_LOOKBACK).spawned.get(row.run_id)
            queued = None if returned is None else max(0.0, row.started_at - returned)
        profiles.append(profile_run(row, own, since=since, now=moment, queued=queued, dark=dark))
    groups: dict[str, list[RunProfile]] = defaultdict(list)
    chats: dict[str, list[RunProfile]] = defaultdict(list)
    for profile in profiles:
        groups[profile.role].append(profile)
        chats[profile.conversation].append(profile)
    ranked = lambda table: sorted((rollup(name, group) for name, group in table.items()), key=lambda item: -item.active)  # noqa: E731
    return Slowness(since=since, until=moment, runs=profiles, longest=sorted(profiles, key=lambda p: -p.active())[:top],
                    by_role=ranked(groups), by_conversation=ranked(chats), total=rollup("all", profiles))
