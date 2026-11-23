"""`galaius conversation`: what ran in a conversation, how a run ended, counts and spend, this PC's limits.

For a debugging agent (or the person) who would otherwise write a script over the run records. One query engine
(`galaius.agents.run_ledger`) behind every verb; the filters are options, never a command per question. A run asking
reads ITS conversation by default (`--all` for every conversation on this computer); `--json` for another program.
Reads local files only: no server, no run derived, no secret printed (a login shows as its 8-character digest)."""

import json
import sys
import time
from datetime import datetime
from typing import Annotated

from cyclopts import App, Parameter
from pydantic import BaseModel

from galaius.agents import pc_state
from galaius.agents.pc_state import Live
from galaius.agents.run_ledger import GONE, RunLedger, RunQuery, RunRow, Said, Stats, own_conversation, parse_since, stats
from galaius.agents.run_slowness import Slowness, slowness

conversation_app = App(name="conversation", help="Read what ran in a conversation: recent runs, how one ended, counts and spend, limits.")

_Roles = Annotated[list[str] | None, Parameter(name="--role", help="Only runs of this role (repeat for several).")]
_Statuses = Annotated[list[str] | None, Parameter(name="--status", help=f"Only runs in this status (done, failed, running, interrupted, stopped, crashed, waiting, {GONE}).")]
_Models = Annotated[list[str] | None, Parameter(name="--model", help="Only runs whose provider/model contains this text.")]
_Logins = Annotated[list[str] | None, Parameter(name="--login", help="Only runs on the login starting with this digest.")]


def _query(conversation: str | None, since: str | None, all_conversations: bool, role: list[str] | None, status: list[str] | None,
           model: list[str] | None, login: list[str] | None, cut: bool, limit: int | None, *, default_since: str | None) -> tuple[RunQuery, str]:
    """The `RunQuery` the options make, and the line that says what it covers. A bad option is an `ERROR:` line and exit 2."""
    try:
        if all_conversations and conversation:
            raise ValueError("choose --conversation or --all, not both")
        scope = None if all_conversations else conversation or own_conversation()
        wanted = since if since is not None else default_since
        query = RunQuery(conversation=scope, since=parse_since(wanted) if wanted and wanted != "all" else None, roles=frozenset(role or ()),
                         statuses=frozenset(status or ()), models=frozenset(model or ()), logins=frozenset(login or ()), cut=cut, limit=limit)
        covers = f"conversation {scope[:12]}" if scope else "every conversation on this computer" + ("" if all_conversations else " (none found for this process: pass --conversation)")
        return query, covers + (f", since {_when(query.since)}" if query.since else ", all time")
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None


def _rows(ledger: RunLedger, query: RunQuery) -> list[RunRow]:
    try:
        return ledger.rows(query)
    except LookupError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None


def _when(instant: float | None) -> str:
    if not instant:
        return "-"
    moment = datetime.fromtimestamp(instant)
    return moment.strftime("%H:%M" if moment.date() == datetime.now().date() else "%m-%d %H:%M")


def _span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds / 3600:.1f}h" if seconds < 172800 else f"{seconds / 86400:.1f}d"


def _count(number: int) -> str:
    for size, unit in ((10**9, "G"), (10**6, "M"), (10**3, "k")):
        if number >= size:
            return f"{number / size:.1f}{unit}"
    return str(number)


def _depths(rows: list[RunRow]) -> list[tuple[int, RunRow]]:
    """`rows` as a tree, children under their parent (a run whose parent is not among them is a root), siblings by start."""
    present = {row.run_id for row in rows}
    children: dict[str | None, list[RunRow]] = {}
    for row in rows:
        children.setdefault(row.parent if row.parent in present else None, []).append(row)
    ordered: list[tuple[int, RunRow]] = []

    def walk(parent: str | None, depth: int) -> None:
        for row in children.get(parent, ()):
            ordered.append((depth, row))
            walk(row.run_id, depth + 1)

    walk(None, 0)
    return ordered


def _says(row: RunRow, width: int) -> str:
    said = f"cut: {row.cut}" + (f" — {row.said}" if row.said else "") if row.cut else row.said
    return said[:width]


class Recent(BaseModel):
    covers: str
    runs: list[RunRow]


