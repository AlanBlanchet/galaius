"""Agent runs from the terminal: spawn, run, resume, send, stop, watch."""

import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Annotated
from uuid import UUID
from cyclopts import Parameter
from galaius.machine.peer.client import PeerClient, PeerError, PeerUnavailable
from galaius.machine.peer.words import pick, place
from galaius_core import refusal_of, ASKED_BY_MAX, IMAGE_TYPES, Coded
from galaius.agents import messaging
from galaius.agents import agent_queue
from galaius.agents.turns import handback
from galaius.agents.catalog import relations
from galaius.agents import providers as agent_providers
from galaius.agents import registry as reg
from galaius.agents.catalog import community as community_agents
from galaius.agents.catalog.community import CommunityAgent
from galaius.agents.conversations import ConversationAnswers
from galaius.agents.catalog.entries import AgentCatalog
from galaius.agents.catalog.connection import CatalogConnectionError
from galaius.isolation.fence import FenceSpec
from galaius.agents.host import run_console
from galaius.agents.providers import (
    available_providers,
    provider_for,
)
from galaius.agents.run import run_agent
from galaius.agents.spawn_line import Admission, Continued, Spawn, SpawnLine
from galaius.cli.commands.agents.app import agents_app, said_refused
from galaius.cli.commands.agents.providers import _provider_modes


@agents_app.command(name="turn-end", show=False)
def agents_turn_end(*argv: str, export_option: Annotated[str, Parameter(name="--export-option")]) -> None:
    """Run one CLI turn whose stream marks no end and write its end as one line (`providers.turn_end`)."""
    raise SystemExit(agent_providers.turn_end(list(argv), export_option))


@agents_app.command(name="console")
def agents_console(workspace_root: Path) -> None:
    """Run the extension's private newline-JSON conversation console on standard I/O."""

    run_console(workspace_root)


@agents_app.command(name="list")
def agents_list(foreign: bool = False, session_id: str | None = None, all_sessions: bool = False) -> None:
    """Show launched runs belonging to this conversation, including nested children.

    Parameters
    ----------
    foreign
        Include discovered editor sessions only with --all-sessions.
    session_id
        Caller conversation used at spawn; defaults to recorded parent, GALAIUS_SESSION_ID,
        or the current CLI harness CODEX_THREAD_ID. Never inferred from cwd or PID.
    all_sessions
        Explicitly show machine-wide history, including runs with unknown owners.
    """

    try:
        identity = session_id if all_sessions else reg.resolve_session_id(session_id, cli_harness=True)
        runs = reg.session_runs(session_id=identity, all_sessions=all_sessions, include_foreign=foreign)
    except ValueError as error:
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
    if any(r.foreign for r in runs):
        print("\n* not started by galaius")


def _spawn_on_machine(machine: str, project: str | None, folder: str | None, task: str, *, agent: str | None, provider: str | None, model: str | None, permission: str | None) -> None:
    """`agents spawn --machine`: the run id of an agent started on another PC of yours (through this PC's own link, no second sign-in), or one line why not."""
    try:
        if project is None:
            raise ValueError("--machine needs --project: the project (name or id) the run works on there")
        print(place(PeerClient(), machine, project, task, role=agent, path=folder, permission=permission, provider=provider, model=model))
    except (ValueError, PeerError, PeerUnavailable) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None


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


@agents_app.command(name="search")
def agents_search(query: str, limit: int = 20, project: str | None = None, since: str | None = None, every_project: bool = False) -> None:
    """Search the conversations this computer holds (galaius's runs and each agent CLI's saved
    sessions) for every word of QUERY, as the `conversation_search` tool does, in the same scope."""

    answers = ConversationAnswers(environment=dict(os.environ), cwd=os.getcwd(), read_call="galaius agents read {id} --around {line}")
    print(answers.search(query, limit, project, since, every_project))


@agents_app.command(name="read")
def agents_read(conversation: str, around: int | None = None, limit: int = 50, every_project: bool = False) -> None:
    """Read one conversation `agents search` found, LIMIT lines around line AROUND (else its last), as
    the `conversation_read` tool does."""

    print(ConversationAnswers(environment=dict(os.environ), cwd=os.getcwd()).read(conversation, around, limit, every_project))


