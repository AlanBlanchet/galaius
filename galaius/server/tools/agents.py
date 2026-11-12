"""The agent-mesh MCP tools: spawn a teammate, see the team, read what one is doing, stop it.

These are what make cross-provider teamwork ordinary. A spawned agent gets galaius declared as
one of its own MCP servers, so ``agent_spawn`` is available to a Claude agent and a Codex agent
alike — they meet on MCP, vendor-neutral. An agent spawning an agent is therefore not a special
case, just one of these calls — the parent id travels automatically, keeping the team tree
connected.
"""

import asyncio
from pathlib import Path
from galaius.account.caller import Caller
from galaius_core import CRITICALITIES, AgentRevisionRef, Criticality, CommunityKind, OwnerQuestion, QuestionOption
from pydantic import ValidationError

from galaius.agents import lending, messaging, registry as reg
from galaius.agents import agent_queue
from galaius.agents.turns import handback
from galaius.agents.catalog import community as community_agents
from galaius.agents.catalog.community import CommunityAgent
from galaius.agents.conversations import ConversationAnswers, ConversationScope
from galaius.agents.providers import PROVIDERS, available_providers, provider_caveat_note, provider_for
from galaius.agents.run import load_policy, run_agent
from galaius.agents.spawn_line import Admission, Continued, Spawn, SpawnLine, StartNew
from galaius.server.core import instrumented, mcp, offloaded, shown_sites


def _fmt(run: reg.AgentRun, waiting: str | None = None) -> str:
    """One run's line; `waiting`: what `SpawnLine.waiting_said` says of it, in place of its last activity."""
    bits = [f"[{run.status}]", run.run_id[:8], f"({run.provider})", run.name]
    if run.foreign:
        bits.append("— not started by galaius")
    if run.cost_usd is not None:
        bits.append(f"~${run.cost_usd:.4f}")
    # A community agent's own words are read quoted (`agent_events`), never in a list line.
    if said := waiting or (run.last if run.community is None else "community agent: agent_events shows what it does"):
        bits.append(f"· {said}")
    return "  " + " ".join(bits)


