"""Why a conversation is slow: where each run's wall-clock time went, read from its transcript (`<run id>.jsonl`) and its record.

A run's time between its start and its end (or now), over a window `[since, now]`, is split into disjoint parts:

* `parked`: the run ended a turn and nobody had started the next (a coordinator waiting for his next message, a reused agent
  between two briefs), or it was silent for over `IDLE_GAP` before a turn opened (a resume after a cut): neither work nor delay;
* `waits`: a tool call that only waits: on an agent it commissioned (`galaius agents wait`), on a deploy (`deploy_queue.py wait`),
  on a CI run (`gh run watch`), on a timer or a polling loop (`sleep`, `until ...; do sleep`), on his answer to a question
  (`AskUserQuestion`), on a background job's output; a call still open when the window ends counts to its end;
* `tools`: any other tool call, from the call to its result (a build, a test run, a read);
* `dark`: no run of the rows read wrote anything for more than `DARK_GAP` seconds before a later event (the computer off or asleep, or every
  agent waiting on him): nobody was slow, so it is told apart from idle;
* `idle`: this run alone wrote nothing for more than `IDLE_GAP` seconds outside a tool call (a vendor limit pause, a stalled stream);
* `model`: the rest, the model thinking and answering between its events.

Before it starts, a spawned run may have `queued` seconds: from the spawn call's result in its parent's transcript (the call prints
the child's id at once) to the child's start. History holds no other record of a queue, so a run whose parent's transcript is gone
has none (`None`, never 0).

The transcript clips a tool call's input to ~300 characters: a wait whose telltale words come after that reads as an ordinary tool.
Stdlib JSON and regexes over the files, a byte search to the window's first line: no registry import, no server."""

import json
import math
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
#: How long before the window a parent's transcript is read for the spawn call of a child that started in it.
SPAWN_LOOKBACK = 3600.0
#: An ended turn counts as parked only past this many seconds (the stop hook's feedback answers within a few).
PARKED_AFTER = 5.0
#: The events that open a turn after the previous one ended.
TURN_OPENERS = frozenset({"message", "started", "injected"})

#: Where a command starts in a tool input (`command='…'`): the input's own start, after `;` `&` `|` `(`, after `do` / `then`; then what may sit before the
#: program (`nohup`, `env -u X`, `VAR=x`, `$PY`, `timeout N`). A telltale word inside a quoted argument (`rg 'sleep 5'`, `git log --grep 'galaius agents wait'`)
#: is no command.
_START = (r"(?:^|command=['\"]\s*|[;&|(]\s*|\b(?:do|then)\s+)"
          r"(?:(?:nohup|env(?:\s+-u\s+\S+|\s+\w+=\S+)*|\$\w+|\w+=\S+|timeout\s+\d+)\s+)*")
_WAITS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("agent", re.compile(rf"{_START}(?:\S*/)?galaius\s+agents\s+wait\b")),
    ("deploy", re.compile(rf"{_START}(?:\S*/)?(?:python3?\s+|\S*/galaius\s+--python\s+)?(?:\S*/)?deploy_queue\.py['\"]?\s+wait\b")),
    ("ci", re.compile(rf"{_START}(?:\S*/)?gh\s+run\s+watch\b")),
    # a timer is a command that IS the sleep or a polling loop, never a call that sleeps for a second among other work (`kill x; sleep 1; pytest`)
    ("timer", re.compile(r"^(?:command=['\"]\s*)?(?:sleep\s+\d|(?:until|while|for)\b[^\n]*\bdo\b[^\n]*\bsleep\s+\d)")),
)
#: Tools whose call is itself a wait, by tool name: the person's answer to a question (the time is his, not a tool's), a background job's output.
_WAITING_TOOLS = {"AskUserQuestion": "user", "TaskOutput": "job", "BashOutput": "job"}
#: Tools whose input is a command line `_WAITS` reads.
_COMMAND_TOOLS = frozenset({"Bash", "Monitor"})
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
    #: Waiting for him to answer a question (`AskUserQuestion`).
    user: float = 0.0
    #: Waiting for a background job's output.
    job: float = 0.0

    def total(self) -> float:
        return self.agent + self.deploy + self.ci + self.timer + self.user + self.job

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
    #: Seconds of the window in which no run wrote anything for over `DARK_GAP` (counted once, not per run).
    dark: float = 0.0


# --- reading a transcript --------------------------------------------------------------------------------------------------


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
    #: (start, end, wait kind or "") of each tool call; `end` is infinite for one with no result yet.
    spans: list[tuple[float, float, str]]
    #: Child run id -> when the spawn call that named it returned.
    spawned: dict[str, float]
    #: The last turn event (a turn's end or its opening) before the window, which says whether the run began the window parked.
    lead: tuple[float, str] | None = None


