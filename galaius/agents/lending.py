"""What a community agent borrows from the run that started it — its coordinator — asked with `request_tool`.

Code decides, never a model reading the asker's words (a stranger's text writes them): a community run gets the
browser (`LENT_TOOLS`) when its coordinator holds it, on a site a page of the coordinator's shows, for `LENT_SECONDS`,
in its own fresh session, signed out; anything else of its coordinator's is never lent. What no agent holds — a
sign-in, a credential (`OWNER_ACCESS`) — is one card in the person's conversation, at most one per community run, worded
by Galaius alone. Every grant lives on the community run's record, outside its fence; every call it makes passes `gate`
first, which takes exactly the parameters each lent tool may carry and forces its session."""

import time
from collections.abc import Callable, Iterable

from galaius_core import OwnerQuestion, QuestionOption

from galaius.agents import registry as reg
from galaius.agents.catalog.community import LENT_ACTIONS, LENT_SECONDS, LENT_SESSION, LENT_TOOLS, OWNER_ACCESS, REQUEST_TOOL, CommunityAgent, ToolGrant, origin, quote
from galaius.agents.policy import TOOL_PREFIX
from galaius.agents.transport.events import AgentEvent

REQUESTS_MAX = 8
"""`request_tool` calls one community run may make: a stranger's text never keeps its coordinator busy."""
WHY_MAX = 200


class Refused(PermissionError):
    """A call of a community run its grants do not cover: what it is told."""


def borrower(run_id: str | None) -> reg.AgentRun | None:
    """Run `run_id` when it is a community run (whose calls `gate` decides); None for any other caller."""
    run = reg.get_run(run_id) if run_id else None
    return run if run is not None and run.community is not None else None


def session(run: reg.AgentRun) -> str:
    return LENT_SESSION.format(run=reg.short_id(run.run_id))


def request(run_id: str | None, tool: str, why: str, site: str | None, shown: Callable[[str], Iterable[str]],
            tools_of: Callable[[str], list[str] | None]) -> str:
    """`request_tool` of run `run_id`: what it is told. `shown(run)` lists the sites (origins) a run's browser pages show
    now; `tools_of(role)` a role's tools (None: every one)."""
    run = borrower(run_id)
    if run is None:
        return "ERROR: request_tool lends tools to a community agent; you hold your role's tools already."
    counted = reg.community_changed(run.run_id, lambda held: held.model_copy(update={"asked": held.asked + 1}) if held.asked < REQUESTS_MAX else None)
    if counted is None or counted.community is None or counted.community.asked == run.community.asked:
        return f"ERROR: {REQUESTS_MAX} requests already: end your task with what you have, and say in your report what you lacked."
    name = tool.strip().removeprefix(TOOL_PREFIX).casefold()
    if name in OWNER_ACCESS:
        return _card(counted, name, why)
    if name != "browser" and name not in LENT_TOOLS:
        return (f"ERROR: {tool[:60]!r} is never lent to a community agent. What can be lent: \"browser\" (its tools: "
                f"{', '.join(LENT_TOOLS)}), from the agent that started you; what only the person gives: {', '.join(OWNER_ACCESS)}.")
    coordinator = reg.get_run(run.parent_run_id) if run.parent_run_id else None
    if coordinator is None or coordinator.community is not None:
        return "ERROR: no agent run started you: nothing can be lent."
    held = tools_of(coordinator.agent)
    lent = tuple(each for each in LENT_TOOLS if held is None or f"{TOOL_PREFIX}{each}" in held)
    if "navigate" not in lent:
        return f"ERROR: the {coordinator.agent} agent that started you has no browser to lend. Say in your report that you needed one."
    wanted = origin(site or "")
    open_sites = sorted(set(shown(coordinator.run_id)))
    if wanted is None or wanted not in open_sites:
        named = f" Sites it shows now: {', '.join(open_sites)}." if open_sites else " It shows none now."
        return (f"ERROR: the browser is lent on a site the {coordinator.agent} agent that started you has open; name it as site "
                f"(http(s)://host:port).{named} If it is another, say so in your report.")
    grant = ToolGrant(tools=lent, origin=wanted, by=coordinator.run_id, until=time.time() + LENT_SECONDS)
    reg.community_changed(run.run_id, lambda each: each.model_copy(update={"grants": (*(old for old in each.grants if old.origin != wanted), grant)}))
    said = (f"lent the browser ({', '.join(lent)}) on {wanted} until {time.strftime('%H:%M', time.localtime(grant.until))} "
            f"to community agent {reg.short_id(run.run_id)}, signed out; it asked: {quote(why[:WHY_MAX], limit=WHY_MAX)}")
    _note((run.run_id, coordinator.run_id), said)
    return (f"Lent: the browser on {wanted} until {time.strftime('%H:%M', time.localtime(grant.until))}: {', '.join(lent)}. It is your own "
            f"session ({session(run)}), fresh and signed out; it stays on {wanted} (a page leaving it is brought back). Call the tools now.")


