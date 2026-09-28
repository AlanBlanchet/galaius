"""Lightweight root command registry; implementations resolve only when selected."""

import importlib
import sys

from cyclopts import App

from interact import installed_version
from interact.upgrade.quiet import UpgradeReady
from interact.upgrade.store import EXIT_UPGRADE
from interact.upgrade.supervisor import Supervisor
from interact.versioning import force_utf8_io

app = App(
    name="interact",
    version=installed_version(),
    version_flags=["--version", "-v"],
    help="Browser + desktop automation MCP server, plus tools to install and configure it.",
)

app.command(
    "interact.cli.mcp_command:mcp", name="mcp",
    help="Run the MCP server over stdio.",
)
app.command(
    "interact.cli.app_commands:status", name="status",
    help="Show registrations, model setup, desktop readiness, and recent usage.",
)
app.command(
    "interact.cli.app_commands:install", name="install",
    help="Register the interact MCP server with a coding client.",
)
app.command(
    "interact.cli.app_commands:providers", name="providers",
    help="List subscription CLIs plus API/local providers and grounding models.",
)
app.command(
    "interact.cli.app_commands:dashboard", name="dashboard",
    help="Show the status dashboard in the terminal.",
)
app.command(
    "interact.cli.app_commands:usage", name="usage",
    help="Summarise local VLM spend, tokens, and calls.",
)
app.command(
    "interact.cli.upgrade_command:upgrade_app", name="upgrade",
    help="Automatic upgrades: status, check now, pin, turn off, go back.",
)
app.command(
    "interact.cli.app_commands:report", name="report",
    help="Report a problem or idea about interact.",
)
app.command(
    "interact.cli.app_commands:doctor", name="doctor",
    help="Diagnose the environment and optionally deliver fixes.",
)
app.command(
    "interact.cli.app_commands:refresh_live_data", name="refresh",
    help="Refresh the live model catalog and benchmark scores.",
)
app.command(
    "interact.cli.app_commands:config_app", name="config",
    help="Manage personal server preferences and local machine settings.",
)
app.command(
    "interact.cli.app_commands:agents_app", name="agents",
    help="Spawn and supervise agent runs across providers.",
)
app.command(
    "interact.cli.login_command:login", name="login",
    help="Connect this computer to your Interact account.",
)
app.command(
    "interact.cli.login_command:logout", name="logout",
    help="Disconnect this computer from your Interact account.",
)
app.command(
    "interact.cli.machine_command:machine_app", name="machine",
    help="Connect this computer as a workflow machine.",
)
app.command(
    "interact.cli.function_command:function_app", name="functions",
    help="Register and test typed functions this machine can run from a workflow.",
)
app.command(
    "interact.cli.workflows_command:workflows_app", name="workflows",
    help="Run a server workflow from this machine.",
)
app.command(
    "interact.cli.prompts:prompts_app", name="prompts",
    help="Author and synchronize prompts through a local Git worktree.",
)
app.command(
    "interact.cli.workspace:workspace_app", name="workspace",
    help="Inspect and edit the connected server workspace.",
)
app.command(
    "interact.cli.tui_command:tui", name="_tui", show=False,
    help="Open the interactive configuration dashboard.",
)


@app.command
def version() -> None:
    """Print the installed interact version."""
    print(installed_version())


def main() -> None:
    """A long-lived command (`mcp`, `machine connect`, the TUI) starts as its supervisor, which
    runs it again as a worker from the active runtime; a worker at its quiet point exits
    `EXIT_UPGRADE` for the supervisor to start the new runtime in its place."""
    force_utf8_io()
    interactive = sys.stdout.isatty()
    supervisor = Supervisor.for_arguments(tuple(sys.argv[1:]), interactive)
    if supervisor is not None:
        raise SystemExit(supervisor.run())
    try:
        app(["_tui"] if len(sys.argv) == 1 and interactive else None)
    except UpgradeReady:
        raise SystemExit(EXIT_UPGRADE) from None


_DEFERRED_EXPORTS = frozenset({
    "_bare_model_name",
    "_mask",
    "_per_vendor",
    "_print_media_transport",
    "_print_ollama",
    "_print_resolved_models",
    "_print_sandboxes",
    "_print_stale_servers",
    "_run_agent_for_cli",
    "agents_clear",
    "agents_console",
    "agents_criterion",
    "agents_definitions",
    "agents_discovered",
    "agents_events",
    "agents_list",
    "agents_models",
    "agents_modes",
    "agents_policy",
    "agents_providers",
    "agents_run",
    "agents_send",
    "agents_spawn",
    "agents_stop",
    "agents_sync",
    "agents_variables",
    "config_app",
    "config_get",
    "config_list",
    "config_path",
    "config_set",
    "config_unset",
    "dashboard",
    "doctor",
    "install",
    "providers",
    "refresh_live_data",
    "report",
    "status",
})


def __getattr__(name: str):
    """Resolve legacy direct imports without loading command dependencies at startup."""
    if name not in _DEFERRED_EXPORTS:
        raise AttributeError(name)
    commands = importlib.import_module("interact.cli.app_commands")
    return getattr(commands, name)


__all__ = ["app", "main", "version"]
