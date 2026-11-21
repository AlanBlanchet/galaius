"""`agents list`, `events`, `wait`, `stop`, `clear`, `console`, `work-for` and `require-role`: the runs on this PC and who they work for."""

import os
import sys
import threading
import traceback
from pathlib import Path
from uuid import UUID
from galaius.machine.peer.client import PeerClient, PeerError, PeerUnavailable
from galaius.machine.peer.words import pick
from galaius_core import ASKED_BY_MAX
from galaius.agents import messaging
from galaius.agents.run_ledger import AT_WORK, RunLedger, RunQuery, UnknownConversation, own_conversation, parse_since
from galaius.agents.turns import handback
from galaius.agents import registry as reg
from galaius.agents.catalog import community as community_agents
from galaius.agents.host import run_console
from galaius.agents.providers import (
    available_providers,
)
from galaius.agents.spawn_line import SpawnLine
from galaius.cli.commands.agents.app import agents_app



@agents_app.command(name="console")
def agents_console(workspace_root: Path) -> None:
    """Run the extension's private newline-JSON conversation console on standard I/O."""

    run_console(workspace_root)


#: How long `agents list` waits for the registry before saying where it is stuck (a record lock another process holds).
LIST_PATIENCE = 20.0


#: The registry's functions that wait for a lock held by another process: on a stuck stack, they are the evidence of a lock.
_LOCK_WAITS = frozenset({"record_lock", "exclusive", "_pinned_registry", "flock", "lockf", "acquire", "_exclusive", "locked"})


def _within(patience: float, work, what: str):
    """`work()` run on a thread, its result returned; past `patience` seconds, an `ERROR:` line saying `what` and the function it
    is in (the reason: a lock another process holds, or slow work), exit 1. The registry's locks wait for their holder with no end of their own: a listing hung
    behind one for half an hour (2026-11-16) and said nothing."""
    outcome: dict = {}

    def run() -> None:
        try:
            outcome["value"] = work()
        except BaseException as error:  # noqa: BLE001 - carried to the caller's thread, raised there
            outcome["error"] = error

    worker = threading.Thread(target=run, daemon=True, name="agents-list")
    worker.start()
    worker.join(patience)
    if worker.is_alive():
        frame = sys._current_frames().get(worker.ident)
        stack = traceback.extract_stack(frame) if frame is not None else []
        locked = next((entry for entry in reversed(stack) if entry.name in _LOCK_WAITS), None)
        where = f"{stack[-1].name} ({Path(stack[-1].filename).name}:{stack[-1].lineno})" if stack else "unknown"
        cause = (f"waiting on a lock another galaius process holds (`{locked.name}`, {Path(locked.filename).name}:{locked.lineno})" if locked is not None
                 else f"still working in {where}, no lock on its stack: slow, not stuck; narrow it with --since or --limit")
        print(f"ERROR: {what} took more than {patience:g} s: {cause}. `galaius conversation recent` reads the same runs without any lock.", file=sys.stderr)
        raise SystemExit(1)
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