@mcp.tool(category="agents")
@instrumented
async def agent_spawn(
    task: str,
    provider: str | None = None,
    agent: str | None = None,
    name: str | None = None,
    model: str | None = None,
    cwd: str | None = None,
    permission_mode: str | None = None,
    profile: str | None = None,
    image_paths: list[str] | None = None,
    agent_ref: AgentRevisionRef | None = None,
    delegate: str | None = None,
    session_id: str | None = None,
    denied_tools: list[str] | None = None,
    fresh: bool = False,
    continues: str | None = None,
    for_run: str | None = None,
    helper: bool = False,
    community: str | None = None,
    read: list[str] | None = None,
) -> str:
    """Start an agent to work alongside you and return its run id immediately. The role's relation to yours (its
    `relations`: who may call it, how many of its runs answer) decides: `1-N` (most roles) starts a new run for every
    task; `N-1` (a role keeping one run, the deployer) hands the task to its run on this PC / of this project, queued
    behind its turn while it works, and refuses a second beside a live one; `N-N` hands it to an idle run of the role in
    this conversation; `max_live` caps how many you run at once. A role whose relations do not name yours is refused.

    Runs as its own process on this machine, using the vendor CLI's own login — you never handle
    a credential. Given galaius as an MCP server, so it can call these same tools: it can spawn
    agents of its own, including from a DIFFERENT provider than yours.

    Returns as soon as the agent is alive, not when it finishes — use agent_list / agent_events
    to watch it, agent_stop to end it.

    profile: one of the OPERATOR's own named profiles (GALAIUS_PROFILE_* in ~/.galaius/config.env)
        deciding what this agent runs on — e.g. a local Ollama model for a cheap critic while a
        reviewer stays on a frontier one. A profile resolves to a fixed, allow-listed set of
        variables; you cannot pass an environment, and an unknown name is refused rather than
        silently ignored.

    session_id: owning caller conversation, shared by spawn and list. Omit only when a
        recorded parent or GALAIUS_SESSION_ID provides it. No cwd or MCP transport inference.
    task: what the agent should do — write it as a complete brief; the agent cannot ask you.
    provider: which CLI to run ("claude", "codex"). Omit it to rank the role's criterion across
        every installed, switched-on CLI and start the first candidate this machine can run;
        naming one restricts the ranked list to it and never falls through to another.
    agent: a definition the CLI resolves itself — Claude Code reads ~/.claude/agents/<name>.md —
        so the run IS that agent (e.g. "code-reviewer"), with its own system prompt and tools.
        The run is named after it, which is what makes a team readable at a glance.
    name: a short role label for the supervisor view. Defaults to the agent definition, then to
        the provider — so several runs are not all just called "claude".
    model: does not override a configured role policy. Edit the rule in the agent UI instead.
        A named agent and a satisfiable role criterion are required; no silent model fallback.
    cwd: directory to work in; defaults to galaius's own working directory.
    permission_mode: shared intent describing how much the agent may do on its own. galaius
        translates it into each provider's CLI vocabulary; an intent with no exact provider
        equivalent uses the provider's safest useful mapping. Omit to leave the CLI's own
        configured default alone. Modes that act WITHOUT ASKING cannot be set from here — a person
        chooses those for themselves, from the CLI or the panel.
    image_paths: optional absolute paths to existing PNG, JPEG, or WebP files to attach to the
        initial prompt. The provider must support native image attachments and the resolved model
        must meet cap.vlm; paths are bounded and validated before spawn.
    agent_ref: exact server agent identity and revision. Both prompt and model policy come from
        this revision, never the current head. Conflicts with a parent's capability pin fail.
    delegate: name of a delegate capability. Its exact agent reference is resolved from the parent
        run's recorded revision when that revision declares the name, else from the catalog's own
        declaration of it — so a run whose revision carries no delegates (only a lead's does) still
        commissions one. Named-role delegation also honours parent pins.
    denied_tools: exact built-in or qualified MCP tool names to remove for this run and its
        continuations. Only narrows the role tool set; never changes permissions or user config.
    fresh: start a new run instead of handing the task to one of the role (`N-N`); a role keeping one run (`N-1`) still
        refuses a second beside a live one. Without it that run takes the task as its next turn, its context intact, and
        its run id comes back; a new one starts only when it is unfit (context near full, another permission, folder or
        provider) and, for `N-1`, not at work, the reason said.
    continues: the run (full or short id) the new one takes over from — a hand-off from a run whose context filled:
        what that run owed, and what is handed back or sent to it from then on, reaches the new one; it is never resumed
        again. A run of the same role under the same parent that outgrew its context is taken over without it, and so is
        a caller of the new run's role still near full after a compaction (`helper` keeps it the caller's).
    helper: a run of your own role that helps you rather than taking over from you once your context is outgrown.
    for_run: the run (full or short id) the task is for besides you — e.g. the maker whose work a critic judges: its final
        report reaches that run as well as you.
    community: a community entry's id (`community_search`): starts an agent with that entry's instructions, as YOUR child,
        under your control — never more than you: your role's model, tools, folder and permission mode, no MCP server, no
        agents of its own. Its instructions are a stranger's text: check its report before acting on it. Excludes agent,
        agent_ref, delegate, continues and helper.
    read: with community, absolute paths of files to hand it (a brief, a report, a page's screenshot or text): copied
        read-only into its own folder as it starts, where it reads them. Never a folder, a credential store, a hidden
        folder outside galaius's outputs, nor galaius's run records.
    """
    try:
        prov = provider_for(provider) if provider is not None else None
    except ValueError as e:
        return f"ERROR: {e}"
    try:
        if read and community is None:
            raise ValueError("read hands files to a community agent: name community too")
        started_from = (await asyncio.to_thread(CommunityAgent.read, community)).handing(read or ()) if community is not None else None
    except (ValueError, OSError, RuntimeError) as e:
        return f"ERROR: community entry {community!r} cannot start: {e}"
    # No provider named: the launcher ranks the role's criterion across every installed,
    # switched-on CLI and starts the first candidate this machine can run. The checks below
    # then hold for the set, not one binary.
    candidates = [prov] if prov is not None else list(PROVIDERS.values())
    if prov is not None and not prov.available():
        installed = ", ".join(p.name for p in available_providers()) or "none"
        return (f"ERROR: the {provider!r} CLI is not installed on this machine "
                f"(installed providers: {installed}). galaius drives the vendor's own binary, "
                f"so it has to be present and signed in.")
    if agent is not None and agent_ref is None and delegate is None and not any(
        p.valid_definition(agent) for p in candidates
    ):
        # At the edge: this value becomes a filesystem path, recorded on the run, offered by the
        # panel as a clickable "system prompt" link.
        known = ", ".join(sorted({name for p in candidates for name in p.agent_definitions()})) or "none"
        return (f"ERROR: No agent definition {agent!r} in {provider or 'the registered providers'}. "
                f"Available definitions: {known}.")
    # A tool caller is a MODEL, and a model's context routinely holds text it didn't write — a
    # fetched page, a file, an issue body — so this parameter is reachable by indirect injection.
    # An unrestricted mode reached that way stands up an agent that acts without asking, nobody
    # watching, nothing on screen before it runs. Refused HERE, not deeper: CLI and panel picker
    # still offer the full set, since a person choosing it for themselves is the point of the
    # control — widening your own privileges is not.
    unrestricted = {m.id for p in candidates for m in p.permission_modes() if m.unrestricted}
    if permission_mode in unrestricted:
        allowed = ", ".join(sorted({m.id for p in candidates for m in p.permission_modes() if not m.unrestricted}))
        return (f"ERROR: {permission_mode!r} lets an agent act without asking, and cannot be set "
                f"from a tool call. Choose one of: {allowed}. To run an agent unrestricted, start "
                f"it yourself — `galaius agents spawn ... --permission-mode {permission_mode}` — "
                "so the choice has a person behind it.")
    if continues is not None:
        if (predecessor := reg.resolve_run_id(continues)) is None or reg.get_run(predecessor) is None:
            return f"ERROR: continues names no agent run of this computer (full or unambiguous short id): {continues!r}."
        continues = predecessor
    if for_run is not None:
        if (asker := reg.resolve_run_id(for_run)) is None or reg.get_run(asker) is None:
            return f"ERROR: for_run names no agent run of this computer (full or unambiguous short id): {for_run!r}."
        for_run = asker
    label = name or agent or (prov.name if prov else "agent")
    try:
        with reg.session_context(session_id) as owner:
            spawn = Spawn.asked(
                task=task, cwd=cwd or str(Caller.current().cwd), provider=provider, agent=agent, name=name, model=model,
                permission_mode=permission_mode, profile=profile, agent_ref=agent_ref, delegate=delegate,
                image_paths=tuple(Path(path) for path in (image_paths or ())), denied_tools=tuple(denied_tools or ()),
                session_id=owner, continues=continues, for_run=for_run, helper=helper, community=started_from, fresh=fresh,
            )
            if (refused := spawn.naming_refusal()) is not None:
                return f"ERROR: {refused}."
            handover = await asyncio.to_thread(spawn.hand_over)
            if isinstance(handover, Continued):
                return handover.sentence()
            line = SpawnLine.here()
            admission = line.enter(spawn)
            if admission.ahead is not None:
                return (f"Queued [{label}] — run_id={admission.run_id}, {admission.sentence()}.{handover.note()}\n"
                        f"It starts by itself once a place frees; agent_events(run_id=\"{admission.run_id}\") says where it stands, "
                        "agent_stop takes it out of the line.")
            predecessor = await asyncio.to_thread(spawn.taking_over)
            launched = await line.launch(admission, spawn, run_agent)
            if isinstance(launched, Admission):   # held at launch: it waits in line, said why
                return (f"Queued [{label}] — run_id={launched.run_id}, {launched.sentence()}.{handover.note()}\n"
                        f"It starts by itself once that clears; agent_events(run_id=\"{launched.run_id}\") says where it stands, "
                        "agent_stop takes it out of the line.")
            handle = launched
            takeover = await asyncio.to_thread(spawn.takeover_sentence, predecessor, handle.run_id)
            takeover = f"{takeover}\n" if takeover else ""
    except ValueError as e:  # an unknown permission mode, or a spawn leaving its caller's tree (`Spawn.asked`)
        return f"ERROR: {e}"
    except (OSError, RuntimeError) as e:
        return f"ERROR: could not start the {provider or 'ranked'} agent — {e}"
    # The recorded run names the CLI that actually started; a ranked launch may have walked
    # past candidates, and the record carries which ones and why.
    started = reg.get_run(handle.run_id)
    ran = provider_for(started.provider) if started is not None else prov
    skipped = ", ".join(
        f"{s.candidate.provider}/{s.candidate.model} ({s.message or s.reason})"
        for s in (started.skipped if started else ())
    )
    walked = f"\nSkipped before starting: {skipped}" if skipped else ""
    caveat_note = provider_caveat_note(ran) if ran is not None else ""
    caveat = f"\nNOTE: the {ran.name} adapter{caveat_note}" if caveat_note else ""
    if started is not None and started.community is not None:
        caveat += f"\n{started.community.started()}"
    return (f"Started [{name or agent or (ran.name if ran else 'agent')}] ({ran.name if ran else 'ranked'}) — run_id={handle.run_id}\n"
            f"Session: {owner or 'unknown; pass session_id to make future launches discoverable in this conversation'}\n"
            f"Task: {' '.join(task.split())[:180]}\n"
            f"Launch policy: model={getattr(handle, 'model', None)}; "
            f"reasoning={getattr(handle, 'reasoning', None)}; "
            f"criterion={getattr(handle, 'criterion', None)}{walked}{handover.note()}\n"
            f"{takeover}"
            f"Watch it with agent_list, or agent_events(run_id=\"{handle.run_id}\").{caveat}")