@agents_app.command(name="stop")
def agents_stop(run_id: str) -> None:
    """Stop a running agent and the tools it spawned. Takes the short id `agents list` prints."""

    resolved = reg.resolve_run_id(run_id)
    if resolved is None:
        cancelled = SpawnLine.here().cancel(run_id)
        print(f"Taken out of the line: {cancelled[:8]} never started." if cancelled else f"No agent run {run_id!r}.")
        return
    print(f"Stopped {resolved[:8]}." if reg.stop(resolved, by=reg.caller_stop_actor(os.environ)) else f"No agent run {run_id!r}.")


@agents_app.command(name="retract")
def agents_retract(run_id: str, message_id: str) -> None:
    """Take back message MESSAGE_ID its person sent run RUN_ID: never read when it still waits; its turn stopped when an
    agent is reading it; the run's next turn told. Prints `Withdrawn: <unread|stopped|after|unknown>.` Only the person
    takes a message back: an agent asking is refused."""

    by = reg.caller_stop_actor(os.environ)
    if by.kind == "agent":
        print("ERROR: only the person who sent a message takes it back.", file=sys.stderr)
        raise SystemExit(1)
    print(f"Withdrawn: {messaging.retract(run_id, message_id, by=by)}.")


@agents_app.command(name="send")
def agents_send(run_id: str, message: str, *, fyi: Annotated[bool, Parameter(name="--fyi")] = False,
                cross_project: Annotated[bool, Parameter(name="--cross-project")] = False,
                image_paths: Annotated[list[Path] | None, Parameter(name="--image")] = None,
                message_id: Annotated[str | None, Parameter(name="--message-id")] = None) -> None:
    """Send a message to a running agent — it answers with its full context intact.

    How the VS Code panel lets you join the conversation instead of only watching: the agent
    resumes its own session, so it remembers what it already did and its reply lands in the same
    transcript the panel shows.

    --fyi: a one-way note (a report, an ack, a heads-up): it reads it, and its turns never report back to you for it.
    --cross-project: the person's deliberate act, at his own terminal (never from an agent run's process): a run working
    for another project than the sending run's is sent to anyway (`relations.foreign_project` refuses it otherwise).
    --image PATH (repeatable): an image the message shows, kept with the run it reaches; the agent sees it (an image
    input when its CLI takes one, else a line naming its file). MESSAGE may then be "".
    --message-id ID: the one id the message keeps end to end (the sender's own, e.g. a web outbox row): its transcript
    entry is recorded under it, and the same id sent again records and enqueues nothing.
    """
    if message_id is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", message_id):
        print("ERROR: --message-id takes 1 to 160 letters, digits, '.', '_', ':' or '-'.")
        raise SystemExit(1)


    if reg.resolve_run_id(run_id) is None and (waiting := SpawnLine.here().describe(run_id)) is not None:
        print(f"ERROR: {run_id} has not started ({waiting}); send once it runs.")
        raise SystemExit(1)
    if (refused := messaging.send_refusal(run_id, cross_project=cross_project)) is not None:
        print(f"ERROR: {refused}")
        raise SystemExit(1)
    try:   # kept with the run the message reaches (`relations.live`), which opens them
        media = reg.RunMedia.keep_files(relations.live(target), image_paths) if image_paths and (target := reg.resolve_run_id(run_id)) else ()
    except (OSError, ValueError) as error:
        print(f"ERROR: --image takes an image file ({', '.join(kind.removeprefix('image/').upper() for _, kind, _ in IMAGE_TYPES)}): {error}")
        raise SystemExit(1)
    try:
        # A person's send waits a bounded time for the run (`messaging.PERSON_PATIENCE`) and watches its turn start without
        # its lock: the web runs this command and gives it 60 s, past which it tells him « send timed out » with no reason
        # (2026-10-26: his run's record lock was wedged since 09:35).
        delivery = messaging.deliver_message(run_id, message, message_id=message_id, turn=agent_queue.MessageTurn(fyi=fyi, media=media))
        refused = messaging.wait_for_start(delivery)
    except reg.RecordBusy as busy:
        print(f"ERROR: {busy}; the message was not recorded, send it again.")
        raise SystemExit(1) from None
    if refused is not None:
        print(f"ERROR: the resumed turn did not start: {refused}")
        raise SystemExit(1)
    waiting = SpawnLine.here().waits(delivery.run_id) if delivery.state == "queued" else None
    if delivery.state == "error":
        said_refused(delivery.text, delivery.refusal)
        raise SystemExit(1)
    print(f"{delivery.text} Its turn: {waiting}" if waiting else delivery.text)


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


