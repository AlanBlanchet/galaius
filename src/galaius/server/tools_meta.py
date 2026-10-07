"""Meta MCP tools that aren't about a page or a window: report_issue (feedback channel) and
list_providers (what VLM models/keys are configured)."""

import asyncio
import json
import os

import litellm as _litellm

from galaius.agents.providers import PROVIDERS, AgentProvider
from galaius.server.core import mcp


async def _subscription_provider_state(provider: AgentProvider) -> dict:
    """Credential-free readiness for one registered subscription CLI."""
    if not provider.available():
        return {
            "provider": provider.name,
            "cli": provider.binary,
            "installed": False,
            "authenticated": None,
            "action": f"Install and log in with the {provider.binary} CLI",
        }
    try:
        authenticated = await provider.subscription_authenticated(
            provider.subscription_env(), timeout=3
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        authenticated = None
    action = (
        "Ready"
        if authenticated is True
        else f"Log in with the {provider.binary} CLI"
        if authenticated is False
        else f"Check the {provider.binary} CLI login; authentication status was unavailable"
    )
    return {
        "provider": provider.name,
        "cli": provider.binary,
        "installed": True,
        "authenticated": authenticated,
        "action": action,
    }


@mcp.tool(category="feedback")
async def report_issue(title: str, body: str, kind: str = "bug") -> str:
    """Report a problem, missing capability, or feedback about GALAIUS ITSELF — not the
    site/app you're automating — to its maintainers, so it gets fixed. Use when galaius errors
    in a way that blocks you, behaves unexpectedly, or is missing something you needed.

    Files a GitHub issue on galaius's repo when gh is authed; otherwise opens the prefilled
    issue page in the user's browser (they just press Submit — tell them). Don't include
    secrets/credentials; galaius appends its version + platform itself.
    kind: bug | limitation | feedback.
    """
    from galaius.feedback import report

    return report(title, body, kind)


@mcp.tool(category="vision")
async def list_providers() -> str:
    """Return subscription visual CLIs, API/local providers, and current configuration.

    Use to discover what models can be passed as the 'model' override to
    get_interactive_elements and screenshot tools.
    """
    from galaius.server.core import config
    config.refresh()

    # Extension declaratively passes which providers have keys configured
    declared = os.environ.get("GALAIUS_CONFIGURED_PROVIDERS", "")
    if declared:
        available = set(declared.split(","))
    else:
        # Fallback: scan env against litellm known providers
        known_providers: set[str] = {
            info.get("litellm_provider", "") for info in _litellm.model_cost.values()
        }
        known_providers.discard("")

        _extra_keys: dict[str, list[str]] = {
            "ollama": ["OLLAMA_API_KEY"],
            "zai": ["ZAI_API_KEY"],
        }
        _provider_aliases: dict[str, str] = {"google": "gemini"}

        available: set[str] = set()
        for key, val in os.environ.items():
            if not val:
                continue
            if key.endswith("_API_KEY"):
                candidate = key.removesuffix("_API_KEY").lower()
                candidate = _provider_aliases.get(candidate, candidate)
                if candidate in known_providers:
                    available.add(candidate)
            for provider, keys in _extra_keys.items():
                if key in keys:
                    available.add(provider)

    from galaius.models import Model

    # Off the event loop: loading the registry now asks a running Ollama daemon what it has, and
    # a host that ACCEPTS then stalls would otherwise block every other MCP call on this server
    # for the length of the timeouts.
    await asyncio.to_thread(Model.load_registry)
    # A provider that ANSWERED us is available whatever the key scan above concluded — a local
    # Ollama needs no key at all, so the scan alone would both hide it and then warn about a
    # model pinned to it. Discovery is the authority here.
    available |= Model.live_providers()

    def _resolved(role: str) -> str | None:
        try:
            return config.resolve_model(role)
        except RuntimeError:
            return None  # nothing clears the criterion right now — named below in warnings

    result: dict = {
        "config": {
            "image_criteria": config.criteria_for("image"),
            "component_criteria": config.criteria_for("component"),
            "video_criteria": config.criteria_for("video"),
            "image_resolved": _resolved("image"),
            "component_resolved": _resolved("component"),
            "video_resolved": _resolved("video"),
        },
        "available_providers": sorted(available),
        "media": {
            "backend": config.media_backend,
            "billing": config.media_billing,
            "no_extra_usage_confirmed_for": list(
                config.media_session_no_extra_usage_confirmed_for
            ),
            "provider_order": list(config.media_provider_order),
            "timeout_seconds": config.media_timeout,
            "models": {
                name: config.media_model_for(name) or None
                for name in config.media_provider_order
            },
            "subscription_providers": await asyncio.gather(*(
                _subscription_provider_state(PROVIDERS[name])
                for name in config.media_provider_order
            )),
            "audio_boundary": (
                "Audio uses its configured API/local-compatible backend only when billing is "
                "api_allowed; Claude subscription sessions handle visual media, not "
                "transcription."
            ),
            "session_credit_limit": (
                "Each installed provider runs; one not listed in media.noExtraUsageConfirmedFor "
                "logs one warning per process that its account-side extra usage may bill past "
                "the plan. galaius cannot inspect those account settings."
            ),
        },
    }

    # An agent picking a model over MCP cannot see the user's daemon, so name what it actually
    # serves — otherwise the only discoverable models are the ones baked into the catalog.
    # Memoised by the load_registry call above, so this is a dict lookup, not a second round trip.
    from galaius import ollama

    served = ollama.serving()
    if served:
        result["ollama"] = {
            "endpoint": served[0].base,
            "models": [
                {"id": m.model_id, "vision": m.vision, "cloud": m.cloud} for m in served
            ],
        }

    # Warn — by name, never blank — on a role whose criterion clears nothing right now, via the
    # same resolver every VLM call uses, not a second re-implementation of the check.
    warnings = []
    for role in ("image", "component", "video"):
        if result["config"][f"{role}_resolved"] is None:
            warnings.append(
                f"{role}: '{config.criteria_for(role)}' clears nothing — "
                f"{config.explain_model(role)}"
            )
    if warnings:
        result["warnings"] = warnings

    return json.dumps(result, indent=2)
