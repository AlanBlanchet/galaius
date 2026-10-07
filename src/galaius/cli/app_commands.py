"""Deferred command implementations for the lightweight root CLI."""

import asyncio
import json
import os
import platform as _pf
import shutil
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path
from collections.abc import Callable
from typing import Annotated, TypeVar, cast
from uuid import UUID

from cyclopts import App, Parameter
from pydantic import ValidationError

from galaius import feedback, live_sources, model_catalog, ollama
from galaius_core import AGENT_TOUCH_SCOPES

from galaius.agents import messaging
from galaius.agents import providers as agent_providers
from galaius.agents import registry as reg
from galaius.agents.catalog import AgentCatalog
from galaius.agents.catalog_connection import CatalogConnection, CatalogConnectionError
from galaius.fence import FenceSpec
from galaius.agents.host import run_console
from galaius.agents.policy import ParadigmProjection, Policy, PolicyError, policy_path
from galaius.agents.providers import (
    MEDIA_PROVIDERS,
    PROVIDERS,
    available_providers,
    provider_caveat_note,
    provider_for,
)
from galaius.agents.run import ModelUnavailable, is_criterion, Ranking, rank, resolve_model, run_agent
from galaius.cli.clients import ClientTarget, MCPServer, Scope
from galaius.cli.command_bootstrap import Config, UserConfig
from galaius.cli.render import CliRenderer
from galaius.cli.usage import UsageReport, default_log_path
from galaius.cli.view import View
from galaius.criteria import Criteria, CriteriaError, Variables
from galaius.desktop import backend as desktop_backend
from galaius.desktop import orphans
from galaius.extension_status import deliver_extension, extension_status
# Shared normalizer: the registry needs it too, to meet a leaderboard NAME with a catalog ID —
# lives beside the catalog, not this command surface.
from galaius.model_catalog import bare_model_name as _bare_model_name
from galaius.models import Model, ModelCapability, _ordinal
from galaius.runtime import _LiveConfig, config
from galaius.server_registry import kill_stale_servers, latest_version, stale_servers
from galaius.server_tool_settings import PORTABLE_ENV, MachineNotice, ServerToolSettings, ToolSettingsConflict
from galaius.server_workspace import ServerWorkspace
from galaius.cli.workspace import WorkspaceCLI


def _print_media_transport(config: Config | _LiveConfig, indent: str = "  ") -> None:
    """Print the configured visual transport and registry-backed subscription CLI readiness."""
    order = tuple(config.media_provider_order)
    print(
        f"{indent}media        : backend={config.media_backend} "
        f"billing={config.media_billing}  order={' → '.join(order) or 'none'}"
    )
    for name in order:
        provider = MEDIA_PROVIDERS[name]
        availability = (
            "installed — subscription login is verified when a visual request runs"
            if provider.available()
            else f"not installed — install and log in with the {provider.binary} CLI"
        )
        warning = config.extra_usage_warning(name)
        confirmation = (
            f"warning: {warning}" if warning
            else "no-extra-usage confirmed (account state remains unverifiable)"
        )
        print(f"{indent}  · {name:<10} {availability}; {confirmation}")
    if config.media_billing == "session_only":
        print(
            f"{indent}  no metered API fallback by galaius; vendor session credit controls "
            "remain account-side; audio is disabled until media.billing=api_allowed"
        )
    elif config.media_backend == "session":
        print(
            f"{indent}  visual work stays on sessions; audio may use its configured API/local "
            "backend"
        )
    else:
        print(f"{indent}  metered API use is explicitly allowed")


def _print_resolved_models(indent: str = "  ") -> None:
    """Print which model each VLM TASK resolves to with current keys — answers "what runs when I
    do X, and why that default?". Organised BY TASK (what a user actually picks) with the config
    knob (the role) named in brackets — different jobs can have different defaults without a
    separate model setting per task (else all resolve to the same best VLM). Real axes: MODALITY
    (image/component/video/audio) and STAKES — review_ui/verify_ui layer a quality tier on the
    image model, the sovereign tier's cheapest clearing model at low/medium, frontier at
    high/critical. Shared by status / providers / doctor / version so every surface tells one
    story. Every role is a CRITERION now — this always shows what it RESOLVES TO, never the bare
    rule text alone, and says plainly when nothing resolves (a blank here is the defect the owner
    has complained about before)."""

    Model.load_registry()

    def _resolved(role: str) -> tuple[str, str]:
        """(display string, bare model id) for a role given the current keys."""
        rule = config.criteria_for(role)
        try:
            mid = config.resolve_model(role)
        except RuntimeError as err:
            return f"— (criterion: {rule})  ⚠ {err}", ""
        return f"{mid} (criterion: {rule})", mid

    def _line(task: str, role: str) -> None:
        print(f"{indent}{task:<33}→ {_resolved(role)[0]}  [{role}]")
        # What it stepped over. "Are we using the best model?" is unanswerable from the chosen id
        # alone — reads the same whether the walk found the strongest available or a stale default
        # returned with no walk at all. Naming the stronger models it couldn't reach, and why,
        # makes the answer checkable and shows which key to add.
        explanation = config.explain_model(role)
        if explanation:
            print(f"{indent}{'':<33}  ↑ {explanation}")

    _line("screenshot(query) · describe", "image")
    _line("get_interactive_elements", "component")
    _line("transcribe", "audio")
    _line("record · video understanding", "video")

    # review_ui / verify_ui pick by STAKES, not role: sovereign tier's cheapest clearing model for
    # a quick look (low/medium), frontier image model for final sign-off (high/critical).
    frontier = _resolved("image")[1] or "—"
    sovereign = config.resolve_quality_model("low")
    if sovereign:
        critique = f"{sovereign} (low/medium tier) · {frontier} (high/critical)"
    else:
        critique = f"{frontier} (all tiers — sovereign criteria {config.tier_sovereign_criteria or '(default)'!r} clears nothing, so low/medium fall back to frontier)"
    print(f"{indent}{'review_ui · verify_ui (critique)':<33}→ {critique}")
    print(f"{indent}{'measure_ui (contrast)':<33}→ deterministic — no model")


#: Enough to see the shortlist without burying the rest of `doctor` — a cloud catalog is ~19 rows.
_OLLAMA_SHOWN = 8


def _print_ollama(indent: str = "  ") -> None:
    """Name what the Ollama daemon actually serves, so "did galaius see the model I pulled?" is
    answerable without guessing.

    Vision models are named individually — those are the ones galaius can DRIVE; the rest are
    counted, not listed, since a wall of text-only ids would bury the rest of doctor with nothing
    actionable. Silent when there's no daemon — someone not running Ollama must not read a line
    about it.
    """

    found = ollama.serving()
    if not found:
        return
    # Where they CAME from, not where we looked first: discovery walks a ladder, and this is also
    # the endpoint a completion is sent to — the two stay checkable against each other.
    where = found[0].base or ollama.candidate_bases()[0]
    vision = sorted((m for m in found if m.vision), key=lambda m: m.name)
    rest = sorted((m for m in found if not m.vision), key=lambda m: m.name)

    usable = f"{len(vision)} vision-capable" if vision else "none vision-capable"
    print(f"{indent}{'ollama':<14}: {len(found)} available on {where} — {usable}")
    for model in vision[:_OLLAMA_SHOWN]:
        print(f"{indent}  · {model.describe()}")
    if len(vision) > _OLLAMA_SHOWN:
        print(f"{indent}  · … +{len(vision) - _OLLAMA_SHOWN} more vision models")
    if rest:
        names = ", ".join(m.name for m in rest[:_OLLAMA_SHOWN])
        more = f", +{len(rest) - _OLLAMA_SHOWN} more" if len(rest) > _OLLAMA_SHOWN else ""
        print(f"{indent}  · text-only ({len(rest)}): {names}{more}")