@conversation_app.command
def recent(*, conversation: Annotated[str | None, Parameter(help="A conversation id or prefix, or any run id of it. Default: this process's own.")] = None,
           since: Annotated[str | None, Parameter(help="6h, 30m, 2d, a date or datetime, or `all`. Default 6h.")] = None,
           every: Annotated[bool, Parameter(name="--all", negative=(), help="Every conversation on this computer.")] = False,
           role: _Roles = None, status: _Statuses = None, model: _Models = None, login: _Logins = None,
           cut: Annotated[bool, Parameter(negative=(), help="Only runs a limit or the system cut short.")] = False,
           limit: Annotated[int | None, Parameter(help="The newest N runs.")] = None,
           flat: Annotated[bool, Parameter(negative=(), help="One list by start time, no parent tree.")] = False,
           json_out: Annotated[bool, Parameter(name="--json", negative=(), help="One JSON object: `covers`, `runs`.")] = False) -> None:
    """The runs of a conversation lately, as a tree: start, status, id, role, model, login, length, spend, and the last words
    (or why a limit cut it)."""
    query, covers = _query(conversation, since, every, role, status, model, login, cut, limit, default_since="6h")
    rows = _rows(RunLedger(), query)
    if json_out:
        print(Recent(covers=covers, runs=rows).model_dump_json(indent=1))
        return
    now = time.time()
    working = sum(row.status in ("running", "starting", "waiting") for row in rows)
    print(f"{covers} · {len(rows)} runs · {working} at work")
    for depth, row in ([(0, row) for row in rows] if flat else _depths(rows)):
        spend = f"~${row.cost_usd:.2f}" if row.cost_usd is not None else "-"
        model_name = f"{row.provider}/{row.model}".strip("/")[:30]
        print(f"{_when(row.started_at):>11} {row.status:<11} {row.run_id[:8]}  {'  ' * depth + row.role:<26} {model_name:<30} {(row.login or '-')[:8]:<8} "
              f"{_span(row.seconds(now)):>5} {spend:>9}  {_says(row, 90)}")
    if any(row.status == GONE for row in rows):
        print(f"\n{GONE}: the record says at work, its process is gone (the registry settles it later).")


@conversation_app.command
def last(run_id: list[str], *, chars: Annotated[int, Parameter(help="Clip the report to this many characters; 0 = whole.")] = 4000,
         json_out: Annotated[bool, Parameter(name="--json", negative=(), help="One JSON object per run, one per line.")] = False) -> None:
    """How each run ended and what it said last: its last report (the `done` or `error` event, else its latest words when still at
    work), and the reason when a limit or the system cut it. Takes the short ids the other verbs print."""
    ledger, failed = RunLedger(), False
    for ref in run_id:
        try:
            ending = ledger.last_words(ref, text_chars=None if chars == 0 else chars)
        except LookupError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            failed = True
            continue
        if json_out:
            print(ending.model_dump_json())
            continue
        run = ending.run
        print(f"===== {run.run_id[:8]} {run.role} · {run.status} · {run.provider}/{run.model} · login {(run.login or '-')[:8]} · "
              f"started {_when(run.started_at)} · ran {_span(run.seconds(time.time()))}")
        for label, value in (("cut", run.cut), ("exit code", "" if run.exit_code in (None, 0) else str(run.exit_code)), ("relaunched from", run.relaunched_from),
                             ("successor run", run.successor), ("resumed after cuts", str(run.resumed_cuts) if run.resumed_cuts else "")):
            if value:
                print(f"{label}: {value}")
        if ending.text:
            clipped = f", first {chars}" if chars and ending.chars > chars else ""
            print(f"--- {ending.event} event ({ending.chars} chars{clipped}) ---\n{ending.text}")
        else:
            print(f"--- no report in its transcript; its record ends: {ending.digest or run.said or '(nothing)'}")
    if failed:
        raise SystemExit(2)


def _table(title: str, groups, rows_shown: int = 8) -> None:
    if not groups:
        return
    print(f"{title}:")
    for group in groups[:rows_shown]:
        print(f"  {group.name[:34]:<34} {group.runs:>5} runs  ~${group.cost_usd:>9.2f}  in {_count(group.tokens.input):>6} out {_count(group.tokens.output):>6} cache {_count(group.tokens.cache_read):>6}")
    if len(groups) > rows_shown:
        print(f"  … {len(groups) - rows_shown} more (--json for all)")


class StatsOut(BaseModel):
    covers: str
    stats: Stats
    live: Live | None = None


