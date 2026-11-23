"""`galaius run <id>`: one run described from its record: how it ended, why that model, its tree, its queue, its last report, whether its commit is live.

For a debugging agent (or the person) who would otherwise read the run's record, ledger, transcript and process table by hand. Local files, one
request to the app only to judge a commit the report names; `--json` for another program. A run asking about itself passes no id."""

import os
import sys
import time
from datetime import datetime
from typing import Annotated

from cyclopts import Parameter

from galaius.agents.run_describe import Branch, RunDescription, describe
from galaius.agents.run_ledger import RunLedger
from galaius.diagnostics import health


def _when(instant: float | None) -> str:
    return health.clock(instant) if instant and datetime.fromtimestamp(instant).date() != datetime.now().date() else (datetime.fromtimestamp(instant).strftime("%H:%M") if instant else "-")


def _live_build() -> str | None:
    """The build the app serves, asked once and only when a commit needs it; None when the app does not say (the slice then reads `unknown`)."""
    build = health.app(time.time()).facts.get("build_id")
    return str(build) if build else None


def _tree(branch: Branch, now: float) -> str:
    return f"  {'  ' * branch.depth}{branch.run_id[:8]} {branch.role:<18} {branch.status:<11} {branch.model[:30]:<30} {_when(branch.started_at):>11}  {branch.said}"


def run(run_id: Annotated[str | None, Parameter(help="The run's id or its first characters (as `agents list` prints it). Default: the run asking (GALAIUS_RUN_ID).")] = None, *,
        chars: Annotated[int, Parameter(help="Clip the last report to this many characters; 0 = whole.")] = 1500,
        slice_check: Annotated[bool, Parameter(name="--slice", negative=("--no-slice",), help="Judge the commits its report names against the build the app serves (one request).")] = True,
        json_out: Annotated[bool, Parameter(name="--json", negative=(), help="One JSON object.")] = False) -> None:
    """How a run ended and why, from its record: status and process, the model it got and why (candidates, what was passed over, a limit that
    refused it), the reason it was cut or stopped, its parent and children, the messages its queue holds with their errors, its last report,
    and whether a commit that report names is live in the app's build. Exits 2 for an id no run has."""
    ref = run_id or os.environ.get("GALAIUS_RUN_ID")
    if not ref:
        print("ERROR: name a run (the id `galaius conversation recent` prints), or run this from inside one.", file=sys.stderr)
        raise SystemExit(2)
    ledger = RunLedger()
    try:
        report = describe(ref, ledger, report_chars=None if chars == 0 else chars, live_build=_live_build if slice_check else None)
    except LookupError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    if json_out:
        print(report.model_dump_json(indent=1))
        return
    _print(report, chars)


def _print(report: RunDescription, chars: int) -> None:
    now, row = time.time(), report.run
    print(f"{row.run_id} {row.role} · {row.status} · {row.provider}/{row.model} · login {(row.login or '-')[:8]} · started {_when(row.started_at)} · ran {health.span(row.seconds(now))}")
    if row.task:
        print(f"task: {row.task}")
    where = f"cwd {report.cwd}" if report.cwd else ""
    process = "" if report.pid is None or report.process_alive is None else f"pid {report.pid} {'running' if report.process_alive else 'gone'}"
    print(" · ".join(part for part in (report.kind, report.permission_mode, process, f"unit {report.unit}" if report.unit else "", where) if part))
    if report.context_tokens:
        print(f"context: {report.context_tokens} of {report.context_window or '?'} tokens")
    why = report.why_model
    pick = (f"candidate #{why.chosen_rank} of {why.candidates}" if why.chosen_rank is not None else f"none of the {why.candidates} candidates (pinned, moved or relaunched)" if why.candidates else "no candidate list kept")
    print(f"model: {why.chosen} — asked {why.requested_model or why.criterion or 'nothing'}{f' (reasoning {why.reasoning})' if why.reasoning else ''}; {pick}" + (f"; top: {', '.join(why.top)}" if why.top and why.chosen_rank != 0 else ""))
    for skipped in why.passed:
        print(f"  passed over {skipped.model}: {skipped.reason}{f' until {_when(skipped.until)}' if skipped.until else ''} {skipped.said}".rstrip())
    if why.fell_back_from:
        print(f"  a limit refused its last turn on {why.fell_back_from}{f' (pool {why.fell_back_pool})' if why.fell_back_pool else ''}{f', reopens {_when(why.fell_back_resets_at)}' if why.fell_back_resets_at else ''}: it ran on the model above")
    end = report.ended
    for label, value in (("cut", f"{end.cut}{f' ({end.cut_source}' + (f' at {_when(end.cut_at)}' if end.cut_at else '') + ')' if end.cut_source else ''}" if end.cut else ""),
                         ("exit code", "" if end.exit_code in (None, 0) else str(end.exit_code)), ("stopped by", end.stopped_by), ("relaunched from", end.relaunched_from),
                         ("relaunch refused", end.relaunch_refused), ("successor run", end.successor), ("resumed after cuts", str(end.resumed_cuts) if end.resumed_cuts else "")):
        if value:
            print(f"{label}: {value}")
    if report.ancestors:
        print("parents: " + " > ".join(f"{each.run_id[:8]} {each.role} ({each.status})" for each in report.ancestors))
    if report.tree:
        print(f"children ({len(report.tree)}):")
        for branch in report.tree:
            print(_tree(branch, now))
    queue = report.queue
    if queue.counts:
        print("queue: " + ", ".join(f"{state} {count}" for state, count in queue.counts.items()))
        for entry in queue.unsettled:
            print(f"  {entry.state:<9} {_when(entry.enqueued_at)} from {entry.sender}{f' — {entry.error}' if entry.error else ''}")
        if queue.more:
            print(f"  … {queue.more} earlier unsettled (--json for all shown)")
    for each in report.slices:
        print(f"slice: {each.commit} in {each.repository}: {each.verdict.upper()} — {each.why}")
    ending = report.report
    if ending.text:
        clipped = f", first {chars}" if chars and ending.chars > chars else ""
        print(f"--- {ending.event} event ({ending.chars} chars{clipped}) ---\n{ending.text}")
    else:
        print(f"--- no report in its transcript; its record ends: {ending.digest or row.said or '(nothing)'}")