def _print_stale_servers(indent: str = "  ", fix: bool = False) -> None:
    """Flag any RUNNING MCP server that loaded an OLDER version than is now available — the silent
    stale-server trap (a long-lived `galaius mcp` keeps serving code imported at startup, so a
    shipped fix never reaches it until reconnected). Names the pid so the user knows which editor
    window's server to reconnect. With ``fix=True`` (``galaius doctor --fix``) it restarts them
    itself (SIGTERM → editor respawns galaius on current code). Nothing printed when every server
    is current."""

    stale = stale_servers()
    if not stale:
        return
    latest = latest_version()
    if fix:
        killed = kill_stale_servers()  # only pids CONFIRMED terminated — never an unverified claim
        unresolved = [s for s in stale if s.get("pid") not in killed]
        if killed:
            pids = ", ".join(str(p) for p in killed)
            print(f"{indent}✓ stopped {len(killed)} stale MCP server(s) (pid {pids}), each was serving "
                  f"code older than v{latest}. Most hosts start a fresh v{latest} process on their next "
                  f"tool call — if galaius tools are still missing afterward, reconnect (or fully "
                  f"restart) that MCP connection in your editor; a stopped process is not a guarantee "
                  f"every host reconnects on its own (#144).")
        if unresolved:
            pids = ", ".join(str(s.get("pid")) for s in unresolved)
            print(f"{indent}⚠ could not confirm {len(unresolved)} stale server(s) stopped (pid {pids}) — "
                  f"not galaius mcp, already gone, or no permission to signal it. Reconnect that MCP "
                  f"connection (or fully restart the editor) to load v{latest}.")
        return
    print(f"{indent}⚠ stale MCP server(s) — serving code older than this tree; reconnect to load fixes")
    print(f"{indent}   (add --fix to this command to stop them):")
    for s in stale:
        if s.get("reason") == "code":
            # The common case between releases: the version never moved, the code did.
            print(f"{indent}    pid {s['pid']}: v{s.get('version')} — started before the last edit")
        else:
            print(f"{indent}    pid {s['pid']}: v{s.get('version')} (tree is v{latest})")


def _print_extension_status(indent: str = "  ", fix: bool = False) -> None:
    """Flag a VS Code extension the user is running that is not the one in this tree.

    A version or compiled-bundle mismatch needs installation; a current bundle loaded before its
    build needs only a restart. Remedies are intentionally distinct — neither substitutes for the
    other — and nothing is printed when the running editor has this build.
    """

    st = extension_status()
    if not st:
        return
    if st["remedy"] == "install":
        if st["reason"] == "version":
            print(
                f"{indent}⚠ VS Code extension v{st['installed']} installed, "
                f"tree is v{st['tree']} — repackage and reinstall to load fixes"
            )
        else:
            print(
                f"{indent}⚠ VS Code extension v{st['installed']} compiled bundle differs from "
                "this tree — repackage and reinstall before restarting"
            )
        if fix:
            # Pair detection with remedy, only on explicit --fix: installing an extension makes VS
            # Code reload its extension hosts, killing whatever session asked for it — often this
            # command's own.

            print(f"{indent}   packaging and installing v{st['tree']}…")
            if deliver_extension():
                print(f"{indent}   ✓ installed v{st['tree']}. FULLY CLOSE AND REOPEN a window — a "
                      "reload is served by the same extension host and will not pick it up.")
            else:
                print(f"{indent}   could not deliver it; the messages above say why.")
    else:
        n, total = st.get("behind", 1), st.get("running", 1)
        print(f"{indent}⚠ VS Code extension v{st['installed']} was rebuilt after {n} of {total} "
              "running editor process(es) started — those are serving the OLD build")
        print(f"{indent}   a reinstall at the same version does NOT reach a running window; "
              "fully restart it (or use a fresh --user-data-dir)")


def _print_sandboxes(real_display: str | None) -> None:
    """Which sandbox displays exist right now, and who owns each.

    Answers "why do I see two Xephyr windows?" — every galaius server owns its own sandbox, so
    one window per server that launched something is BY DESIGN. Not by design: a window whose
    owner is gone, called out here and swept on the next launch.
    """

    try:
        servers = orphans._list_x_servers()
    except (OSError, subprocess.SubprocessError):
        return
    if not servers:
        return
    print(f"  sandboxes     : {len(servers)} open (one per galaius server that has launched an app)")
    for srv in servers:
        where = orphans.display_of(srv.cmdline) or "?"
        clients = len(orphans.display_clients(where)) if where != real_display else 0
        owner = "ORPHANED — no owner left; swept on the next launch" if orphans.is_orphan(srv) \
            else f"owner pid {srv.ppid}"
        print(f"                  {where}  {clients} app(s)  ({owner})")


def status(
    project: Annotated[Path, Parameter(name=["--project", "-p"])] = Path("."),
    *,
    fix: bool = False,
) -> None:
    """Show how galaius is set up: which clients it's registered with, the configured
    subscription-media policy, models and desktop target, optional API keys, and recent usage. The
    at-a-glance overview for a normal user (grounding/scenario probes live in the tests).

    Parameters
    ----------
    project
        Project root to check for project-scoped client registrations (default: cwd).
    fix
        Stop any stale MCP server this same check just flagged, so your editor starts a fresh one
        (issue #144: `status` used to only name the problem, leaving `doctor --fix` — a second,
        undiscoverable command — as the only repair; this is that repair, inline).
    """

    root = project.resolve()
    print("galaius status\n")
    _print_stale_servers(fix=fix)

    print("Registered with (galaius install <client> to add):")
    bound = False
    for target in ClientTarget.all():
        registrations = target.registrations(root)
        if registrations:
            bound = True
            print(f"  ✓ {target.label}: {', '.join(registrations)}")
    if not bound:
        print("  (none yet)")

    print("\nMedia analysis:")
    _print_media_transport(config)

    print("\nModels (API/local roles and fallback models — pin via `galaius config set`):")
    _print_resolved_models()
    if config.media_billing == "session_only":
        print("  note       API/local role models are inactive while media.billing=session_only")

    if not desktop_backend.desktop_supported():
        print("  desktop    not available on this OS (Linux/X11 only) — browser automation works here")
    else:
        target_line = f"target={config.desktop_target}"
        if config.desktop_target == "nested":
            target_line += f"  headless={config.nested_headless}  display=:{config.nested_display}"
        print(f"  desktop    {target_line}")

    Model.load_registry()
    providers = Model.available_providers()
    grounding = Model.available_by_capability(ModelCapability.GUI_GROUNDING)
    print(
        f"\nAPI keys (optional for visual subscriptions; required by configured audio/API paths): "
        f"{', '.join(providers) or 'none'}"
    )
    print(f"Grounding models ready: {len(grounding)}")

    report = UsageReport.build(since_days=30)
    session_note = (
        f"; {report.session_usage_calls} session-usage call(s), account impact unknown"
        if report.session_usage_calls
        else ""
    )
    print(
        f"\nUsage (last 30d): {report.entries} calls, observed metered API spend "
        f"${report.total_cost:.4f}{session_note}   (details: galaius usage)"
    )