@conversation_app.command(name="stats")
def stats_command(*, conversation: Annotated[str | None, Parameter(help="A conversation id or prefix, or any run id of it. Default: this process's own.")] = None,
                  since: Annotated[str | None, Parameter(help="6h, 30m, 2d, a date or datetime, or `all`. Default all time.")] = None,
                  every: Annotated[bool, Parameter(name="--all", negative=(), help="Every conversation on this computer.")] = False,
                  role: _Roles = None, status: _Statuses = None, model: _Models = None, login: _Logins = None,
                  live: Annotated[bool, Parameter(negative=(), help="Add this PC now: places taken vs max, the runs waiting and for how long, the agents' memory.")] = False,
                  json_out: Annotated[bool, Parameter(name="--json", negative=(), help="One JSON object: `covers`, `stats`, `live`.")] = False) -> None:
    """Counts by status, role, model and login; tokens and spend; runs cut by a limit; relaunches. Spend is the record's own
    figure (a subscription's turns carry an API-equivalent price): runs with none are counted apart."""
    query, covers = _query(conversation, since, every, role, status, model, login, False, None, default_since=None)
    done, state = stats(_rows(RunLedger(), query)), pc_state.live() if live else None
    if json_out:
        print(StatsOut(covers=covers, stats=done, live=state).model_dump_json(indent=1))
        return
    print(f"{covers} · {done.runs} runs" + (f" · {_when(done.first_at)} to {_when(done.last_at)}" if done.runs else ""))
    print(f"spend ~${done.cost_usd:.2f} (no price on record for {done.unpriced} runs) · tokens in {_count(done.tokens.input)}, out {_count(done.tokens.output)}, "
          f"cache read {_count(done.tokens.cache_read)}, cache write {_count(done.tokens.cache_write)}")
    _table("status", done.by_status)
    _table("role", done.by_role)
    _table("model", done.by_model)
    _table("login", done.by_login)
    if done.cuts or done.relaunched or done.resumed:
        print("cut: " + ", ".join(f"{reason} {count}" for reason, count in done.cuts.items()) + f" · relaunched on another model {done.relaunched} · resumed after a cut {done.resumed}")
    if state is not None:
        print(f"this PC now: {state.running} runs at work, {state.places_taken}/{state.places_max} places taken, {len(state.waiting)} waiting, {len(state.failed)} failed to start")
        for kind, entries in (("waiting", state.waiting), ("failed", state.failed)):
            for held in entries:
                print(f"  {kind} {held['waited_seconds'] // 60}m {held['run_id']} {held['role']} {held['said']}")
        if state.memory_current is not None:
            print(f"  agents memory {state.memory_current / 2**30:.1f} GiB of " + (f"{state.memory_max / 2**30:.1f} GiB" if state.memory_max else "no max"))


@conversation_app.command
def limits(*, json_out: Annotated[bool, Parameter(name="--json", negative=(), help="A JSON list, one object per login.")] = False) -> None:
    """This PC's subscription logins: each window's usage reading and reset, the model-family pools, and what this PC passes over
    (a paused login, a blocked model) until when. Local files only; a login shows as its digest, never an account name."""
    logins = pc_state.limits()
    if json_out:
        print(json.dumps([login.model_dump() for login in logins], indent=1))
        return
    if not logins:
        print("No subscription login on this computer.")
    quiet = [login for login in logins if not (login.windows or login.pools or login.paused or login.blocked)]
    for login in (login for login in logins if login not in quiet):
        print(f"{login.provider} · subscription {login.subscription} · login {login.login or '-'}")
        for reading in login.windows:
            print(f"  {reading['window']:<10} {reading['used_percent']:>5}%  resets {_stamp(reading['resets_at'])}")
        for reading in login.pools:
            print(f"  pool {reading['pool']:<5} {reading['used_percent']:>5}%  resets {_stamp(reading['resets_at'])}")
        for pause in login.paused:
            print(f"  PAUSED {pause['reason']} until {_stamp(pause['until'])} {pause['said']}")
        for block in login.blocked:
            print(f"  passed over: {block['scope']} until {_stamp(block['until'])} ({block['window'] or 'window unnamed'}, {block['refusals']} refusals)")
    if quiet:
        print("no reading, nothing passed over: " + ", ".join(f"{login.provider} {login.subscription}" for login in quiet))


def _stamp(iso: str | None) -> str:
    return "-" if not iso else _when(datetime.fromisoformat(iso).timestamp())


class MessagesOut(BaseModel):
    covers: str
    messages: list[Said]


