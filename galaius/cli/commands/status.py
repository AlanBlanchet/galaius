"""`galaius status`, `providers` and `dashboard`: how galaius is set up here and what it can use."""

from pathlib import Path
from typing import Annotated
from cyclopts import Parameter
from galaius.cli.clients import ClientTarget
from galaius.cli.command_bootstrap import Config
from galaius.cli.render import CliRenderer
from galaius.cli.usage import UsageReport
from galaius.cli.view import View
from galaius.desktop import backend as desktop_backend
from galaius.models.registry import Model
from galaius.models.capability import ModelCapability
from galaius.runtime import config
from galaius.cli.health.media import print_media_transport
from galaius.cli.health.models import print_resolved_models
from galaius.cli.health.servers import print_stale_servers


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
    print_stale_servers(fix=fix)

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
    print_media_transport(config)

    print("\nModels (API/local roles and fallback models — pin via `galaius config set`):")
    print_resolved_models()
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
    api_providers = Model.available_providers()
    grounding = Model.available_by_capability(ModelCapability.GUI_GROUNDING)
    print(
        f"\nAPI keys (optional for visual subscriptions; required by configured audio/API paths): "
        f"{', '.join(api_providers) or 'none'}"
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


def providers() -> None:
    """List subscription CLIs plus API/local providers and grounding models."""

    Model.load_registry()
    available = Model.available_providers()
    print("Subscription visual providers:")
    print_media_transport(Config())
    print(
        f"\nAPI/local providers ({len(available)}): "
        f"{', '.join(available) or 'none (subscription visual sessions can still run)'}"
    )

    print("\nResolved selection (what each tool uses, given your keys):")
    print_resolved_models()

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