@mcp.tool(category="agents")
@instrumented
@offloaded
def request_tool(tool: str, why: str, site: str | None = None) -> str:
    """Ask the agent that started you for a tool you lack — for a community agent, the one Galaius tool it holds.

    Decided by Galaius at once, from what that agent holds, never from your words: "browser" lends you a browser of
    your own, fresh and signed out, on one site a page of that agent shows now (`site`), to read and click for 30
    minutes (navigate, get_page_state, get_interactive_elements, screenshot, run_actions). Anything else — a sign-in, a
    credential, another tool — is never lent and never asked of the person by you: finish without it and say in your report
    what you lacked and for which step; the agent that started you decides.

    tool: "browser".
    why: one line, what you need it for (shown to the agent that started you).
    site: with "browser", the address of the site (http(s)://host:port).
    """
    return lending.request(messaging.asking_run(), tool, why, site, shown_sites, load_policy().tools_for)


@mcp.tool(category="agents", read_only=True)
@instrumented
@offloaded
def community_search(need: str, kind: CommunityKind | None = None) -> str:
    """Search the community library — agents, skills, prompts, MCP servers and pipelines published by people, imported
    from public repositories under their licence, or found by searching GitHub, npm and the MCP registry — for one made
    for `need` (a few words naming the domain: « reverse engineer a binary », « write release notes »). Search before
    starting work in a domain, and again for each sub-domain a plan adds. Lists the best matches (id, kind, title, what it
    is for, author, stars or downloads, last update, licence, source); a listed one is shown by link only. Start one with
    agent_spawn(community=<id>, task=...): it runs as your child, under your control.

    need: what you want done, in a few words; entries holding more of them rank first.
    kind: "agent", "skill", "prompt", "mcp" or "pipeline" only; every kind when omitted.
    """
    try:
        return CommunityAgent.search(need, kind)
    except (OSError, RuntimeError, ValueError) as e:
        return f"ERROR: the community library could not be read: {e}"