def _card(run: reg.AgentRun, name: str, why: str) -> str:
    """The one question run `run`'s request for `name` puts in the person's conversation (its root run), worded by
    Galaius: never a second for the same run."""
    assert run.community is not None
    if run.community.card is not None:
        return f"ERROR: your request already reached the person (question {run.community.card[:8]}); his answer goes to the agent that started you."
    root = run.root_run_id or run.parent_run_id
    if root is None:
        return "ERROR: no conversation to ask in."
    coordinator = reg.get_run(run.parent_run_id) if run.parent_run_id else None
    asker = coordinator.agent if coordinator is not None and coordinator.agent else "an"
    title = run.community.title[:60]
    question = OwnerQuestion(
        question=f"Community agent « {title} » ({asker} agent's) asks for {OWNER_ACCESS[name]}"[:200],
        options=(QuestionOption(label="Refuse", consequence="It carries on without it and says what it lacked."),
                 QuestionOption(label="Allow", consequence=f"The {asker} agent is told you allow it and hands it over itself.")),
        recommended=0, criticality="credential" if name == "credential" else "new_access", allow_custom=False,
        reason=f"No agent here holds it; source {run.community.source}"[:160])
    if reg.community_changed(run.run_id, lambda each: each.model_copy(update={"card": question.id}) if each.card is None else None).community.card != question.id:
        return "ERROR: your request already reached the person; his answer goes to the agent that started you."
    try:
        reg.ask_owner(root, question)
    except reg.QuestionRefused as refused:
        return f"ERROR: the person could not be asked ({refused}). Say in your report what you lacked."
    _note((run.run_id, *((run.parent_run_id,) if run.parent_run_id else ())),
          f"asked the person ({question.short}) for {name} for community agent {reg.short_id(run.run_id)}; it said: {quote(why[:WHY_MAX], limit=WHY_MAX)}")
    return (f"Asked the person (question {question.short}): one card in his conversation. Do not wait for it: finish what you can "
            "without it, and say in your report what you lacked.")


def _note(runs: Iterable[str], said: str) -> None:
    for run_id in dict.fromkeys(runs):
        reg.append_event(run_id, AgentEvent(kind="other", raw_type="tool_lent", text=f"Galaius {said}"))


def gate(run: reg.AgentRun, name: str, arguments: dict) -> dict:
    """`arguments` of community run `run`'s call to `name`, its session forced (`session`); `Refused` saying why when no
    live grant covers it: a tool or a parameter not lent, a site other than the one lent."""
    if name == REQUEST_TOOL:
        return arguments
    community: CommunityAgent = run.community
    allowed = LENT_TOOLS.get(name)
    grant = next((each for each in reversed(community.grants) if each.live() and name in each.tools), None)
    if allowed is None or grant is None:
        raise Refused(f"{name}: not lent to you. Ask with request_tool(tool=\"browser\", why=..., site=...): only the browser is lent.")
    if extra := sorted(set(arguments) - allowed):
        raise Refused(f"{name}: parameters not lent to you: {', '.join(extra)} (yours: {', '.join(sorted(allowed))}).")
    if name == "navigate" and origin(str(arguments.get("url", ""))) != grant.origin:
        raise Refused(f"navigate: only {grant.origin} is lent to you.")
    for step in arguments.get("actions") or () if name == "run_actions" else ():
        kind = step.get("type") if isinstance(step, dict) else None
        if kind not in LENT_ACTIONS:
            raise Refused(f"run_actions: step {kind!r} not lent to you (yours: {', '.join(LENT_ACTIONS)}).")
        if extra := sorted(set(step) - LENT_ACTIONS[kind]):
            raise Refused(f"run_actions: {kind} fields not lent to you: {', '.join(extra)}.")
    return {**arguments, "session": session(run)}


def site(run: reg.AgentRun) -> str | None:
    """The site run `run`'s live browser grant is on; None when none is live."""
    return next((each.origin for each in reversed(run.community.grants) if each.live()), None) if run.community else None