def install(
    client: str | None = None,
    *,
    scope: Scope = Scope.user,
    project: Path = Path("."),
    dev_from: Path | None = None,
    dry_run: bool = False,
) -> None:
    """Register the galaius MCP server with a coding client.

    Parameters
    ----------
    client
        Client id (omit to list them): claude, cursor, codex, vscode, copilot, windsurf, zed, claude-desktop.
    scope
        ``user`` (default) or ``project`` config location.
    project
        Project root for ``--scope project`` (default: current directory).
    dev_from
        Run the server from a local checkout via ``uvx --from <path> galaius mcp``.
    dry_run
        Print what would be written/run without changing anything.
    """
    if client is None:
        print("Known clients (use: galaius install <client>):\n")
        for target in ClientTarget.all():
            scopes = [s.value for s in Scope if target.path_for(s, Path(".")) is not None]
            print(f"  {target.id:<16} {target.label:<24} scopes: {', '.join(scopes) or 'cli'}")
            if target.note:
                print(f"  {'':<16} {target.note}")
        print("\n  (copilot is an alias of vscode)")
        return

    target = ClientTarget.by_id(client)
    if target is None:
        print(f"Unknown client '{client}'. Known: {', '.join(ClientTarget.ids())}")
        raise SystemExit(2)

    root = project.resolve()
    server = MCPServer.resolve(
        dev_from=dev_from,
        portable=scope == Scope.project,
        project=root,
    )
    result = target.install(server, scope, root, dry_run)
    icon = {"wrote": "✓ wrote", "ran": "✓ ran", "manual": "→ manual", "skipped": "• skipped"}[result.action]
    print(f"{icon}: {target.label}  [{result.target}]")
    if result.detail:
        print(result.detail)
    print(f"\nLaunches: {server.command} {' '.join(server.args)}")
    print(f"Docs: {target.doc_url}")
    if target.note and result.action in ("wrote", "ran"):
        print(f"Note: {target.note}")
    if target.id == "codex" and result.action in ("wrote", "ran", "skipped"):
        print("Start a fresh Codex session, or restart the Codex IDE extension, then verify:")
        print("  codex mcp get galaius")


def providers() -> None:
    """List subscription CLIs plus API/local providers and grounding models."""

    Model.load_registry()
    available = Model.available_providers()
    print("Subscription visual providers:")
    _print_media_transport(Config())
    print(
        f"\nAPI/local providers ({len(available)}): "
        f"{', '.join(available) or 'none (subscription visual sessions can still run)'}"
    )

    print("\nResolved selection (what each tool uses, given your keys):")
    _print_resolved_models()

    grounding = Model.available_by_capability(ModelCapability.GUI_GROUNDING)
    print(f"\nGrounding models ready ({len(grounding)}):")
    for model in grounding:
        print(f"  {model.id:<40} ${model.cost_score:.2f}/Mtok")


def dashboard() -> None:
    """Show the status dashboard (providers, models, grounding) in the terminal.

    Renders the same declarative `View` an HTTP endpoint will serve to the browser
    and the VS Code webview — defined once, shown on every surface. No VLM calls.
    """

    CliRenderer.render(View.dashboard(config))


def usage(
    days: Annotated[int | None, Parameter(name=["--days", "-d"])] = None,
) -> None:
    """Summarise local VLM spend, tokens and calls by model and provider.

    Reads ~/.galaius/out/usage.jsonl (the same log the VS Code dashboard charts) —
    no network, no API calls. This is the CLI's usage analysis.

    Parameters
    ----------
    days
        Only count calls from the last N days (default: all time).
    """

    log_path = default_log_path()
    report = UsageReport.build(since_days=days)
    window = f"last {days}d" if days else "all time"
    if report.entries == 0:
        where = "no calls recorded yet" if not log_path.exists() else f"no calls in the {window}"
        print(f"galaius usage ({window}): {where}\n  log: {log_path}")
        return

    unknown = (
        f"; {report.session_usage_calls} session-usage call(s) with unknown account impact"
        if report.session_usage_calls
        else ""
    )
    print(
        f"galaius usage ({window}) — {report.entries} calls, observed metered API spend "
        f"${report.total_cost:.4f}{unknown}, {report.total_input:,} in / "
        f"{report.total_output:,} out tokens\n"
    )
    print(f"  {'model':<40} {'calls':>6} {'in':>10} {'out':>10} {'cost':>10}")
    for group in report.by_model:
        cost = (
            f"${group.cost:.4f} + unknown"
            if group.unknown_cost_calls and group.cost
            else "unknown"
            if group.unknown_cost_calls
            else f"${group.cost:.4f}"
        )
        print(
            f"  {group.name:<40} {group.calls:>6} {group.input_tokens:>10,} "
            f"{group.output_tokens:>10,} {cost:>10}"
        )
    if len(report.by_provider) > 1:
        print("\n  by provider:")
        for group in report.by_provider:
            cost = (
                f"${group.cost:.4f} + unknown"
                if group.unknown_cost_calls and group.cost
                else "unknown"
                if group.unknown_cost_calls
                else f"${group.cost:.4f}"
            )
            print(f"  {group.name:<40} {group.calls:>6} {' ':>10} {' ':>10} {cost:>10}")


def report(
    title: str,
    body: str,
    kind: Annotated[str, Parameter(name=["--kind", "-k"])] = "bug",
) -> None:
    """Report a problem or idea about galaius itself to its maintainers (GitHub issue).

    The shell twin of the MCP ``report_issue`` tool — any agent or user with a terminal can
    send feedback without an MCP connection: ``galaius report "title" "what happened"``.

    Parameters
    ----------
    title
        One-line summary of the problem or request.
    body
        What happened, what you expected, any repro steps or environment details.
    kind
        bug | limitation | feedback.
    """

    print(feedback.report(title, body, kind))