@agents_app.command(name="compact")
def agents_compact(run_id: str) -> None:
    """Compact an agent's conversation: its CLI replaces what was said so far with its own summary,
    freeing its context. A turn of its own: a run at work compacts once its turn ends."""

    delivery = messaging.deliver_message(run_id, "", turn=agent_queue.CompactionTurn())
    if (refused := messaging.wait_for_start(delivery)) is not None:
        print(f"ERROR: the compaction did not start: {refused}")
        raise SystemExit(1)
    print(delivery.text)
    if delivery.state == "error":
        raise SystemExit(1)


@agents_app.command(name="resume")
def agents_resume(
    run_id: list[str] | None = None, *, all_cut: bool = False, hours: float = 24.0,
) -> None:
    """Pick up agents whose run was CUT — a usage limit, an OOM kill, a reboot, a closed laptop.

    With no argument it only LISTS them (a cut run is one whose process is gone with nothing having
    recorded how it ended). `--all-cut` resumes every one it lists; naming ids resumes those.

    Resuming also drops the quota refusals remembered under an exhausted window: they were the
    reason half of these runs stopped, and they would refuse the same models again.
    """

    # Only a LISTING pays for the whole store: deriving every run re-reads every transcript, which
    # took over a minute on a real history and made a named resume time out (visual-critic r2).
    cut = [] if run_id else reg.interrupted_runs(within_hours=hours)
    targets = list(run_id or ()) or ([run.run_id for run in cut] if all_cut else [])
    if not targets:
        if not cut:
            print(f"Nothing cut in the last {hours:g} h.")
            return
        for run in cut:
            stopped = run.finished_at or run.started_at
            print(f"  {run.run_id[:8]}  {run.agent or run.name:<20} stopped {(time.time() - stopped) / 60:.0f} min ago")
        print(f"\nResume them with  galaius agents resume --all-cut  (or by id).")
        return
    refused = False
    for identity in targets:
        # A run at one of his subscription caps is refused before anything is queued, typed (`said_refused`) for the
        # caller to word with its reset time.
        if (run := reg.get_run(reg.resolve_run_id(identity) or identity)) is not None:
            try:
                run.prepare_continue()
            except Coded as refusal:
                said_refused(f"{identity[:8]}: {refusal}", refusal_of(refusal), file=sys.stderr)
                refused = True
                continue
        delivery = messaging.deliver_message(identity, reg.RESUME_BRIEF)
        if delivery.state == "error":
            said_refused(f"{identity[:8]}: {delivery.text.removeprefix('ERROR: ')}", delivery.refusal, file=sys.stderr)
        else:
            print(f"{identity[:8]}: {delivery.text}")
        refused = refused or delivery.state == "error"
    if refused:
        raise SystemExit(1)


#: A terminal launch waits for the vendor's own refusal rather than handing back a run that is
#: about to die at $0.00: the human is already watching this command, and a dead candidate costs
#: them a relaunch. In-process callers keep the short window.
CLI_QUOTA_WINDOW = 20.0


async def _run_agent_for_cli(provider, task, **kwargs):
    """Indirection the tests replace — spawning for real costs money and a live CLI."""

    kwargs.setdefault("quota_window", CLI_QUOTA_WINDOW)
    return await run_agent(provider, task, **kwargs)