@agents_app.command(name="list")
def agents_list(foreign: bool = False, session_id: str | None = None, all_sessions: bool = False, since: str = "24h", limit: int = 100) -> None:
    """Show launched runs belonging to this conversation, including nested children (those at work always; the others that moved within --since).

    Parameters
    ----------
    foreign
        Include discovered editor sessions only with --all-sessions.
    session_id
        Conversation to list (an id, or any run id of it); defaults to the calling run's own conversation (its parent
        chain, GALAIUS_SESSION_ID, or the current CLI harness CODEX_THREAD_ID). Never inferred from cwd or PID.
    all_sessions
        Explicitly show machine-wide history, including runs with unknown owners.
    since
        Only runs that moved this recently (`6h`, `2d`, a date) or `all`; default 24h, so a conversation of thousands of runs
        lists the ones at work now. `galaius conversation recent` is the richer view.
    limit
        Every run at work and this many of the others, the newest (0: all; each costs ~20 ms to read); the older ones are counted.
    """

    try:
        if all_sessions:
            if session_id is not None:
                raise ValueError("Choose session_id or all_sessions, not both")
            identity = None
            runs, older = _within(LIST_PATIENCE, lambda: _runs_of(RunQuery(since=_since(since)), limit), "listing every run")
            runs += reg.foreign_runs() if foreign else []
        else:
            identity, runs, older = _within(LIST_PATIENCE, lambda: _conversation_runs(session_id, since, limit), "listing the conversation's runs")
    except (ValueError, LookupError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print("All sessions (including unknown owners)" if all_sessions else f"Session: {identity}")
    line = SpawnLine.here()
    places, waiting = line.session(None if all_sessions else identity), line.waiting_said()
    for place, said in places:
        state = "failed" if place.state == "failed" else "queued"
        print(f"  {state:8} {reg.short_id(place.run_id)}  {'-':7} {place.label():16} {'—':>10}  {said}")
    if not runs and not places:
        names = ", ".join(p.name for p in available_providers()) or "none installed"
        print(f"No agent runs. Providers available here: {names}.")
        return
    for run in runs:
        cost = f"~${run.cost_usd:.4f}" if run.cost_usd is not None else "—"
        mark = "*" if run.foreign else " "
        said = run.interruption.describe() if run.interruption else waiting.get(run.run_id, run.last)
        print(f"{mark} {run.status:8} {reg.short_id(run.run_id)}  {run.provider:7} {run.name:16} "
              f"{cost:>10}  {said[:60]}")
    if older:
        print(f"\n… {older} older runs not shown: --limit 0 for all, or `galaius conversation recent --since 7d`.")
    if any(r.foreign for r in runs):
        print("\n* not started by galaius")


def _since(since: str) -> float | None:
    return None if since == "all" else parse_since(since)


def _runs_of(query: RunQuery, limit: int) -> tuple[list[reg.AgentRun], int]:
    """The runs `query` keeps (`RunLedger`): every one at work and the `limit` newest of the others (0: all), each read alone from its record
    with its status re-checked (`registry.current_of`, ~20 ms a run), and how many older ones are not shown."""
    rows = RunLedger().rows(query)
    others = [row for row in rows if row.status not in AT_WORK]
    shown = {row.run_id for row in rows if row.status in AT_WORK} | {row.run_id for row in (others[-limit:] if limit else others)}
    return [run for row in rows if row.run_id in shown and (run := reg.current_of(row.run_id)) is not None], len(rows) - len(shown)


def _conversation_runs(session_id: str | None, since: str, limit: int) -> tuple[str, list[reg.AgentRun], int]:
    """The conversation asked (else the caller's own) and its runs that moved within `since`. A conversation is what the run ledger
    says (`run_ledger`): a session's runs and every run descended from its root, which the registry's session filter missed for a root
    run that never had a session id (« Session identity required »). An id the caller's harness set that no record has yet (a
    thread before its first spawn) is a conversation with no runs, as before."""
    ledger = RunLedger()
    inherited = session_id is None and (session_id := reg.resolve_session_id(None, cli_harness=True)) is not None
    if session_id is not None:
        try:
            conversation = ledger.resolve_conversation(session_id)
        except UnknownConversation:
            if not inherited:
                raise
            return session_id, [], 0
    elif (conversation := own_conversation()) is None:
        raise ValueError("Session identity required: run this inside an agent run, pass --session-id, or set GALAIUS_SESSION_ID; use --all-sessions for every run")
    return (conversation, *_runs_of(RunQuery(conversation=conversation, since=_since(since)), limit))


@agents_app.command(name="events")
def agents_events(run_id: str, limit: int = 30, machine: str | None = None) -> None:
    """Print what an agent run has been doing. Takes the short id `agents list` prints; with
    --machine, the full id of a run you placed on that PC (`agents spawn --machine`)."""

    if machine is not None:
        try:
            client = PeerClient()
            events = client.events(pick(client.fleet(), machine).id, UUID(run_id)).events
        except (ValueError, PeerError, PeerUnavailable) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            raise SystemExit(2) from None
        for line in events.items[-max(1, limit):]:
            print(f"  {line.kind:11} {line.text}")
        return
    resolved = reg.resolve_run_id(run_id) or run_id
    events = reg.read_events(resolved)
    if not events:
        waiting = SpawnLine.here().describe(run_id)
        print(f"  {'queued':11} {waiting}" if waiting else f"No events for {run_id!r}.")
        return
    shown = events[-max(1, limit):]
    print(community_agents.shown(reg.get_run(resolved), "\n".join(f"  {event.kind:11} {event.summary(viewer=resolved)}" for event in shown)))
    if report := handback.read_inline(resolved, shown):
        print(f"\n{report}")


@agents_app.command(name="wait")
def agents_wait(run_ids: list[str], timeout: float = 600.0) -> None:
    """Wait until every named run has ended (or --timeout seconds), then print each one's final report. Exit 1 when one
    is still at work at the timeout (its id said, and how long it has been silent), 2 for an id naming no run, 3 when a
    message for the run waiting (`GALAIUS_RUN_ID`) arrived first: it reaches that run at its next step."""
    try:
        waited = handback.wait(run_ids, timeout=timeout, asker=os.environ.get("GALAIUS_RUN_ID"))
    except LookupError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print("\n\n".join(report.section(limit=None) for report in waited.reports if not report.pending))
    pending = waited.still_at_work()
    if waited.message is not None:
        print(f"A message for you arrived from {waited.message.writer}: wait cut short. Still at work: {', '.join(pending)}")
        raise SystemExit(3)
    if pending:
        print(f"Still at work after {timeout:g} s: {', '.join(pending)}")
        raise SystemExit(1)


@agents_app.command(name="stop")
def agents_stop(run_id: str) -> None:
    """Stop a running agent and the tools it spawned. Takes the short id `agents list` prints."""

    resolved = reg.resolve_run_id(run_id)
    if resolved is None:
        cancelled = SpawnLine.here().cancel(run_id)
        print(f"Taken out of the line: {cancelled[:8]} never started." if cancelled else f"No agent run {run_id!r}.")
        return
    print(f"Stopped {resolved[:8]}." if reg.stop(resolved, by=reg.caller_stop_actor(os.environ)) else f"No agent run {run_id!r}.")


@agents_app.command(name="work-for")
def agents_work_for(run_id: str) -> None:
    """Say the agent running this command works for run RUN_ID: its record lists RUN_ID among the runs that asked it
    (`asked_by`), as a message RUN_ID sent it would. Only an agent's own record: run from inside an agent run."""
    caller, asker = os.environ.get("GALAIUS_RUN_ID") or "", reg.resolve_run_id(run_id)
    if reg.get_run(caller) is None:
        print("ERROR: work-for names who the running agent works for; run it from inside an agent run (GALAIUS_RUN_ID).")
        raise SystemExit(1)
    if asker is None:
        print(f"ERROR: no agent run {run_id!r}.")
        raise SystemExit(1)
    with reg.record_lock(caller):
        reg.note_asker_locked(caller, asker)
        run = reg.get_run(caller)
    if run is None or asker not in run.askers:
        print(f"ERROR: {caller[:8]} does not list {asker[:8]}: it is itself, a run that works for it, or it lists {ASKED_BY_MAX} runs already.")
        raise SystemExit(1)
    print(f"{caller[:8]} works for {asker[:8]}.")


@agents_app.command(name="require-role")
def agents_require_role(role: str) -> None:
    """Exit 0 when the process asking is a run of ROLE or the person at a terminal; else say who asks in one line and exit 3.
    A script reserving an act to one role runs it first: only the deployer ships (`deploy.sh`; owner 2026-11-08 « it's only
    the service for the agent that's supposed to deploy that's deploying, right? »). Who asks is read from the process
    (`messaging.Asker`), never from a variable: an agent outside galaius's runs and a `systemd-run` unit are no person."""
    asker = messaging.Asker.here()
    if asker.person or asker.role == role:
        return
    print(f"REFUSED: only the {role} does this, and {asker.said()} asks; hand it to the {role} instead.", file=sys.stderr)
    raise SystemExit(3)


@agents_app.command(name="clear")
def agents_clear(run_id: str | None = None) -> None:
    """Forget finished agent runs — all of them, or one by the short id `agents list` prints.

    A running agent is never forgotten: its record is the only handle on the process.
    """

    if run_id:
        print(f"Forgot {run_id}." if reg.forget(run_id)
              else f"Kept {run_id!r} — unknown, or still running.")
        return
    cleared = reg.clear_finished()
    print(f"Forgot {len(cleared)} finished run(s)." if cleared else "Nothing finished to clear.")
