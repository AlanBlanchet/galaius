"""Unified ``interact`` command line — one entry point for every front-end.

``interact mcp`` runs the stdio MCP server (what clients launch); the rest give
non-VS-Code clients parity with the extension's UI: ``install`` registers the server
with a client, ``config`` persists model/key settings, ``doctor``/``providers`` report
the environment. Heavy modules (the server, the model registry, Playwright) are
imported inside the commands that need them so ``--help``, ``install`` and ``config``
start instantly.
"""

from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter

from interact import installed_version
from interact.clients import ClientTarget, MCPServer, Scope
from interact.userconfig import UserConfig

app = App(
    name="interact",
    version=installed_version(),
    help="Browser + desktop automation MCP server, plus tools to install and configure it.",
)


@app.command
def mcp() -> None:
    """Run the MCP server over stdio. Clients launch this; register it with `interact install`."""
    # Keys/settings (config.env + project .env) are loaded centrally in main() before dispatch.
    from interact.server import main as serve  # deferred: pulls in Playwright + litellm + FastMCP

    serve()


@app.command
def status(
    project: Annotated[Path, Parameter(name=["--project", "-p"])] = Path("."),
) -> None:
    """Show how interact is set up: which clients it's registered with, the configured
    models and desktop target, which API keys are present, and recent usage. The
    at-a-glance overview for a normal user (grounding/scenario probes live in the tests).

    Parameters
    ----------
    project
        Project root to check for project-scoped client registrations (default: cwd).
    """
    from interact.clients import ClientTarget
    from interact.models import Model, ModelCapability
    from interact.runtime import config
    from interact.usage import UsageReport

    root = project.resolve()
    print("interact status\n")

    print("Registered with (interact install <client> to add):")
    bound = False
    for target in ClientTarget.all():
        registrations = target.registrations(root)
        if registrations:
            bound = True
            print(f"  ✓ {target.label}: {', '.join(registrations)}")
    if not bound:
        print("  (none yet)")

    print("\nModels:")
    for role in ("image", "component", "video"):
        print(f"  {role:<10} {getattr(config, f'{role}_model') or '(auto — best available)'}")
    target_line = f"target={config.desktop_target}"
    if config.desktop_target == "nested":
        target_line += f"  headless={config.nested_headless}  display=:{config.nested_display}"
    print(f"  desktop    {target_line}")

    Model.load_registry()
    providers = Model.available_providers()
    grounding = Model.available_by_capability(ModelCapability.GUI_GROUNDING)
    print(f"\nAPI keys: {', '.join(providers) or 'none — interact config set OPENAI_API_KEY …'}")
    print(f"Grounding models ready: {len(grounding)}")

    report = UsageReport.build(since_days=30)
    print(f"\nUsage (last 30d): {report.entries} calls, ${report.total_cost:.4f}   (details: interact usage)")


@app.command
def install(
    client: str | None = None,
    *,
    scope: Scope = Scope.user,
    project: Path = Path("."),
    dev_from: Path | None = None,
    dry_run: bool = False,
) -> None:
    """Register the interact MCP server with a coding client.

    Parameters
    ----------
    client
        Client id (omit to list them): claude, cursor, codex, vscode, copilot, windsurf, zed, claude-desktop.
    scope
        ``user`` (default) or ``project`` config location.
    project
        Project root for ``--scope project`` (default: current directory).
    dev_from
        Run the server from a local checkout via ``uvx --from <path> interact mcp``.
    dry_run
        Print what would be written/run without changing anything.
    """
    if client is None:
        print("Known clients (use: interact install <client>):\n")
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

    server = MCPServer.resolve(dev_from=dev_from)
    result = target.install(server, scope, project.resolve(), dry_run)
    icon = {"wrote": "✓ wrote", "ran": "✓ ran", "manual": "→ manual", "skipped": "• skipped"}[result.action]
    print(f"{icon}: {target.label}  [{result.target}]")
    if result.detail:
        print(result.detail)
    print(f"\nLaunches: {server.command} {' '.join(server.args)}")
    print(f"Docs: {target.doc_url}")
    if target.note and result.action in ("wrote", "ran"):
        print(f"Note: {target.note}")