@agents_app.command(name="spawn")
def agents_spawn(task: str, provider: str | None = None, agent: str | None = None,
                 name: str | None = None, model: str | None = None,
                 cwd: str | None = None, permission_mode: str | None = None,
                 image_paths: Annotated[list[Path] | None, Parameter(name="--image")] = None,
                 agent_id: UUID | None = None, agent_revision: UUID | None = None,
                 delegate: str | None = None, parent_run_id: str | None = None,
                 session_id: str | None = None,
                 denied_tools: Annotated[list[str] | None, Parameter(name="--deny-tool")] = None,
                 provider_modes: Annotated[list[str] | None, Parameter(name="--provider-mode")] = None,
                 quota_window: Annotated[float, Parameter(name="--quota-window")] = CLI_QUOTA_WINDOW,
                 machine: str | None = None, folder: str | None = None, project: str | None = None,
                 fence: Annotated[Path | None, Parameter(name="--fence")] = None,
                 for_run: Annotated[str | None, Parameter(name="--for")] = None,
                 continues: str | None = None, fresh: bool = False, single: bool = False, helper: bool = False,
                 cross_project: Annotated[bool, Parameter(name="--cross-project")] = False,
                 community: str | None = None,
                 read: Annotated[list[Path] | None, Parameter(name="--read")] = None) -> None:
    """Start an agent and return its id immediately, without waiting for it to finish.

    --community <entry id> starts the community library's entry (`galaius agents community "<need>"` lists them) as the
    child of the agent run asking, under its control: its role, tools, folder and permission mode, no MCP server.
    --read <file> (repeatable, with --community) hands it a file: a brief, a report, a page's screenshot or text, copied
    read-only into its own folder as it starts.

    --machine <name, id or auto> starts it on another of your PCs instead (auto: the other PC with the most free
    places), through this PC's own link: no second sign-in. --project <name or id> is the project it works on there
    (its folder is the one that PC already holds; a project not on it yet is put there once from the web), --folder a
    folder beneath it, --agent the role. The run is isolated in that PC's fence (a PC that cannot build one takes it only when its owner
    allowed that), takes the model its rule picks, and gets at most
    workspace_write (--permission-mode read_only narrows it). Follow it with
    `galaius agents events <run id> --machine <name>`.

    `agents run` streams until the agent is done — right at a terminal, useless to a UI — the
    panel needs the id NOW to show the agent working. Nothing is lost by letting go: the child
    leads its own session and writes its own stream, so it outlives this process.

    --delegate selects that capability's pinned revision: the parent run's own when its revision
    declares the name, else the catalog's declaration of it.
    --agent-id plus --agent-revision selects an exact server revision directly.
    --parent-run-id defaults to the calling agent's recorded run from its environment.
    --session-id records the owning conversation, otherwise inherited from the parent,
    GALAIUS_SESSION_ID, or the CLI harness CODEX_THREAD_ID. Unknown ownership stays unknown.
    Omit --provider to rank across providers. --deny-tool removes an exact tool for this run
    and its continuations; it never grants permissions or changes global configuration.
    --provider-mode provider=mode preserves a provider's existing workspace permission setting
    during automatic selection. An explicit --permission-mode takes precedence.
    --permission-mode read_only on Linux with an OS fence: the agent reads its folder alone and writes only in its
    own scratch folder (the kernel refuses its shell's and file tools' writes); it drives its sandbox's apps
    (launch_app, screenshot, run_actions, …) and saves their outputs in that scratch folder. Elsewhere it runs on its
    CLI's own read-only mode and calls only galaius tools that change nothing (conversation_search, agent_list, …),
    app tools refused. A critic that drives the browser (navigate) or the real desktop runs with its usual mode.
    --quota-window is how many seconds the new child is watched for a vendor quota refusal
    before the id is printed (a refusal inside it falls through to the next candidate); a
    supervisor that shows the run at once passes a short one.
    --fence names a file holding what the run's OS fence is built from (`galaius.isolation.fence.FenceSpec`
    JSON); every turn builds it again from the machine's levels. The machine runner writes it.
    The role's relation to the caller's (its `relations`) decides where the task goes: `1-N` (the default) starts a new
    run every time; `N-1` gives it to the role's run on this PC / of this project as its next turn (its context intact,
    queued behind its turn while it works) and print its id, and refuses a second beside a live one; `N-N` gives it to
    an idle run of the role in the conversation; past its `max_live` runs at once, or from a role it does not name, the
    spawn is refused. --fresh starts a new one instead of handing it over, never a second `N-1` run beside a live one. --single keeps ONE agent of the role in the conversation (a job queue's worker): one at work is queued
    the task behind its turn instead of another started beside it, and every other one of the role not at work is
    forgotten (`agents clear`) once the task is handed over or its run started (one queued for a place keeps them).
    --for names the run of this computer (full or short id) the task is asked for (an operator passing on its ask: the
    job queue waking its worker for the run that queued the job): the message handing it over records it, and the
    run taking it lists it among the runs it works for (`asked_by`).
    --continues names the run of this computer the new one takes over from (a hand-off): what that run owed, what is
    handed back or sent to it from then on, reaches the new one; it is never resumed again. A run of the same role under
    the same parent that outgrew its context is taken over without it, and so is a calling run of the new run's role whose
    context is still near full after a compaction; --helper keeps the new run the caller's. --cross-project: a person's
    deliberate act, at his own terminal (never from an agent run's process): --continues names a run of another project
    than the new run's, or --cwd lies in another project's repository than its conversation's (each refused otherwise).
    """


    # Resolved to its full id: a stored run of this computer is the only one a run can list as its asker, and the
    # short id `agents list` prints names it as everywhere else (`reg.resolve_run_id`).
    if for_run is not None:
        asker = None if machine is not None else reg.resolve_run_id(for_run)
        if asker is None or reg.get_run(asker) is None:
            print(f"ERROR: --for names an agent run of this computer (full or unambiguous short id): {for_run!r} is none", file=sys.stderr)
            raise SystemExit(2)
        for_run = asker
    if cross_project and (refused := relations.person_override_refusal()) is not None:
        print(f"ERROR: {refused}", file=sys.stderr)
        raise SystemExit(2)
    if continues is not None:
        if (predecessor := reg.resolve_run_id(continues)) is None or reg.get_run(predecessor) is None:
            print(f"ERROR: --continues names an agent run of this computer (full or unambiguous short id): {continues!r} is none", file=sys.stderr)
            raise SystemExit(2)
        continues = predecessor
    if machine is not None:
        _spawn_on_machine(machine, project, folder, task, agent=agent, provider=provider, model=model, permission=permission_mode)
        return
    try:
        if read and community is None:
            raise ValueError("--read hands files to a community agent: pass --community too")
        started_from = CommunityAgent.read(community).handing(read or ()) if community is not None else None
    except (ValueError, CatalogConnectionError) as error:
        print(f"ERROR: community entry {community!r} cannot start: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    if folder is not None:
        print("ERROR: --folder names a folder on another computer: pass --machine too (here, use --cwd)", file=sys.stderr)
        raise SystemExit(2)

    try:
        with reg.session_context(session_id, parent_run_id=parent_run_id, cli_harness=True) as owner:
            if owner is None:
                print("Session unknown: pass --session-id for conversation-scoped listings.", file=sys.stderr)
            if provider is not None:
                provider_for(provider)  # an unknown name is refused here, never by a waiter later
            spawn = Spawn.asked(
                task=task, cwd=cwd or os.getcwd(), provider=provider, agent=agent, name=name, model=model,
                permission_mode=permission_mode, image_paths=tuple(image_paths or ()),
                agent_ref=AgentCatalog.reference(agent_id, agent_revision), delegate=delegate,
                parent_run_id=parent_run_id, session_id=owner,
                denied_tools=tuple(denied_tools or ()), provider_modes=_provider_modes(provider_modes),
                quota_window=quota_window,
                fence=FenceSpec.model_validate_json(fence.read_text()) if fence is not None else None,
                for_run=for_run, single=single, continues=continues, helper=helper, cross_project=cross_project,
                community=started_from, fresh=fresh,
            )
            if (refused := spawn.naming_refusal()) is not None:
                print(f"ERROR: {refused}", file=sys.stderr)
                raise SystemExit(2)
            handover = spawn.hand_over()

            def forget_others(keep: str) -> None:
                if forgotten := spawn.forget_peers(keep):
                    print(f"Forgot {', '.join(reg.short_id(run_id) for run_id in forgotten)}: one {agent or 'agent'} of the role in this conversation (--single).")
            # The hand-over's sentence on stdout too: a caller capturing only stdout sees why it was (not) continued; the run id
            # stays the last line.
            if isinstance(handover, Continued):
                print(handover.sentence())
                forget_others(handover.run.run_id)
                print(handover.run.run_id)
                return
            if handover.why:
                print(handover.why)
            line = SpawnLine.here()
            admission = line.enter(spawn)
            if admission.ahead is not None:
                # Before the id: the run id stays the last stdout line.
                print(admission.sentence())  # its peers stay until it starts: the next --single spawn forgets them
                print(admission.run_id)
                return
            predecessor = spawn.taking_over()
            launched = asyncio.run(line.launch(admission, spawn, _run_agent_for_cli))
            if isinstance(launched, Admission):   # held at launch: it waits in line, said why
                print(launched.sentence())
                print(launched.run_id)
                return
            started = launched.run_id
            forget_others(started)
            if (record := reg.get_run(started)) is not None and record.community is not None:
                print(record.community.started())   # before the id: the run id stays the last stdout line
            if sentence := spawn.takeover_sentence(predecessor, started):
                print(sentence)  # before the id: the run id stays the last stdout line
            print(started)
    except (ValueError, RuntimeError) as e:
        # argv builder validates permission mode, four frames down; a criterion nothing clears, a
        # nonexistent profile, and a provider switched off all refuse the same way. Without this
        # the CLI printed a traceback where every other galaius failure prints one actionable
        # line.
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2) from None