@mcp.tool(category="agents", read_only=True)
@instrumented
@offloaded
def agent_list(include_foreign: bool = False, session_id: str | None = None, all_sessions: bool = False) -> str:
    """Launched runs owned by this conversation, including nested children; not the agent roster.

    Pass session_id used at spawn, or inherit the recorded parent / GALAIUS_SESSION_ID.
    Missing identity requires an explicit choice; it never lists unrelated sessions.
    all_sessions=True opts into machine-wide history, including unassigned old records.
    include_foreign=True only adds discovered editor sessions when all_sessions=True.

    API-equivalent cost is an estimate, not proof of billed spend. Charge path and account impact
    are unknown here: subscription usage may be included, limited, credited, or separately charged.
    """
    try:
        runs = reg.session_runs(session_id=session_id, all_sessions=all_sessions, include_foreign=include_foreign)
    except ValueError as error:
        return f"ERROR: {error}"
    scope = "All sessions (including unknown owners)" if all_sessions else f"Session {reg.resolve_session_id(session_id)}"
    line = SpawnLine.here()
    queued = [f"[{'failed' if place.state == 'failed' else 'queued'}] {place.run_id[:8]} {place.label()}  {said}"
              for place, said in line.session(None if all_sessions else reg.resolve_session_id(session_id))]
    if not runs and not queued:
        installed = ", ".join(p.name for p in available_providers()) or "none installed"
        return f"{scope}: No agent runs. Providers available here: {installed}."
    waiting = line.waiting_said()
    lines = [_fmt(r, waiting.get(r.run_id)) for r in runs]
    live = sum(1 for r in runs if r.status == "running")
    total = sum(r.cost_usd or 0 for r in runs)
    tree = [f"  {r.run_id[:8]} ← spawned by {r.parent_run_id[:8]}"
            for r in runs if r.parent_run_id]
    out = [f"{scope}: {len(runs)} agent run(s), {live} running, ~${total:.4f} API-equivalent:", *lines]
    if queued:
        out += ["", "Not started (waiting for a place on this computer):", *queued]
    if tree:
        out += ["", "Team tree:", *tree]
    return "\n".join(out)