@app.command
def providers() -> None:
    """List providers and grounding models available in the current environment."""
    from interact.models import Model, ModelCapability

    Model.load_registry()
    available = Model.available_providers()
    print(f"Available providers ({len(available)}): {', '.join(available) or 'none — no API keys found'}")
    grounding = Model.available_by_capability(ModelCapability.GUI_GROUNDING)
    print(f"\nGrounding models ready ({len(grounding)}):")
    for model in grounding:
        print(f"  {model.id:<40} ${model.cost_score:.2f}/Mtok")


@app.command
def dashboard() -> None:
    """Show the status dashboard (providers, models, grounding) in the terminal.

    Renders the same declarative `View` an HTTP endpoint will serve to the browser
    and the VS Code webview — defined once, shown on every surface. No VLM calls.
    """
    from interact.render import CliRenderer
    from interact.runtime import config
    from interact.view import View

    CliRenderer.render(View.dashboard(config))


@app.command
def usage(
    days: Annotated[int | None, Parameter(name=["--days", "-d"])] = None,
) -> None:
    """Summarise local VLM spend, tokens and calls by model and provider.

    Reads ~/.interact/logs/usage.jsonl (the same log the VS Code dashboard charts) —
    no network, no API calls. This is the CLI's usage analysis.

    Parameters
    ----------
    days
        Only count calls from the last N days (default: all time).
    """
    from interact.usage import UsageReport, default_log_path

    log_path = default_log_path()
    report = UsageReport.build(since_days=days)
    window = f"last {days}d" if days else "all time"
    if report.entries == 0:
        where = "no calls recorded yet" if not log_path.exists() else f"no calls in the {window}"
        print(f"interact usage ({window}): {where}\n  log: {log_path}")
        return

    print(f"interact usage ({window}) — {report.entries} calls, ${report.total_cost:.4f}, "
          f"{report.total_input:,} in / {report.total_output:,} out tokens\n")
    print(f"  {'model':<40} {'calls':>6} {'in':>10} {'out':>10} {'cost':>10}")
    for group in report.by_model:
        print(f"  {group.name:<40} {group.calls:>6} {group.input_tokens:>10,} "
              f"{group.output_tokens:>10,} ${group.cost:>9.4f}")
    if len(report.by_provider) > 1:
        print("\n  by provider:")
        for group in report.by_provider:
            print(f"  {group.name:<40} {group.calls:>6} {' ':>10} {' ':>10} ${group.cost:>9.4f}")


@app.command
def update(check: Annotated[bool, Parameter(name=["--check", "-c"])] = False) -> None:
    """Update interact to the latest GitHub release (or just check with --check).

    Parameters
    ----------
    check
        Only report whether an update is available; don't install it.
    """
    import shutil
    import subprocess

    from interact.update import REPO, available_update, installed_version

    current = installed_version()
    newer = available_update()
    if not newer:
        print(f"interact {current} is up to date.")
        return
    print(f"Update available: {current} → {newer}")
    if check:
        print("Run `interact update` to install it.")
        return
    source = f"git+https://github.com/{REPO}"
    if shutil.which("uv"):
        subprocess.run(["uv", "tool", "install", "--force", source], check=True)
    elif shutil.which("pipx"):
        subprocess.run(["pipx", "install", "--force", source], check=True)
    else:
        print("Need uv or pipx to update. See install.sh.")
        raise SystemExit(1)
    print(f"✓ updated to {newer}")


@app.command
def report(
    title: str,
    body: str,
    kind: Annotated[str, Parameter(name=["--kind", "-k"])] = "bug",
) -> None:
    """Report a problem or idea about interact itself to its maintainers (GitHub issue).

    The shell twin of the MCP ``report_issue`` tool — any agent or user with a terminal can
    send feedback without an MCP connection: ``interact report "title" "what happened"``.

    Parameters
    ----------
    title
        One-line summary of the problem or request.
    body
        What happened, what you expected, any repro steps or environment details.
    kind
        bug | limitation | feedback.
    """
    from interact import feedback

    print(feedback.report(title, body, kind))