def read_transcript(path: Path, since: float) -> Transcript:
    """The events of `path` from `since` on. A missing or unreadable file reads empty."""
    before = None
    events: list[tuple[float, str]] = []
    opened: dict[str, tuple[float, str, bool]] = {}
    spans: list[tuple[float, float, str]] = []
    spawned: dict[str, float] = {}
    try:
        with path.open("rb") as handle:
            for raw in handle:
                line = raw.decode("utf-8", "replace")
                at, kind = _line_at(line), _line_kind(line)
                if at is None or not kind:
                    continue
                if at < since:
                    if kind in TURN_OPENERS or kind in ("done", "error"):
                        before = (at, kind)   # the last turn event before the window, in file order
                    continue
                events.append((at, kind))
                if kind == "tool":
                    try:
                        call = json.loads(line)
                    except ValueError:
                        continue
                    name, text = str(call.get("tool") or ""), str(call.get("tool_input") or "")
                    wait = _WAITING_TOOLS.get(name) or (next((label for label, pattern in _WAITS if pattern.search(text)), "") if name in _COMMAND_TOOLS else "")
                    opened[str(call.get("tool_id") or "")] = (at, wait, _SPAWNS.search(f"{name} {text}") is not None)
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
        # A call with no result yet is open until its turn ends (the next turn event) or, in the last turn, the run's end or now.
        turn_marks = sorted(at for at, kind in events if kind in TURN_OPENERS or kind in ("done", "error"))
        for start, wait, _ in opened.values():
            later = bisect_right(turn_marks, start)
            spans.append((start, turn_marks[later] if later < len(turn_marks) else math.inf, wait))
    except OSError:
        pass
    return Transcript(events=events, spans=spans, spawned=spawned, lead=before)


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


def _minus(spans: list[tuple[float, float, str]], holes: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """The disjoint, sorted `spans` with every part inside the disjoint, sorted `holes` taken out."""
    kept: list[tuple[float, float, str]] = []
    ends = [hole[1] for hole in holes]
    for start, end, kind in spans:
        for hole_start, hole_end, _ in holes[bisect_right(ends, start):]:
            if hole_start >= end:
                break
            if hole_start > start:
                kept.append((start, hole_start, kind))
            start = max(start, hole_end)
            if start >= end:
                break
        if start < end:
            kept.append((start, end, kind))
    return kept


def _within(spans: list[tuple[float, float, str]], low: float, high: float) -> list[tuple[float, float, str]]:
    return [(max(a, low), min(b, high), kind) for a, b, kind in spans if b > low and a < high]


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
    """`row`'s time in `[since, now]` split as the module doc says; `dark` the stretches no run wrote in (`dark_stretches`).

    Four sets of disjoint intervals, each taking from the ones before it: parked, dark, tool calls (by what they wait on); what none of them
    covers is the model's own time, or idle where its gap between two events is over `IDLE_GAP`."""
    start = max(row.started_at, since)
    end = now if row.status in AT_WORK or not row.finished_at else min(row.finished_at, now)
    end = max(end, start)
    if row.status not in AT_WORK:   # an ended run's record time says nothing of the stretch after its last event (a cut, a rewrite)
        end = max(start, min(end, max((at for at, _ in transcript.events if at <= end), default=start)))
    events = [(max(at, start), kind) for at, kind in transcript.events if at <= end]
    if transcript.lead is not None and transcript.lead[0] < start:
        events.insert(0, (start, transcript.lead[1]))   # the window opens inside the turn that event began or ended
    last = max((at for at, _ in events), default=start)
    stretches = _parked(events, end)
    if row.status == "waiting" and end - last > PARKED_AFTER:   # waiting for its next message, by its own record
        stretches.append((last, end, "parked"))
    parked = _within(_disjoint(stretches), start, end)
    dark_set = _minus(_within(list(dark), start, end), parked)
    tools = _minus(_minus(_within(_disjoint(transcript.spans), start, end), parked), dark_set)
    totals: dict[str, float] = defaultdict(float)
    for kind, spans in (("parked", parked), ("dark", dark_set)):
        totals[kind] = sum(b - a for a, b, _ in spans)
    for a, b, kind in tools:
        totals[kind or "tool"] += b - a
    covered = _disjoint([*parked, *dark_set, *tools])
    starts = [c[0] for c in covered]
    openers = {at for at, kind in events if kind in TURN_OPENERS}
    marks = sorted({start, end, *(at for at, _ in events)})
    for a, b in zip(marks, marks[1:]):
        if b <= a:
            continue
        taken, index = 0.0, max(0, bisect_right(starts, a) - 1)
        while index < len(covered) and covered[index][0] < b:
            taken += max(0.0, min(b, covered[index][1]) - max(a, covered[index][0]))
            index += 1
        rest = (b - a) - taken
        if rest > 1e-9:
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
        model=sum(p.model for p in profiles), idle=sum(p.idle for p in profiles), waits=sum((p.waits for p in profiles), Waits()),
        queued_runs=len(queued), queued_total=sum(queued), queued_median=statistics.median(queued) if queued else None,
        queued_max=max(queued) if queued else None)


def dark_stretches(transcripts: Iterable[Transcript], *, since: float, now: float) -> list[tuple[float, float, str]]:
    """The stretches of `[since, now]` longer than `DARK_GAP` in which none of `transcripts` has an event and a later event shows the computer came
    back: the stretch up to `now` is never one (everyone may be waiting on a call, or on him, and the computer is on)."""
    marks = sorted({since, *(at for t in transcripts for at, _ in t.events if since <= at <= now)})
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
                    by_role=ranked(groups), by_conversation=ranked(chats), total=rollup("all", profiles),
                    dark=sum(b - a for a, b, _ in dark))