def _fenced_team() -> tuple[str, frozenset[str]] | None:
    """When the asking agent is fenced (`ConversationScope.held`): what holds it and its team's run ids, the only runs
    it may read or stop; None when it is not. Raises PermissionError as `ConversationScope.asking` does."""
    asking = ConversationScope.asking(Caller.environ())
    if asking is None or (held := ConversationScope.held(asking)) is None:
        return None
    return held, frozenset(run.run_id for run in reg.trees(frozenset({asking.root_run_id or asking.run_id})))


@mcp.tool(category="agents")
@instrumented
@offloaded
def agent_events(run_id: str, limit: int = 20) -> str:
    """What an agent has actually been doing — its most recent events, oldest first."""
    resolved = reg.resolve_run_id(run_id) or run_id
    try:
        fenced = _fenced_team()
    except PermissionError as error:
        return f"ERROR: {error}"
    if fenced is not None and resolved not in (team := fenced[1]):
        waiting = SpawnLine.here().describe(run_id, within=team)  # its own child, not started yet
        return f"{run_id[:8]}: {waiting}" if waiting is not None else f"ERROR: {fenced[0]} reads only its own team's runs."
    events = reg.read_events(resolved)
    if not events:
        if (waiting := SpawnLine.here().describe(run_id)) is not None:
            return f"{run_id[:8]}: {waiting}"
        known = {r.run_id for r in reg.list_runs()}
        if resolved not in known:
            return f"ERROR: no agent run {run_id!r}. Use agent_list to see the run ids."
        return f"{run_id[:8]}: no events yet (it may still be starting)."
    shown = events[-max(1, limit):]
    lines = community_agents.shown(reg.get_run(resolved), "\n".join(f"  {e.kind}: {e.summary(viewer=resolved)}" for e in shown))
    return f"{lines}\n\n{report}" if (report := handback.read_inline(resolved, shown)) else lines


@mcp.tool(category="agents")
@instrumented
@offloaded
def agent_stop(run_id: str) -> str:
    """Stop a running agent (and the tool processes it spawned), or take one not started yet out of the line."""
    if reg.stop(run_id, by=reg.caller_stop_actor(Caller.environ())):
        return f"Stopped {run_id[:8]}."
    try:
        fenced = _fenced_team()
    except PermissionError as error:
        return f"ERROR: {error}"
    if reg.resolve_run_id(run_id) is None and (cancelled := SpawnLine.here().cancel(run_id, within=fenced[1] if fenced else None)) is not None:
        return f"Taken out of the line: {cancelled[:8]} never started."
    return f"ERROR: no agent run {run_id!r}. Use agent_list to see the run ids."


