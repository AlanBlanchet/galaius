"""Status, install, doctor, report and the other whole-install commands."""

import os
import platform as _pf
import shutil
import subprocess
from importlib.util import find_spec
from pathlib import Path
from typing import Annotated
from cyclopts import Parameter
from galaius.diagnostics import feedback
from galaius.catalog import live_sources
from galaius.inference import ollama
from galaius.agents.providers import (
    MEDIA_PROVIDERS,
)
from galaius.cli.clients import ClientTarget, MCPServer, Scope
from galaius.cli.command_bootstrap import Config, UserConfig
from galaius.cli.render import CliRenderer
from galaius.cli.usage import UsageReport, default_log_path
from galaius.cli.view import View
from galaius.desktop import backend as desktop_backend
from galaius.desktop import orphans
from galaius.diagnostics.extension_status import deliver_extension, extension_status
from galaius.models.registry import Model
from galaius.models.capability import ModelCapability
from galaius.runtime import _LiveConfig, config
from galaius.mcp.registry import kill_stale_servers, latest_version, stale_servers


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
    if target.verify and result.action in ("wrote", "ran", "skipped"):
        print(target.verify)


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
        f"${report.total_cost:.4f}{unknown}, {report.usage.prompt:,} in / "
        f"{report.usage.output or 0:,} out tokens\n"
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
            f"  {group.name:<40} {group.calls:>6} {group.usage.prompt:>10,} "
            f"{group.usage.output or 0:>10,} {cost:>10}"
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
