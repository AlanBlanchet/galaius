"""`agents spawn` and `agents run`: start an agent here or on another PC, and the quota window a terminal launch waits out."""

import asyncio
import os
import sys
from pathlib import Path
from typing import Annotated
from uuid import UUID
from cyclopts import Parameter
from galaius.machine.peer.client import PeerClient, PeerError, PeerUnavailable
from galaius.machine.peer.words import place
from galaius.agents.catalog import relations
from galaius.agents import registry as reg
from galaius.agents.catalog.community import CommunityAgent
from galaius.agents.catalog.entries import AgentCatalog
from galaius.agents.catalog.connection import CatalogConnectionError
from galaius.isolation.fence import FenceSpec
from galaius.agents.providers import (
    provider_for,
)
from galaius.agents.run import run_agent
from galaius.agents.spawn_line import Admission, Continued, Spawn, SpawnLine
from galaius.cli.commands.agents.app import agents_app
from galaius.cli.commands.agents.providers import modes_by_provider


def _spawn_on_machine(machine: str, project: str | None, folder: str | None, task: str, *, agent: str | None, provider: str | None, model: str | None, permission: str | None,
                      here: str | None = None) -> None:
    """`agents spawn --machine`: the run id of an agent started on another PC of yours (through this PC's own link, no second sign-in), or one line why not."""
    try:
        print(place(PeerClient(), machine, project, task, role=agent, path=folder, permission=permission, provider=provider, model=model, here=here or os.getcwd()))
    except (ValueError, PeerError, PeerUnavailable) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None


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
    (default: the one named like the current folder's repository, else like the conversation's; a PC that does not hold
    it gets it first, unless that needs your yes), --folder a folder beneath it, --agent the role (default: main). A
    start still on its way is waited for up to 5 minutes. The run is isolated in that PC's fence (a PC that cannot
    build one takes it only when its owner allowed that), takes the model its rule picks, and gets at most
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
        _spawn_on_machine(machine, project, folder, task, agent=agent, provider=provider, model=model, permission=permission_mode, here=cwd)
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
                denied_tools=tuple(denied_tools or ()), provider_modes=modes_by_provider(provider_modes),
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
                                 provider_modes=modes_by_provider(provider_modes))
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
