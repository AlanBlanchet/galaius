"""The agent-mesh MCP tools: spawn a teammate, see the team, read what one is doing, stop it.

These are what make cross-provider teamwork ordinary. A spawned agent gets galaius declared as
one of its own MCP servers, so ``agent_spawn`` is available to a Claude agent and a Codex agent
alike — they meet on MCP, vendor-neutral. An agent spawning an agent is therefore not a special
case, just one of these calls — the parent id travels automatically, keeping the team tree
connected.
"""

import asyncio
import os
from pathlib import Path
from galaius_core import AgentRevisionRef

from galaius.agents import messaging, registry as reg
from galaius.agents.providers import PROVIDERS, available_providers, provider_caveat_note, provider_for
from galaius.agents.run import run_agent
from galaius.server.core import instrumented, mcp


def _fmt(run: reg.AgentRun) -> str:
    bits = [f"[{run.status}]", run.run_id[:8], f"({run.provider})", run.name]
    if run.foreign:
        bits.append("— not started by galaius")
    if run.cost_usd is not None:
        bits.append(f"~${run.cost_usd:.4f}")
    if run.last:
        bits.append(f"· {run.last}")
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
) -> str:
    """Start another agent to work alongside you, and return its run id immediately.

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
    delegate: name of a delegate capability on the parent run's recorded revision. Its exact
        agent reference is resolved automatically. Named-role delegation also honours parent pins.
    denied_tools: exact built-in or qualified MCP tool names to remove for this run and its
        continuations. Only narrows the role tool set; never changes permissions or user config.
    """
    try:
        prov = provider_for(provider) if provider is not None else None
    except ValueError as e:
        return f"ERROR: {e}"
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
    try:
        with reg.session_context(session_id) as owner:
            handle = await run_agent(
                prov, task, name=name or agent, cwd=cwd or os.getcwd(),
                agent=agent, model=model, permission_mode=permission_mode,
                profile=profile,
                agent_ref=agent_ref, delegate=delegate,
                image_paths=tuple(Path(path) for path in (image_paths or ())),
                denied_tools=tuple(denied_tools or ()),
            )
    except ValueError as e:  # an unknown permission mode, refused before it reaches a shell
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
    return (f"Started [{name or agent or (ran.name if ran else 'agent')}] ({ran.name if ran else 'ranked'}) — run_id={handle.run_id}\n"
            f"Session: {owner or 'unknown; pass session_id to make future launches discoverable in this conversation'}\n"
            f"Task: {' '.join(task.split())[:180]}\n"
            f"Launch policy: model={getattr(handle, 'model', None)}; "
            f"reasoning={getattr(handle, 'reasoning', None)}; "
            f"criterion={getattr(handle, 'criterion', None)}{walked}\n"
            f"Watch it with agent_list, or agent_events(run_id=\"{handle.run_id}\").{caveat}")


@mcp.tool(category="agents")
@instrumented
async def agent_list(include_foreign: bool = False, session_id: str | None = None, all_sessions: bool = False) -> str:
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
    if not runs:
        installed = ", ".join(p.name for p in available_providers()) or "none installed"
        return f"{scope}: No agent runs. Providers available here: {installed}."
    lines = [_fmt(r) for r in runs]
    live = sum(1 for r in runs if r.status == "running")
    total = sum(r.cost_usd or 0 for r in runs)
    tree = [f"  {r.run_id[:8]} ← spawned by {r.parent_run_id[:8]}"
            for r in runs if r.parent_run_id]
    out = [f"{scope}: {len(runs)} agent run(s), {live} running, ~${total:.4f} API-equivalent:", *lines]
    if tree:
        out += ["", "Team tree:", *tree]
    return "\n".join(out)


@mcp.tool(category="agents")
@instrumented
async def agent_events(run_id: str, limit: int = 20) -> str:
    """What an agent has actually been doing — its most recent events, oldest first."""
    resolved = reg.resolve_run_id(run_id) or run_id
    events = reg.read_events(resolved)
    if not events:
        known = {r.run_id for r in reg.list_runs()}
        if resolved not in known:
            return f"ERROR: no agent run {run_id!r}. Use agent_list to see the run ids."
        return f"{run_id[:8]}: no events yet (it may still be starting)."
    shown = events[-max(1, limit):]
    return "\n".join(f"  {e.kind}: {e.summary(viewer=resolved)}" for e in shown)


@mcp.tool(category="agents")
@instrumented
async def agent_stop(run_id: str) -> str:
    """Stop a running agent (and the tool processes it spawned)."""
    if reg.stop(run_id):
        return f"Stopped {run_id[:8]}."
    return f"ERROR: no agent run {run_id!r}. Use agent_list to see the run ids."


@mcp.tool(category="agents")
@instrumented
async def agent_providers() -> str:
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


@mcp.tool(category="agents")
@instrumented
async def agent_send(run_id: str, message: str, wait: bool = False) -> str:
    """Send a message to another agent — it answers with its full context intact.

    Delivery resumes the recipient's own session rather than handing it a cold summary, so it
    remembers everything it already did and its reply lands in the same transcript — what makes
    the exchange readable afterwards, and what a sequence view draws its arrows from.

    Use it to ask a teammate for something, or answer one. The exchange is recorded on BOTH
    sides, so either agent's history shows it.

    run_id: the agent to address (agent_list shows the ids).
    message: what to say — write it as a complete request; it cannot ask you a follow-up.
    wait: block until it has replied, instead of returning as soon as the message is delivered.
    """
    # Shared with `galaius agents send`, so both surfaces use one routing and policy lifecycle.
    delivery = messaging.deliver_message(run_id, message)
    if delivery.state == "error" or not wait:
        return delivery.text
    return await messaging.wait_for_reply(delivery)
