"""What the visual transport is set to and which subscription CLIs can serve it."""

from galaius.agents.providers import (
    MEDIA_PROVIDERS,
)
from galaius.cli.command_bootstrap import Config
from galaius.runtime import _LiveConfig


def print_media_transport(config: Config | _LiveConfig, indent: str = "  ") -> None:
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