@mcp.tool(category="agents", read_only=True)
@instrumented
@offloaded
def agent_providers() -> str:
    """Which agent CLIs can be spawned here, and which named agents each can resolve.

    The definitions matter as much as the providers: `agent_spawn(agent=...)` runs one of them
    with its own system prompt and tools, and a caller — usually an agent, which cannot ask a
    follow-up question — has no other way to learn the names.
    """
    lines = []
    for p in PROVIDERS.values():
        state = "available" if p.available() else f"not installed (no {p.binary!r} on PATH)"
        lines.append(f"  {p.name}: {state}{provider_caveat_note(p)}")
        lines.append(
            f"    image attachments: {'supported' if p.image_attachment_support() else 'unsupported'}"
        )
        if definitions := p.agent_definitions():
            lines.append(f"    agents: {', '.join(definitions)}")
    return "Agent providers:\n" + "\n".join(lines)


def _deliver(run_id: str, message: str, fyi: bool) -> "tuple[messaging.Delivery, str | None] | str":
    """`agent_send`'s delivery (records and a dispatcher process): the delivery and what its turn waits for, or the refusal."""
    if reg.resolve_run_id(run_id) is None and (waiting := SpawnLine.here().describe(run_id)) is not None:
        return f"ERROR: {run_id[:8]} has not started ({waiting}); send once it runs."
    # A tool is never the person: no override of the project rule here (`galaius agents send --cross-project`).
    if (refused := messaging.send_refusal(run_id)) is not None:
        return f"ERROR: {refused}"
    # Shared with `galaius agents send`, so both surfaces use one routing and policy lifecycle.
    delivery = messaging.deliver_message(run_id, message, turn=agent_queue.MessageTurn(fyi=fyi))
    return delivery, SpawnLine.here().waits(delivery.run_id) if delivery.state == "queued" else None


@mcp.tool(category="agents")
@instrumented
async def agent_send(run_id: str, message: str, wait: bool = False, fyi: bool = False) -> str:
    """Send a message to another agent — it answers with its full context intact.

    Delivery resumes the recipient's own session rather than handing it a cold summary, so it
    remembers everything it already did and its reply lands in the same transcript — what makes
    the exchange readable afterwards, and what a sequence view draws its arrows from.

    Use it to ask a teammate for something, or answer one. The exchange is recorded on BOTH
    sides, so either agent's history shows it.

    run_id: the agent to address (agent_list shows the ids).
    message: what to say — write it as a complete request; it cannot ask you a follow-up.
    wait: block until it has replied, instead of returning as soon as the message is delivered.
    fyi: a one-way note (a report, an ack, a heads-up): it reads it, and its turns never report back to you for it.
    """
    sent = await asyncio.to_thread(_deliver, run_id, message, fyi)
    if isinstance(sent, str):
        return sent
    delivery, waiting = sent
    said = f"{delivery.text} Its turn: {waiting}" if waiting else delivery.text
    if delivery.state == "error" or not wait:
        return said
    reply = await messaging.wait_for_reply(delivery)
    if delivery.state == "replied" and (answered := await asyncio.to_thread(reg.get_run, delivery.run_id)) is not None:
        await asyncio.to_thread(handback.collected, messaging.sender_id(), [handback.Report.of(answered)])
    return f"{said}\n{reply}" if waiting else reply


def _ask(subject: str, question: str, options: list[QuestionOption], recommended: int, criticality: Criticality, reason: str) -> str:
    """`ask_owner`'s posting (a record write): what the asking agent reads back, a refusal included. The asker is the run
    whose process scope the caller lives in (`messaging.asking_run`): no environment variable it could clear moves it."""
    if Caller.current().unowned():
        return f"ERROR: {messaging.UNOWNED}"
    if (run_id := messaging.asking_run()) is None:
        return "ERROR: this is the owner's own conversation: he reads you here, ask him in your answer."
    try:
        posted = OwnerQuestion(subject=subject, question=question, options=tuple(options), recommended=recommended, criticality=criticality,
                               reason=reason)
    except ValidationError as error:
        said = "; ".join(f"{'.'.join(map(str, problem['loc'])) or 'question'}: {problem['msg']}" for problem in error.errors())
        return (f"ERROR: not asked: {said}. He must understand it cold, from his phone: name the thing it acts on, what each answer does "
                f"for him and what it costs, in plain words, no internal ids. Only these reasons reach him: {', '.join(CRITICALITIES)}; "
                "anything else you decide yourself: take the reversible, conventional option, say it in one line, keep working.")
    try:
        reg.ask_owner(run_id, posted)
    except reg.QuestionRefused as refused:
        return f"ERROR: not asked: {refused}."
    return (f"Asked the owner: question {posted.short}, a card in his conversation with your options and a notification on his "
            "devices. Do not wait for it: keep working on everything that does not depend on it, and end your turn when that is "
            f"done. His answer arrives later as a message from him opening « [question {posted.short}] »; act on it then.")