@conversation_app.command
def messages(*, conversation: Annotated[str | None, Parameter(help="A conversation id or prefix, or any run id of it. Default: every conversation.")] = None,
             since: Annotated[str, Parameter(help="6h, 30m, 2d, a date or datetime, or `all`. Default 24h.")] = "24h",
             frustrated: Annotated[bool, Parameter(negative=(), help="Only messages holding his frustration wording (the coordinator's gate pattern).")] = False,
             chars: Annotated[int, Parameter(help="Clip each message to this many characters; 0 = whole.")] = 240,
             json_out: Annotated[bool, Parameter(name="--json", negative=(), help="One JSON object: `covers`, `messages`.")] = False) -> None:
    """The messages he typed in the web app lately, oldest first: time, conversation, the run it reached, his words. `!` marks his
    frustration wording. What he types in an editor or terminal session is in that CLI's own transcript (`galaius agents search`)."""
    try:
        moment = None if since == "all" else parse_since(since)
        found = RunLedger().his_messages(since=moment or 0.0, conversation=conversation, frustrated=frustrated)
    except (ValueError, LookupError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    covers = (f"conversation {conversation[:12]}" if conversation else "every conversation") + (f", since {_when(moment)}" if moment else ", all time")
    if json_out:
        print(MessagesOut(covers=covers, messages=found).model_dump_json(indent=1))
        return
    print(f"{covers} · {len(found)} messages" + (f" · {sum(bool(said.frustrated) for said in found)} frustrated" if found else ""))
    for said in found:
        text = " ".join(said.text.split())
        print(f"{_when(said.at):>11} {said.conversation[:12]:<12} → {said.role:<18} {'!' if said.frustrated else ' '} {text if not chars else text[:chars]}")


class SlowOut(BaseModel):
    covers: str
    slowness: Slowness


def _split(profile) -> str:
    waits = profile.waits
    return (f"tools {_span(profile.tools)} · model {_span(profile.model)} · waits {_span(waits.total())} "
            f"(agents {_span(waits.agent)}, deploy {_span(waits.deploy)}, ci {_span(waits.ci)}, timer {_span(waits.timer)}) · idle {_span(profile.idle)} · dark {_span(profile.dark)}")


@conversation_app.command
def slow(*, conversation: Annotated[str | None, Parameter(help="A conversation id or prefix, or any run id of it. Default: this process's own.")] = None,
         since: Annotated[str | None, Parameter(help="6h, 30m, 2d, a date or datetime. Default 24h.")] = None,
         every: Annotated[bool, Parameter(name="--all", negative=(), help="Every conversation on this computer.")] = False,
         role: _Roles = None, status: _Statuses = None, model: _Models = None, login: _Logins = None,
         top: Annotated[int, Parameter(help="How many of the longest runs to list.")] = 8,
         json_out: Annotated[bool, Parameter(name="--json", negative=(), help="One JSON object: `covers`, `slowness`.")] = False) -> None:
    """Why it is slow: where the time of each run in the window went, from its transcript. Per run, role and conversation:
    parked (between turns), waiting (on agents, deploys, CI, timers), tools, model, idle (this run silent > 2 min outside a tool call),
    dark (no run wrote for 15 min: the computer off, or everyone waiting on him), and the time a spawned run was queued before it started."""
    query, covers = _query(conversation, since, every, role, status, model, login, False, None, default_since="24h")
    ledger = RunLedger()
    rows = _rows(ledger, query)
    start = query.since or 0.0
    report = slowness(ledger, rows, since=start, top=max(1, top))
    if json_out:
        print(SlowOut(covers=covers, slowness=report).model_dump_json(indent=1))
        return
    total = report.total
    print(f"{covers} · {total.runs} runs · active {_span(total.active)} (parked and dark time excluded)")
    print(f"  {_split_rollup(total)}")
    if total.queued_runs:
        print(f"  queued before start: {total.queued_runs} runs, total {_span(total.queued_total)}, median {_span(total.queued_median or 0)}, max {_span(total.queued_max or 0)}")
    else:
        print("  queued before start: no spawn call found for these runs in their parents' transcripts")
    print("longest (active time):")
    for profile in report.longest:
        queued = "" if profile.queued is None else f" · queued {_span(profile.queued)}"
        print(f"  {profile.run_id[:8]} {profile.role:<20} {profile.status:<11} {_span(profile.active()):>6} · {_split(profile)}{queued}")
    for title, rollups in (("by role", report.by_role), ("by conversation", report.by_conversation if every or not query.conversation else [])):
        if rollups:
            print(f"{title}:")
            for item in rollups[:8]:
                print(f"  {item.name[:22]:<22} {item.runs:>4} runs · active {_span(item.active):>6} · {_split_rollup(item)}"
                      + (f" · queued median {_span(item.queued_median)}" if item.queued_median is not None else ""))


def _split_rollup(item) -> str:
    waits = item.waits
    return (f"tools {_span(item.tools)} · model {_span(item.model)} · waits {_span(waits.total())} "
            f"(agents {_span(waits.agent)}, deploy {_span(waits.deploy)}, ci {_span(waits.ci)}, timer {_span(waits.timer)}) · idle {_span(item.idle)} · dark {_span(item.dark)}")