@app.command
def doctor() -> None:
    """Diagnose the environment: command, providers, Playwright, desktop capture."""
    import os
    import shutil

    from interact.models import Model, ModelCapability

    print("interact doctor\n")
    print(f"  command       : {shutil.which('interact') or 'NOT on PATH'}")
    print(f"  config file   : {UserConfig.PATH} ({'present' if UserConfig.PATH.exists() else 'absent'})")

    try:
        import playwright  # noqa: F401

        print(f"  playwright    : installed (browser: {os.environ.get('INTERACT_BROWSER_TYPE', 'chromium')})")
    except ImportError:
        print("  playwright    : MISSING — run `uv run playwright install`")

    from interact.runtime import config

    session = os.environ.get("XDG_SESSION_TYPE", "?")
    display = os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")
    maim = shutil.which("maim")
    print(f"  desktop       : session={session} display={display or 'none'} maim={maim or 'MISSING (apt install maim)'}")
    if session == "wayland":
        print("                  note: browser + the nested sandbox + uinput input work on "
              "Wayland; local (non-nested) window capture/enumeration is X11-only for now "
              "→ prefer `desktop.target=nested`.")
    print(f"  desktop target: {config.desktop_target}  (set: interact config set desktop.target local|nested)")
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
    print(f"  providers     : {', '.join(available) or 'none — set a provider API key'}")
    print(f"  grounding     : {len(grounding)} model(s) ready")


config_app = App(name="config", help="Persist model/key settings to ~/.interact/config.env.")
app.command(config_app)


def _mask(name: str, value: str) -> str:
    return f"{value[:4]}…{value[-4:]}" if name.endswith("_API_KEY") and len(value) > 8 else value


@config_app.command(name="list")
def config_list() -> None:
    """Show persisted settings (secrets masked)."""
    data = UserConfig.read()
    if not data:
        print(f"No settings yet. Set one with: interact config set image.model <id>\n({UserConfig.PATH})")
        return
    for name, value in sorted(data.items()):
        print(f"  {name} = {_mask(name, value)}")


@config_app.command(name="get")
def config_get(key: str) -> None:
    """Print one persisted value."""
    value = UserConfig.get(key)
    print(value if value is not None else f"{UserConfig.normalize_key(key)} is unset")


@config_app.command(name="set")
def config_set(key: str, value: str) -> None:
    """Persist a setting. e.g. `interact config set image.model gpt-4o` or `... OPENAI_API_KEY sk-...`."""
    env = UserConfig.set(key, value)
    print(f"✓ {env} = {_mask(env, value)}  →  {UserConfig.PATH}")


@config_app.command(name="unset")
def config_unset(key: str) -> None:
    """Remove a persisted setting."""
    env = UserConfig.normalize_key(key)
    print(f"✓ removed {env}" if UserConfig.unset(key) else f"{env} was not set")


@config_app.command(name="path")
def config_path() -> None:
    """Print the config file path."""
    print(UserConfig.PATH)


def main() -> None:
    from interact.versioning import force_utf8_io

    force_utf8_io()  # UTF-8 stdout so glyphs (✓, →) don't crash on Windows' cp1252 console
    # Load keys/settings once for EVERY command: persisted config first, then a project
    # `.env` (both override=False, so precedence is host env > config.env > .env).
    # Centralised here so providers/doctor/status/dashboard, the TUI and the server all
    # see the same keys — previously only `mcp` loaded .env, so `interact providers`
    # reported none inside a checkout that had a .env.
    UserConfig.apply()
    from interact.dotenv_loader import load_dotenv_for_cli

    load_dotenv_for_cli()
    # Bare `interact` in a terminal opens the configuration TUI (configure models/keys,
    # see bindings, view usage — no commands to memorise). With args, or when stdout
    # isn't a TTY (piped/scripted), behave as the normal command-line app.
    import sys

    if len(sys.argv) == 1 and sys.stdout.isatty():
        from interact.tui import run as run_tui

        run_tui()
        return
    app()