@mcp.tool(category="agents")
@instrumented
async def ask_owner(subject: str, question: str, options: list[QuestionOption], recommended: int, criticality: Criticality,
                    reason: str) -> str:
    """Put ONE critical question to the owner without waiting: it shows as a card in his conversation with one-tap
    answers and notifies his devices; you keep working, and his pick reaches you later as a message from him.

    Only a conversation's coordinator may ask (a sub-agent sends its question to its coordinator with agent_send and keeps
    working), and only when the fork is critical — `criticality` one of: destructive (cannot be taken back), prod
    (touches production), public (publishes to an audience he does not control), credential (needs one he holds),
    new_access (opens an access that does not exist yet), spend_past_cap (spends past his cap). Every other fork you
    decide yourself: the reversible, conventional option, said in one line.

    He reads it cold, maybe on his phone, with none of your context: write in the language he writes to you (French when
    he writes French), plain words, no internal ids (run, commit, question ids), no agent or role names he did not choose.
    A question missing its subject or an option's cost, not ending with « ? », or carrying an id, is refused.

    subject: what it is about, named as he knows it: the file, screen, server, account or amount it acts on.
    question: one line ending with « ? ».
    options: 2 to 4, each {label, consequence: what happens for him if he picks it, cost: what it costs him in time or
        money, « rien » when nothing}; shown A, B, C, D. His own words (« Autre ») always follow them.
    recommended: the option you would take, 0-based.
    reason: one line, why this cannot be decided without him.
    """
    return await asyncio.to_thread(_ask, subject, question, options, recommended, criticality, reason)


@mcp.tool(category="agents", read_only=True)
@instrumented
async def conversation_search(query: str, limit: int = 20, project: str | None = None, since: str | None = None,
                              every_project: bool = False) -> str:
    """Search the conversations this computer holds — the runs galaius started and the sessions each
    agent CLI saved (the person's editor conversations) — for every word of `query`.

    Searched: what was asked, what was answered, what agents told each other, each tool call by its
    leading argument. Never what a tool returned, a call searching conversations itself, or the asking
    agent's own conversation. Credentials are masked. An agent searches its own project's
    conversations (the repository its folder is in); a fenced run, a member's run on a lent PC, or a
    run denied reading files searches none (`ConversationScope`).

    query: words that must all appear in one line; "quoted words" must appear together.
    limit: conversations returned, best match first: the person's own prompts, then what agents were
        asked, answered and told each other, then tool calls (1-50).
    project: only conversations of the project with this name (a repository's folder name).
    since: only conversations active since this date or moment (ISO 8601, e.g. 2026-10-11; UTC unless it says).
    every_project: the person's own session only — search every project, not just this folder's.
    Read one with conversation_read(id, around=<line>).
    """
    caller = Caller.current()
    answers = ConversationAnswers(environment=dict(caller.environment), cwd=str(caller.cwd))
    return await asyncio.to_thread(answers.search, query, limit, project, since, every_project)


@mcp.tool(category="agents", read_only=True)
@instrumented
async def conversation_read(id: str, around: int | None = None, limit: int = 50, every_project: bool = False) -> str:
    """Read one conversation this computer holds, `limit` lines at a time (what was asked, answered,
    said between agents, each tool call by its leading argument; never tool output; credentials masked).

    id: its id (from conversation_search), or the start of exactly one.
    around: the line to centre on (conversation_search names it); omitted: its last lines.
    limit: lines returned (1-200). Page with around= the first or last line number shown.
    every_project: the person's own session only — read outside this folder's project.
    """
    caller = Caller.current()
    answers = ConversationAnswers(environment=dict(caller.environment), cwd=str(caller.cwd))
    return await asyncio.to_thread(answers.read, id, around, limit, every_project)
