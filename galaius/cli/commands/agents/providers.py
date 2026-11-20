"""The providers and modes the agent commands list."""

import json
import sys
from galaius_core import DEFAULT_PROGRAM
from galaius.machine.presence import PresenceFiles
from galaius.agents import providers as agent_providers
from galaius.agents import registry as reg
from galaius.agents.policy import Policy, PolicyError
from galaius.agents.providers import (
    PROVIDERS,
    provider_caveat_note,
    provider_for,
)
from galaius.cli.commands.agents.app import agents_app


@agents_app.command(name="providers")
def agents_providers(
    name: str | None = None, state: str | None = None, *, json_out: bool = False,
) -> None:
    """Which agent CLIs drive agents here — list them, or switch one on/off.

    ``galaius agents providers`` lists each provider as ``<name> <on|off> <availability>``, then
    the named agents and permission intents it can resolve. On a PC connected to a workspace the
    switches are the workspace's (its agent catalog, changed on the web) and this command only lists
    them; on a PC connected to none, ``galaius agents providers codex off`` writes
    ``~/.galaius/agents.json``. Unmentioned providers are ON: a CLI you installed is one you meant to use.

    Parameters
    ----------
    name
        A provider (claude, codex, ...). Omit to list.
    state
        ``on`` or ``off``.
    json_out
        Print provider availability and policy switches as JSON for installed clients.
    """


    policy = Policy.load()
    if name is not None:
        if json_out:
            print("JSON output is for listing providers; omit name and state", file=sys.stderr)
            raise SystemExit(2)
        if state not in ("on", "off"):
            print(f"say 'on' or 'off' for {name!r} — not {state!r}", file=sys.stderr)
            raise SystemExit(2)
        if name not in PROVIDERS:
            print(f"unknown provider {name!r}; galaius knows: {', '.join(PROVIDERS)}",
                  file=sys.stderr)
            raise SystemExit(2)
        try:
            policy.set_provider_active(name, state == "on")
        except PolicyError as err:
            print(f"ERROR: {err}", file=sys.stderr)
            raise SystemExit(2) from err
        print(f"{name} {state}")
        return
    if json_out:
        # `web`: where a workspace-connected PC's programs are switched (its page on the web), so a picker opens it instead
        # of writing a switch this PC no longer reads; None on a PC connected to no workspace.
        print(json.dumps({"providers": [
            {"id": p.name, "label": p.name, "active": policy.provider_active(p.name),
             "available": p.available()}
            for p in PROVIDERS.values()
        ], "web": (f"{page}&section=web-limit" if (page := PresenceFiles.default().read().page) else policy.catalog.connection.endpoint) if policy.catalog is not None else None,
           "switch_place": policy.switch_place()}))
        return
    for p in PROVIDERS.values():
        switch = "on" if policy.provider_active(p.name) else "off"
        avail = "available" if p.available() else f"not installed (no {p.binary!r} on PATH)"
        # `<name> <on|off>` first: the panel's toggle parses exactly those two tokens.
        print(f"{p.name:8} {switch:3} {avail}{provider_caveat_note(p)}")
        print(f"           image attachments: {'supported' if p.image_attachment_support() else 'unsupported'}")
        if definitions := p.agent_definitions():
            print(f"           agents: {', '.join(definitions)}")
        if modes := p.permission_modes():
            print(f"           permission modes: {', '.join(m.id for m in modes)}")


@agents_app.command(name="definitions")
def agents_definitions(provider: str | None = None) -> None:
    """Print the agent definitions this CLI can resolve, one per line.

    Machine-readable on purpose: the panel's picker used to scrape the human `agents providers`
    output, so a wording change would silently empty it. Nothing printed when there are none —
    prose here would parse as an agent called "no agents found".
    """

    providers = [provider_for(provider)] if provider is not None else list(agent_providers.PROVIDERS.values())
    for name in sorted({name for item in providers for name in item.agent_definitions()}):
        print(name)


@agents_app.command(name="discovered")
def agents_discovered() -> None:
    """Print the agent sessions galaius did NOT start, one JSON object per line.

    Your own editor windows. No record on disk — found by asking each provider at list time — so
    a front end reading the registry directory (the VS Code panel does) can't see them at all,
    however many icons it has for them.

    One object per line, not one array: a front end can parse incrementally, and a truncated
    write costs one session, not the whole list. Nothing printed when there are none; a provider
    that can't look is skipped, not taking the caller down with it — a failed discovery must never
    blank an otherwise-fine panel.
    """


    try:
        found = reg._discover_foreign()
    except (OSError, ValueError) as e:
        # Loud, on stderr, non-zero — never a silent empty answer. Each provider already swallows
        # its own failure, so reaching here means a real bug; printing nothing would be
        # indistinguishable from "you have no other sessions" — the panel would cache the empty
        # answer and re-cache it every refresh with no line saying why.
        print(f"ERROR: could not ask the providers what is running — {e}", file=sys.stderr)
        raise SystemExit(1) from None
    for raw in found:
        # The RECORD builds itself. Used to re-declare the same nine fields, so a field added to
        # AgentRun would reach the listing but not this JSON — which the VS Code panel parses, so
        # the panel would silently lack it with nothing to notice.
        run = reg.AgentRun.from_foreign(raw)
        if run is not None:
            print(run.model_dump_json())


@agents_app.command(name="modes")
def agents_modes(provider: str = DEFAULT_PROGRAM) -> None:
    """Print the shared permission intents this CLI accepts, one tab-separated row per intent.

    ``id<TAB>label<TAB>what it lets the agent do<TAB>yes|no`` — last column is whether the intent
    acts without asking, so a front end can mark it without inferring danger from wording.
    Machine-readable for the same reason `agents definitions` is: the panel used to scrape the
    human table, and a rewording emptied it silently.

    Nothing printed for a provider whose flags aren't verified against a real binary — prose here
    would parse as an intent called "no modes found".
    """

    for mode in provider_for(provider).permission_modes():
        print(f"{mode.id}\t{mode.label}\t{mode.detail}\t{'yes' if mode.unrestricted else 'no'}")


def modes_by_provider(values: list[str] | None) -> dict[str, str]:
    modes: dict[str, str] = {}
    for value in values or ():
        name, separator, mode = value.partition("=")
        if not separator or not name or not mode:
            raise ValueError("--provider-mode expects provider=mode")
        provider_for(name).validate_permission_mode(mode)
        if name in modes and modes[name] != mode:
            raise ValueError(f"Conflicting workspace modes for {name!r}")
        modes[name] = mode
    return modes