def doctor(*, fix: bool = False) -> None:
    """Diagnose the environment: command, providers, Playwright, desktop capture.

    Parameters
    ----------
    fix
        Deliver this tree to what you're actually running: restart any stale `galaius mcp`
        server (it respawns on current code from your editor), repackage + reinstall the VS Code
        extension when the installed one is older than this tree. One-step cure for "I shipped
        the fix but the bug persists" — a real failure mode here: a day's work once sat
        undelivered behind a warning this command could only print.

        Installing an extension makes VS Code reload its extension hosts, interrupting sessions
        in your editor windows — why it never happens without --fix.
    """


    print("galaius doctor\n")
    _print_stale_servers(fix=fix)
    _print_extension_status(fix=fix)
    print(f"  command       : {shutil.which('galaius') or 'NOT on PATH'}")
    print(f"  config file   : {UserConfig.PATH} ({'present' if UserConfig.PATH.exists() else 'absent'})")

    if find_spec("playwright") is not None:
        print(f"  playwright    : installed (browser: {os.environ.get('GALAIUS_BROWSER_TYPE', 'chromium')})")
    else:
        print("  playwright    : MISSING — run `uv run playwright install`")


    _print_media_transport(config)

    # Desktop diagnostics are Linux/X11-specific (maim, /dev/uinput, Xephyr) — on macOS/Windows
    # they'd print misleading "MISSING (apt install …)" advice, so report cleanly that desktop
    # automation is N/A and browser is the ready path.
    if not desktop_backend.desktop_supported():

        print(f"  desktop       : not available on {_pf.system()} — desktop automation is "
              "Linux/X11 only (issues/24); browser automation works fully here.")
    else:
        session = os.environ.get("XDG_SESSION_TYPE", "?")
        display = os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")
        maim = shutil.which("maim")
        print(f"  desktop       : session={session} display={display or 'none'} maim={maim or 'MISSING (apt install maim)'}")
        _print_sandboxes(display)
        if session == "wayland":
            print("                  note: browser + the nested sandbox + uinput input work on "
                  "Wayland; local (non-nested) window capture/enumeration is X11-only for now "
                  "→ prefer `desktop.target=nested`.")
        print(f"  desktop target: {config.desktop_target}  (set: galaius config set desktop.target local|nested)")
        if config.desktop_target == "nested":
            server = "Xvfb" if config.nested_headless else "Xephyr"
            pkg = "xvfb" if config.nested_headless else "xserver-xephyr"
            server_path = shutil.which(server) or f"MISSING (apt install {pkg})"
            xdotool = shutil.which("xdotool") or "MISSING (apt install xdotool)"
            mode = "headless/background" if config.nested_headless else "visible"
            print(f"  nested deps   : {server}={server_path} ({mode}) xdotool={xdotool} display=:{config.nested_display}")
        uinput = "writable" if os.access("/dev/uinput", os.W_OK) else "NOT writable — add a udev rule + join `input` group (no root)"
        print(f"  input driver  : /dev/uinput {uinput}  (absolute pointer, works on X11 + Wayland)")

    Model.load_registry()
    available = Model.available_providers()
    grounding = Model.available_by_capability(ModelCapability.GUI_GROUNDING)
    print(
        f"  API/local     : {', '.join(available) or 'none (subscription visuals need no API key)'}"
    )
    print(f"  grounding     : {len(grounding)} model(s) ready")
    _print_ollama()
    print("  selection     : (what each tool resolves to — answers 'why is my default X?')")
    _print_resolved_models(indent="    ")


config_app = App(name="config", help="Personal portable settings on the server; credentials and machine settings stay local.")


def _settings_status(*, allow_stale: bool = True) -> dict:
    server = ServerToolSettings.configured()
    if server is None:
        machine = MachineNotice.connection()
        return {"configured": False, "source": "local", "stale": False, "revision": None,
                "values": {key: value for key, value in UserConfig.read_local().items() if key in PORTABLE_ENV},
                "portable_keys": list(PORTABLE_ENV)} | ({"notice": str(MachineNotice(machine))} if machine else {})
    return server.read(allow_stale=allow_stale).status()


@config_app.command(name="status")
def config_status(*, json_out: bool = False) -> None:
    """Report portable settings source, account, revision and explicit cache staleness."""
    WorkspaceCLI.emit(_settings_status)


#: The tools that pick their model by rule, as `ToolRoleModels.role` names them; "sovereign" is the
#: quick (low / medium) review tier.
_TOOL_MODEL_ROLES = ("image", "component", "video", "audio", "sovereign")


def _tool_models() -> dict:
    """Each tool's rule and what it resolves to HERE, best first: the rule is the account's, the
    keys that decide what clears it are this computer's."""
    from galaius.config.settings import _TIER_SOVEREIGN_DEFAULT_CRITERIA

    config.refresh()
    Model.load_registry()
    roles = []
    for role in _TOOL_MODEL_ROLES:
        written = (config.tier_sovereign_criteria if role == "sovereign" else getattr(config, f"{role}_criteria")) or ""
        criterion = written.strip() or (_TIER_SOVEREIGN_DEFAULT_CRITERIA if role == "sovereign" else config.criteria_for(role))
        try:
            rule = Criteria.parse(criterion)
            rule.validate_weights(config.criteria_weights)
            ranked = rule.ranked(available_only=True, weights=config.criteria_weights)
            reason = "" if ranked else rule.explain(available_only=True)
        except CriteriaError as error:
            ranked, reason = [], str(error)
        roles.append({"role": role, "criterion": criterion, "configured": bool(written.strip()),
                      "models": [{"provider": model.provider, "model": model.id} for model in ranked[:5]],
                      "reason": reason[:600]})
    return {"roles": roles}


@config_app.command(name="models")
def config_models(*, json_out: bool = False) -> None:
    """What each tool's model rule (image, component, video, audio, and the quick review tier)
    resolves to on this computer, best first. Change a rule with `galaius config set
    image.criteria "…"`; it applies on every computer of your account."""
    if json_out:
        WorkspaceCLI.emit(_tool_models)
        return
    for role in _tool_models()["roles"]:
        chosen = ", ".join(item["model"] for item in role["models"]) or f"— {role['reason']}"
        print(f"{role['role']:<10} {role['criterion']}{'' if role['configured'] else ' (default)'}\n           → {chosen}")


@config_app.command(name="sync")
def config_sync(*, json_out: bool = False) -> None:
    """Refresh the replaceable personal settings cache online; never upload local entries."""
    WorkspaceCLI.emit(lambda: _settings_status(allow_stale=False))


@config_app.command(name="import-preview")
def config_import_preview(*, json_out: bool = False) -> None:
    """Show only allowlisted local portable overrides for review; never save or print secrets."""
    def preview():
        status = _settings_status(allow_stale=False)
        local = {key: value for key, value in UserConfig.read_local().items() if key in PORTABLE_ENV}
        return {**status, "changes": {key: {"current": status["values"].get(key), "proposed": value}
                for key, value in local.items() if status["values"].get(key) != value}}

    WorkspaceCLI.emit(preview)


def _mask(name: str, value: str) -> str:
    return f"{value[:4]}…{value[-4:]}" if name.endswith("_API_KEY") and len(value) > 8 else value


@config_app.command(name="list")
def config_list() -> None:
    """Show persisted settings (secrets masked)."""
    server = UserConfig.server()
    data = UserConfig.read_local()
    if server:
        snapshot = server.read()
        data = {key: value for key, value in data.items() if key not in PORTABLE_ENV} | snapshot.env
        print(f"Personal server settings · revision {snapshot.settings.revision} · {'STALE cache' if snapshot.stale else 'current'}; other settings local")
    if not data:
        print(f"No settings yet. Set one with: galaius config set image.model <id>\n({UserConfig.PATH})")
        return
    for name, value in sorted(data.items()):
        print(f"  {name} = {_mask(name, value)}")


@config_app.command(name="get")
def config_get(key: str) -> None:
    """Print one persisted value."""
    env = UserConfig.normalize_key(key)
    server = UserConfig.server() if env in PORTABLE_ENV else None
    if server:
        snapshot = _refused_plainly(server.read)
        value = snapshot.env.get(env)
        print(f"Personal server settings · revision {snapshot.settings.revision} · {'STALE cache' if snapshot.stale else 'current'}")
    else:
        value = UserConfig.get(key)
    print(value if value is not None else f"{UserConfig.normalize_key(key)} is unset")


@config_app.command(name="set")
def config_set(key: str, value: str, *, expected_revision: int | None = None, account_id: UUID | None = None, json_out: bool = False) -> None:
    """Persist a setting. e.g. `galaius config set image.model gpt-4o` or `... OPENAI_API_KEY sk-...`.

    Connected portable preferences save on the server without a model call.
    Local API keys and standalone model pins use the existing configuration probe.
    """
    if json_out:
        WorkspaceCLI.emit(lambda: _settings_change(key, value, expected_revision, account_id))
        return
    env = _refused_plainly(lambda: UserConfig.set(key, value))
    source = "personal server settings" if env in PORTABLE_ENV and UserConfig.server() else str(UserConfig.PATH)
    print(f"✓ {env} = {_mask(env, value)}  →  {source}")
    if env in PORTABLE_ENV and UserConfig.server():
        return
    _maybe_probe(env, key, value)