@agents_app.command(name="run")
def agents_run(task: str, provider: str | None = None, agent: str | None = None,
               name: str | None = None, model: str | None = None, cwd: str | None = None,
               permission_mode: str | None = None,
               image_paths: Annotated[list[Path] | None, Parameter(name="--image")] = None,
               agent_id: UUID | None = None, agent_revision: UUID | None = None,
               delegate: str | None = None, parent_run_id: str | None = None,
               session_id: str | None = None,
               denied_tools: Annotated[list[str] | None, Parameter(name="--deny-tool")] = None,
               provider_modes: Annotated[list[str] | None, Parameter(name="--provider-mode")] = None) -> None:
    """Spawn an agent and stream its events until it finishes.

    ``--agent`` selects a server role when a catalog is configured, otherwise an installed
    local definition. Prompt and model policy come from the same selected revision.

    --delegate selects that capability's pin — the parent run's own when its revision declares the
    name, else the catalog's declaration of it. --agent-id together with
    --agent-revision selects an exact server revision; --parent-run-id selects its parent run.
    --session-id records the owning caller conversation, shared with `agents list`.
    """


    async def _go() -> int:
        prov = provider_for(provider) if provider is not None else None
        handle = await run_agent(prov, task, name=name or agent,
                                 quota_window=CLI_QUOTA_WINDOW,
                                 cwd=cwd or os.getcwd(), agent=agent, model=model,
                                 permission_mode=permission_mode,
                                 image_paths=tuple(image_paths or ()),
                                 agent_ref=AgentCatalog.reference(agent_id, agent_revision),
                                 delegate=delegate, parent_run_id=parent_run_id,
                                 denied_tools=tuple(denied_tools or ()),
                                 provider_modes=_provider_modes(provider_modes))
        print(f"run_id {handle.run_id}")
        seen = 0
        while True:
            events = reg.read_events(handle.run_id)
            for event in events[seen:]:
                print(f"  {event.kind:11} {event.summary(viewer=handle.run_id)}")
            seen = len(events)
            if handle.process.returncode is not None:
                break
            await asyncio.sleep(0.4)
        return await handle.wait()

    try:
        with reg.session_context(session_id, parent_run_id=parent_run_id, cli_harness=True) as owner:
            if owner is None:
                print("Session unknown: pass --session-id for conversation-scoped listings.", file=sys.stderr)
            raise SystemExit(asyncio.run(_go()))
    except (ValueError, RuntimeError) as e:
        # argv builder validates the mode; without this the CLI printed a traceback while every
        # other galaius failure prints one actionable line.
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2) from None