def _maybe_probe(env: str, key: str, value: str) -> None:
    """Verify a just-set model key or pin with one cheap vision call."""
    from galaius.cli import config_check

    if env.endswith("_API_KEY"):
        if config_check.ConfigCheck._criterion_for(env) is None:
            return
        verdict = config_check.ConfigCheck.run(config_check.ConfigCheck.key(env))
    elif key in ("image.model", "image-model", "component.model", "video.model"):
        from galaius.config.settings import config

        if config.media_billing != "api_allowed":
            return  # probe would run through a session backend, not this pin
        verdict = config_check.ConfigCheck.run(config_check.ConfigCheck.model(value))
    else:
        return
    print(config_check.ConfigCheck.line(verdict))


@config_app.command(name="unset")
def config_unset(key: str, *, expected_revision: int | None = None, account_id: UUID | None = None, json_out: bool = False) -> None:
    """Remove a persisted setting."""
    if json_out:
        WorkspaceCLI.emit(lambda: _settings_change(key, None, expected_revision, account_id))
        return
    env = UserConfig.normalize_key(key)
    print(f"✓ removed {env}" if _refused_plainly(lambda: UserConfig.unset(key)) else f"{env} was not set")


_T = TypeVar("_T")


def _refused_plainly(write: Callable[[], _T]) -> _T:
    """A refused settings write (machine connection, conflict, signed out) is its sentence and exit 1."""
    try:
        return write()
    except CatalogConnectionError as refused:
        if not (isinstance(refused, MachineNotice) and MachineNotice.said):
            print(refused, file=sys.stderr)
        raise SystemExit(1) from None


def _settings_change(key: str, value: str | None, revision: int | None, account_id: UUID | None) -> dict:
    env = UserConfig.normalize_key(key)
    if env not in PORTABLE_ENV:
        raise ValueError("JSON settings bridge accepts portable settings only")
    server = ServerToolSettings.configured()
    if server is None:
        if revision is not None or account_id is not None:
            raise ToolSettingsConflict("Server connection removed. Reload before saving; draft retained.")
        UserConfig.update({env: value})
        return _settings_status()
    base = server.read(allow_stale=False)
    if revision is None or account_id is None or base.settings.revision != revision or base.account_id != account_id:
        raise ToolSettingsConflict("Settings account or revision changed. Reload; draft retained.")
    return server.update({env: value}, base=base).status()


@config_app.command(name="path")
def config_path() -> None:
    """Print the config file path."""
    print(UserConfig.PATH)


agents_app = App(name="agents", help="Spawn and supervise agent runs across providers.")


@agents_app.command(name="sync")
def agents_sync(
    endpoint: Annotated[str | None, Parameter(name="--endpoint")] = None,
    machine: bool = False,
    workspace: Annotated[str | None, Parameter(name="--workspace")] = None,
    token_file: Annotated[Path | None, Parameter(name="--token-file")] = None,
) -> None:
    """Refresh the server-owned role catalog; save connection settings after validation.

    First sync requires --machine (this PC's own link: its server, company and machine
    token) or --endpoint with --token-file. Later syncs reuse the saved connection.
    --workspace selects a workspace UUID; otherwise the authenticated bootstrap supplies it.
    """
    try:
        if machine:
            if endpoint is not None or workspace is not None or token_file is not None:
                raise ValueError("--machine takes the server and company from this PC's link")
            connection = CatalogConnection.linked()
            if connection is None:
                raise ValueError("this PC is not linked to a Galaius server; run `galaius login`")
        elif endpoint is None:
            if workspace is not None or token_file is not None:
                raise ValueError("connection changes require an explicit --endpoint")
            connection = CatalogConnection.load()
            if connection is None:
                raise ValueError("first sync requires --machine, or --endpoint with --token-file")
        else:
            if token_file is None:
                raise ValueError("--endpoint requires --token-file")
            connection = CatalogConnection(
                endpoint=endpoint.rstrip("/"), workspace_id=workspace,
                auth_mode="token", token_file=token_file.absolute(),
            )
        catalog = AgentCatalog.refresh(connection)
        catalog.connection.save()
    except ValidationError:
        print("ERROR: invalid catalog connection; check endpoint, workspace UUID and auth mode", file=sys.stderr)
        raise SystemExit(2) from None
    except (ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({
        "status": "current", "workspace": str(catalog.connection.workspace_id),
        "agents": len(catalog.snapshot.agents), "paradigms": len(catalog.snapshot.paradigms),
        "cursor": catalog.snapshot.cursor, "fetched_at": catalog.fetched_at.isoformat(),
    }))


@agents_app.command(name="policy-hook")
def agents_policy_hook() -> None:
    """Apply the shared agent policy to Codex lifecycle events on stdin; no model call."""
    from galaius.agents.codex_policy_hook import main

    raise SystemExit(main())


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
    if not runs:

        names = ", ".join(p.name for p in available_providers()) or "none installed"
        print(f"No agent runs. Providers available here: {names}.")
        return
    for run in runs:
        cost = f"~${run.cost_usd:.4f}" if run.cost_usd is not None else "—"
        mark = "*" if run.foreign else " "
        said = run.interruption.describe() if run.interruption else run.last
        print(f"{mark} {run.status:8} {run.run_id[:8]}  {run.provider:7} {run.name:16} "
              f"{cost:>10}  {said[:60]}")
    if any(r.foreign for r in runs):
        print("\n* not started by galaius")


def _spawn_on_machine(machine: str, folder: str | None, task: str, *, agent: str | None, provider: str | None, model: str | None, permission: str | None) -> None:
    """`agents spawn --machine`: the run id of an agent started on another computer, or one line why not."""
    try:
        if folder is None:
            raise ValueError("--machine needs --folder: an agent folder of that computer, or a folder beneath one")
        if permission is not None and permission not in AGENT_TOUCH_SCOPES:
            raise ValueError(f"--permission-mode on another computer is one of {', '.join(AGENT_TOUCH_SCOPES)}")
        workspace = ServerWorkspace.configured()
        print(workspace.start_on_machine(workspace.machine(machine), folder, task, role=agent, provider=provider, model=model, permission=permission))
    except (ValueError, CatalogConnectionError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from None


@agents_app.command(name="events")
def agents_events(run_id: str, limit: int = 30, machine: str | None = None) -> None:
    """Print what an agent run has been doing. Takes the short id `agents list` prints; with
    --machine, the full id of a run started on that computer (`agents spawn --machine`)."""

    if machine is not None:
        try:
            workspace = ServerWorkspace.configured()
            events = workspace.machine_run_events(workspace.machine(machine), UUID(run_id))
        except (ValueError, CatalogConnectionError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            raise SystemExit(2) from None
        for line in events.items[-max(1, limit):]:
            print(f"  {line.kind:11} {line.text}")
        return
    resolved = reg.resolve_run_id(run_id) or run_id
    events = reg.read_events(resolved)
    if not events:
        print(f"No events for {run_id!r}.")
        return
    for event in events[-max(1, limit):]:
        print(f"  {event.kind:11} {event.summary(viewer=resolved)}")


@agents_app.command(name="stop")
def agents_stop(run_id: str) -> None:
    """Stop a running agent and the tools it spawned. Takes the short id `agents list` prints."""

    resolved = reg.resolve_run_id(run_id)
    if resolved is None:
        print(f"No agent run {run_id!r}.")
        return
    print(f"Stopped {resolved[:8]}." if reg.stop(resolved) else f"No agent run {run_id!r}.")


def refresh_live_data() -> None:
    """Re-fetch the live model catalog and benchmark scores the dashboard reads.

    Python is the SOLE writer of those cache files — the extension used to fetch and write the
    catalog itself with a narrower schema, so whichever side wrote last decided whether prices
    existed. Now the extension asks for this instead. A source that did not refresh is printed
    with its reason and the age of the copy still served, and the command exits 1.
    """

    outcomes = live_sources.refresh_all()
    done = [name for name, why in outcomes.items() if why is None]
    if done:
        print("Refreshed: " + ", ".join(done))
    for name, why in outcomes.items():
        if why is not None:
            print(f"{name} NOT refreshed: {why}")
    if len(done) < len(outcomes):
        raise SystemExit(1)


@agents_app.command(name="send")
def agents_send(run_id: str, message: str) -> None:
    """Send a message to a running agent — it answers with its full context intact.

    How the VS Code panel lets you join the conversation instead of only watching: the agent
    resumes its own session, so it remembers what it already did and its reply lands in the same
    transcript the panel shows.
    """


    delivery = messaging.deliver_message(run_id, message)
    if (refused := messaging.wait_for_start(delivery)) is not None:
        print(f"ERROR: the resumed turn did not start: {refused}")
        raise SystemExit(1)
    print(delivery.text)
    if delivery.state == "error":
        raise SystemExit(1)


@agents_app.command(name="variables")
def agents_variables() -> None:
    """Every comparison a model criterion can make, and who measured each one.

    Discovery surface for writing a criterion: can't write ``aa.mmmu > 0.7`` if nothing says it
    exists. Each variable is namespaced by its SOURCE — a bare ``intelligence`` hides who measured
    it, and two leaderboards rarely agree.
    """

    rows = Variables.all()
    width = max(len(v.name) for v in rows)
    for v in sorted(rows, key=lambda v: v.name):
        kind = "yes/no  " if v.flag else "measure "
        print(f"  {v.name:<{width}}  {kind} {v.describe}")
    print("\nWrite one as  cap.vlm and gui.screenspot > 0.85 and price.in < 10  — cheapest that"
          " clears every term is used, and re-resolved at every spawn.")


def _per_vendor(ranking: Ranking, policy: Policy) -> dict[str, str | None]:
    """What each switched-on vendor CLI would ACTUALLY run: its first candidate in the one ranked
    list a spawn walks — a CLI given alone gets that same list, filtered (`rank`)."""
    answers: dict[str, str | None] = {}
    # Read THROUGH the module: which CLIs exist is a fact about the caller's environment — a name
    # bound at import time would freeze whatever the process started with.
    for name in agent_providers.PROVIDERS:
        if not policy.provider_active(name):
            continue
        first = next((c for c in ranking.candidates if c.provider == name), None)
        answers[name] = None if first is None else resolve_model(
            first.model, dict(os.environ), provider=agent_providers.provider_for(name))[1]
    return answers


@agents_app.command(name="criterion")
def agents_criterion(criterion: str, json_out: bool = False) -> None:
    """Resolve current model: bare benchmarks rank; comparison terms filter.

    "We shouldn't write a model, but resolve a model from the constraints." A pinned id freezes
    when typed; a criterion is that claim written down instead, re-read every time it's asked.
    Nothing could ASK one what it currently picks, so every surface that helped choose could only
    offer a pinned id — the thing criteria exist to replace.

    Refuses loudly rather than falling back: silently resolving to some other model is worse than
    none — it looks like it worked.
    """
    try:
        parsed = Criteria.parse(criterion)
    except CriteriaError as err:
        if json_out:
            print(json.dumps({"criterion": criterion, "model": None, "why": str(err)}))
            return
        print(f"that criterion cannot be evaluated: {err}")
        raise SystemExit(1)
    chosen = parsed.choose()
    why = parsed.explain()
    if json_out:
        policy = Policy.load()
        try:
            ranking = rank(criterion, dict(os.environ), providers=list(agent_providers.PROVIDERS.values()), policy=policy)
        except ModelUnavailable:
            ranking = Ranking(candidates=())
        print(json.dumps({
            "criterion": criterion,
            "model": chosen.id if chosen else None,
            "score": chosen.intelligence_score if chosen else None,
            "competence": chosen.competence() if chosen else None,
            # What each switched-on CLI would run: the answer a spawn will actually get.
            "providers": _per_vendor(ranking, policy),
            "why": why,
        }))
        return
    if chosen is None:
        print(f"nothing clears {parsed} right now.\n{why}")
        raise SystemExit(1)
    print(f"{parsed}  →  {chosen.id}")
    print(f"  {chosen.competence()}")
    print(f"  {why}")


@agents_app.command(name="models")
def agents_models(
    limit: int = 0, json_out: bool = False, names: list[str] | None = None,
) -> None:
    """The models the registry has a capability score for, best first, ONE ROW PER MODEL.

    "Models should also spell out their intelligence score, such that we can compare the most
    competents and trust one agent more than another in some situations (isn't absolute)" — a
    choice is only a comparison if candidates are distinct, so a model's provider aliases
    (`azure/gpt-5.5`, `azure_ai/gpt-5.5-2026-04-23`, `vertex_ai/…`) collapse into one row carrying
    the plainest id: forty rows of four facts compared nothing. Rank is over distinct models. One
    measure among several — a lower-scored model is often the better fit for a narrow job.
    """
    # RANKED ON ONE VINTAGE. Registry keeps every baked score — selection/criteria need whatever
    # is known about a model — but a ranking PRINTED beside the panel's Benchmarks tab must be the
    # board that tab shows, or the two disagree in public about which model leads. Read THROUGH
    # the module, never bound by name: a board a test points elsewhere must move this command too.
    live = model_catalog.live_scores()

    class _Row:
        __slots__ = ("id", "intelligence_score")

        def __init__(self, id: str, intelligence_score: float | None) -> None:
            self.id, self.intelligence_score = id, intelligence_score

    # Every model this machine can REACH, scored by the board DIRECTLY. Reading the score off the
    # registry instead inverted the ask: registry holds none of the board's top models, so the
    # most competent ones printed "nothing scored carries this name" and every number on screen
    # approximated a superseded model. Ids come from registry AND browse catalogue — a model can
    # be routable through either.
    reachable: dict[str, _Row] = {}
    for model_id in [m.id for m in Model.catalog()] + [
        m.id for m in (model_catalog.load_catalog().models if live else [])
    ]:
        if model_id in reachable:
            continue
        measured = live.get(_bare_model_name(model_id)) if live else None
        if measured is None and live:
            continue
        reachable[model_id] = _Row(model_id, measured)
    if not live:  # no board on disk: fall back to whatever the registry knows
        reachable = {m.id: _Row(m.id, m.intelligence_score) for m in Model.catalog()
                     if m.intelligence_score is not None}
    scored = sorted(
        reachable.values(),
        key=lambda m: (-(m.intelligence_score or 0.0), len(m.id), m.id),
    )
    # One entry per distinct model: same score AND same bare name (id minus provider prefixes,
    # regions, date suffixes) is the same model resold under another name.
    distinct: dict[tuple[float, str], dict] = {}
    for m in scored:
        key = (m.intelligence_score or 0.0, _bare_model_name(m.id))
        entry = distinct.get(key)
        if entry is None:
            distinct[key] = {"id": m.id, "score": m.intelligence_score, "aliases": 0, "covers": []}
        else:
            entry["aliases"] += 1
            # Every absorbed id named: a SECOND list (browse catalogue) can drop what's already
            # ranked above it, instead of offering `openai/gpt-5.5` as "not scored" right under
            # `gpt-5.5` at "60.2 · 1st" — the picker contradicting itself.
            entry["covers"].append(m.id)
    rows = list(distinct.values())
    # Rank over the BOARD, never what this machine happens to reach. Denominator used to be
    # "models you can route to", so one model once read 10th from one directory, 13th from
    # another with different credentials in scope — a local-availability measure wearing an
    # intelligence label. Which models are LISTED still depends on what you can reach; the PLACE
    # doesn't. Within that, rank by COMPETITION: equal scores share one rank, say "joint" —
    # enumerating would manufacture the difference the measure denies.
    population = sorted(live.values(), reverse=True) or [r["score"] for r in rows]
    for row in rows:
        ahead = sum(1 for value in population if value > row["score"])
        tied = sum(1 for value in population if value == row["score"])
        joint = "joint " if tied > 1 else ""
        row["competence"] = (
            f"aa.intelligence {row['score']:.1f} · {joint}{_ordinal(ahead + 1)} of {len(population)}"
        )
    ranked_total = len(rows)  # before any --limit: the population the RANK is over
    listed = rows[:limit] if limit else rows
    # Rows a picker puts NEAREST THE TOP are aliases, not ids: Claude Code's `sonnet`/`opus` tiers,
    # whatever the agent runs on today. Unscored, they made the one comparison anyone makes — keep
    # the incumbent, or switch — impossible, so each resolves to the best-scored model it stands
    # for, carrying THAT model's score. An alias nothing scores stays absent.
    resolved = []
    for alias in names or []:
        want = _bare_model_name(alias)
        # EXACT first. A containment match is how a family word (`sonnet`, `opus`) finds the model
        # wearing it, but once let `claude-fable-5` take `claude-fable-5.1`'s number — a different
        # model four places higher — with nothing marking the swap, purely because one name
        # prefixes the other.
        hit = next((r for r in rows if _bare_model_name(r["id"]) == want), None)
        if hit is not None:
            resolved.append({**hit, "asked": alias})
            continue
        hit = next((r for r in rows if want in _bare_model_name(r["id"])), None)
        if hit is not None:
            # A bare family word asks for no particular version, so nothing was substituted. A
            # name carrying its OWN version did — an approximation that must say so.
            versioned = any(ch.isdigit() for ch in want)
            resolved.append({**hit, "asked": alias, **({"approximate": True} if versioned else {})})
            continue
        # Registry is OLDER than the fleet: an agent runs `claude-sonnet-5` while ranking still
        # knows `claude-sonnet-4-6`, so an exact match scores nothing and the one comparison
        # anyone makes has no left-hand side. Fall back to the closest ranked RELATIVE — row
        # sharing the most leading name-parts — flagged approximate so no surface passes it off as
        # the model itself. Sharing fewer than two parts is not a relative.
        parts = want.split("-")
        best, best_shared = None, 1
        for row in rows:  # rows are ranked, so the first of a tie is the best-scored
            other = _bare_model_name(row["id"]).split("-")
            shared = 0
            for mine, theirs in zip(parts, other):
                if mine != theirs:
                    break
                shared += 1
            if shared > best_shared:
                best, best_shared = row, shared
        if best is not None:
            resolved.append({**best, "asked": alias, "approximate": True})
    rows = listed + resolved
    if json_out:
        print(json.dumps(rows))
        return
    # Naming the SCOPE: the panel's Benchmarks tab shows the whole board, and one tab once called
    # a model first while another had it lower behind a different leader, with nothing saying this
    # one ranks only what this machine can route to. Header names BOTH populations — the
    # difference is the point: board ranks everyone, this list shows what you can actually run.
    print(
        f"aa.intelligence is one measure, not a verdict — {ranked_total} of the "
        f"{len(population)} models on the Artificial Analysis board are routable from here, and "
        "the place each holds is its place on that whole board.\n"
    )
    for row in rows:
        also = f"  (+{row['aliases']} provider aliases)" if row["aliases"] else ""
        stands_for = f"  [{row['asked']}]" if row.get("asked") else ""
        print(f"{row['score']:5.1f}  {row['id']}{also}{stands_for}")


@agents_app.command(name="policy")
def agents_policy(json_out: bool = False) -> None:
    """Active role policy: server catalog when configured, otherwise local policy.

    Each agent row shows the rule as WRITTEN and what it currently RESOLVES to, so a criterion
    quietly matching nothing is visible here rather than at the spawn that fails.

    `--json-out` hands the panel the same answer — otherwise it can only print the rule, leaving a
    reader unable to tell a RESOLUTION from a model somebody typed, the distinction the mechanism
    exists for.
    """
    policy = Policy.load()
    if json_out:
        # One shape test, one resolver, both sides: panel must not re-derive "is this a criterion"
        # in TypeScript, where it'd drift from the spawn's own answer.
        agents = []
        for agent in policy.agents:
            rule = policy.criterion_for(agent) or ""
            resolves: str | None = rule
            why = None
            ranking = Ranking(candidates=())
            if is_criterion(rule):
                try:
                    ranking = rank(rule, dict(os.environ), providers=list(agent_providers.PROVIDERS.values()), weights=policy.weights_for(agent), role=agent, policy=policy)
                    resolves = ranking.candidates[0].model
                except ModelUnavailable as err:
                    resolves, why = None, str(err).splitlines()[0]
            agents.append({
                "name": agent, "rule": policy.agents[agent], "criterion": is_criterion(rule),
                "resolves": resolves, "why": why,
                "ranked": [candidate.model_dump() for candidate in ranking.candidates[:3]],
                "dropped": [str(item) for item in ranking.dropped],
                "providers": _per_vendor(ranking, policy) if is_criterion(rule) else {},
            })
        print(json.dumps({
            "policy": "server catalog" if policy.catalog is not None else str(policy_path()),
            "catalog_cursor": policy.catalog.snapshot.cursor if policy.catalog is not None else None,
            "stale": policy.catalog.stale if policy.catalog is not None else False,
            "profiles": dict(policy.profiles), "agents": agents,
            "paradigms": {
                agent: [{"paradigm": a.paradigm, "as": a.projection} for a in assignments]
                for agent, assignments in policy.paradigms.items()
            },
        }))
        return
    print(f"policy: {'server catalog ' + policy.catalog.snapshot.cursor if policy.catalog is not None else policy_path()}")
    if policy.profiles:
        print("\nprofiles")
        for name, rule in policy.profiles.items():
            print(f"  @{name:<14} {rule}")
    if policy.agents:
        print("\nagents")
        for agent, rule in policy.agents.items():
            resolved = policy.criterion_for(agent) or ""
            shown = rule if rule == resolved else f"{rule}  →  {resolved}"
            note = ""
            ranking = Ranking(candidates=())
            if is_criterion(resolved):
                try:
                    ranking = rank(resolved, dict(os.environ), providers=list(agent_providers.PROVIDERS.values()), weights=policy.weights_for(agent), role=agent, policy=policy)
                    note = "  ranked: " + " → ".join(f"{item.provider}/{item.model}" for item in ranking.candidates[:3]) + " (availability checked at start)"
                except ModelUnavailable as err:
                    note = "  ⇒ NOTHING (" + str(err).splitlines()[0].rstrip(":") + ")"

            print(f"  {agent:<16} {shown}{note}")
            ahead = ranking.drops_ahead_of(3)
            for item in ahead:
                print(f"  {'':<16}   dropped {item}")
            if len(ranking.dropped) > len(ahead):
                print(f"  {'':<16}   + {len(ranking.dropped) - len(ahead)} more dropped further down the list (--json-out lists them)")
    if policy.toolsets:
        print("\ntoolsets")
        for name in policy.toolsets:
            print(f"  @{name:<14} {', '.join(policy.expand_toolset(name))}")
    if policy.agent_tools:
        print("\nagent tools")
        for agent in policy.agent_tools:
            print(f"  {agent:<16} {', '.join(policy.tools_for(agent))}")
    if policy.paradigms:
        print("\nparadigms")
        for agent, assignments in policy.paradigms.items():
            worn = ", ".join(f"{a.paradigm} (as {a.projection})" for a in assignments)
            print(f"  {agent:<16} {worn}")
    print("\nproviders")
    for name in PROVIDERS:
        print(f"  {name:<8} {'on' if policy.provider_active(name) else 'off'}")


@agents_app.command(name="paradigm-assign")
def agents_paradigm_assign(agent: str, paradigm: str, projection: str) -> None:
    """Give AGENT a PARADIGM, projected as "skill" (loaded on demand) or "system_prompt" (always
    present). Writes ~/.galaius/agents.json — the one file the spawn reads.

    "a user doesn't edit a 'system prompt' or a 'skill', but a paradigm, and choses to add it to
    an agent as 'skill' or as 'system prompt'." Same paradigm can be a skill for one agent, baked
    into another's system prompt — choice is per agent, never global to the paradigm.
    """
    if projection not in ("skill", "system_prompt"):
        print(f"projection must be 'skill' or 'system_prompt', not {projection!r}", file=sys.stderr)
        raise SystemExit(2)
    try:
        policy = Policy.load()
        policy.assign_paradigm(agent, paradigm, cast(ParadigmProjection, projection))
    except PolicyError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        raise SystemExit(2) from err
    print(f"{agent} now carries {paradigm!r} as {projection}.")


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


@agents_app.command(name="providers")
def agents_providers(
    name: str | None = None, state: str | None = None, *, json_out: bool = False,
) -> None:
    """Which agent CLIs drive agents here — list them, or switch one on/off.

    ``galaius agents providers`` lists each provider as ``<name> <on|off> <availability>``, then
    the named agents and permission intents it can resolve. ``galaius agents providers codex off``
    switches one off at the one place every spawn passes through (``~/.galaius/agents.json``), so
    the panel's toggle and this command read/write the same fact. Unmentioned providers are ON: a
    CLI you installed is one you meant to use.

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
        policy.set_provider_active(name, state == "on")
        print(f"{name} {state}")
        return
    if json_out:
        print(json.dumps({"providers": [
            {"id": p.name, "label": p.name, "active": policy.provider_active(p.name),
             "available": p.available()}
            for p in PROVIDERS.values()
        ]}))
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


#: A terminal launch waits for the vendor's own refusal rather than handing back a run that is
#: about to die at $0.00: the human is already watching this command, and a dead candidate costs
#: them a relaunch. In-process callers keep the short window.
CLI_QUOTA_WINDOW = 20.0


async def _run_agent_for_cli(provider, task, **kwargs):
    """Indirection the tests replace — spawning for real costs money and a live CLI."""

    kwargs.setdefault("quota_window", CLI_QUOTA_WINDOW)
    return await run_agent(provider, task, **kwargs)


def _agent_runner():
    """Honor an explicitly replaced CLI runner while command code remains deferred."""
    cli_module = sys.modules.get("galaius.cli.app")
    return getattr(cli_module, "_run_agent_for_cli", _run_agent_for_cli)


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
def agents_modes(provider: str = "claude") -> None:
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


def _provider_modes(values: list[str] | None) -> dict[str, str]:
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
                 machine: str | None = None, folder: str | None = None,
                 fence: Annotated[Path | None, Parameter(name="--fence")] = None) -> None:
    """Start an agent and return its id immediately, without waiting for it to finish.

    --machine <name or id> starts it on another of your computers instead, through the server, in
    --folder (an agent folder of that computer, or a folder beneath one); --permission-mode there
    is read_only or workspace_write, never more than that computer allows. Follow it with
    `galaius agents events <run id> --machine <name>`.

    `agents run` streams until the agent is done — right at a terminal, useless to a UI — the
    panel needs the id NOW to show the agent working. Nothing is lost by letting go: the child
    leads its own session and writes its own stream, so it outlives this process.

    --delegate selects the parent run's named capability and its pinned revision.
    --agent-id plus --agent-revision selects an exact server revision directly.
    --parent-run-id defaults to the calling agent's recorded run from its environment.
    --session-id records the owning conversation, otherwise inherited from the parent,
    GALAIUS_SESSION_ID, or the CLI harness CODEX_THREAD_ID. Unknown ownership stays unknown.
    Omit --provider to rank across providers. --deny-tool removes an exact tool for this run
    and its continuations; it never grants permissions or changes global configuration.
    --provider-mode provider=mode preserves a provider's existing workspace permission setting
    during automatic selection. An explicit --permission-mode takes precedence.
    --quota-window is how many seconds the new child is watched for a vendor quota refusal
    before the id is printed (a refusal inside it falls through to the next candidate); a
    supervisor that shows the run at once passes a short one.
    --fence names a file holding what the run's OS fence is built from (`galaius.fence.FenceSpec`
    JSON); every turn builds it again from the machine's levels. The machine runner writes it.
    """


    if machine is not None:
        _spawn_on_machine(machine, folder, task, agent=agent, provider=provider, model=model, permission=permission_mode)
        return
    if folder is not None:
        print("ERROR: --folder names a folder on another computer: pass --machine too (here, use --cwd)", file=sys.stderr)
        raise SystemExit(2)

    async def _go():
        handle = await _agent_runner()(
            provider_for(provider) if provider is not None else None, task, name=name or agent,
            cwd=cwd or os.getcwd(), agent=agent, model=model,
            permission_mode=permission_mode,
            image_paths=tuple(image_paths or ()),
            agent_ref=AgentCatalog.reference(agent_id, agent_revision),
            delegate=delegate, parent_run_id=parent_run_id,
            denied_tools=tuple(denied_tools or ()),
            provider_modes=_provider_modes(provider_modes),
            quota_window=quota_window,
            fence=FenceSpec.model_validate_json(fence.read_text()) if fence is not None else None,
        )
        # Give the child a moment to be alive before this process exits out from under it.
        await asyncio.sleep(0.2)
        return handle.run_id

    try:
        with reg.session_context(session_id, parent_run_id=parent_run_id, cli_harness=True) as owner:
            if owner is None:
                print("Session unknown: pass --session-id for conversation-scoped listings.", file=sys.stderr)
            print(asyncio.run(_go()))
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

    --delegate selects the parent run's pinned capability. --agent-id together with
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
