"""Meta MCP tools that aren't about a page or a window: report_issue (feedback channel) and
list_providers (what VLM models/keys are configured)."""

import json
import os

import litellm as _litellm

from interact.server.core import mcp


@mcp.tool()
async def report_issue(title: str, body: str, kind: str = "bug") -> str:
    """Report a problem, missing capability, or feedback about INTERACT ITSELF — not the site/app
    you're automating — to its maintainers, so it gets fixed. Use it when interact errors in a way
    that blocks you, behaves unexpectedly, or is missing something you needed.

    Files a GitHub issue on interact's repo when gh is authed; otherwise it opens the prefilled
    issue page in the user's browser (they just press Submit — tell them). Don't include
    secrets/credentials; interact appends its version + platform itself.
    kind: bug | limitation | feedback.
    """
    from interact.feedback import report

    return report(title, body, kind)


@mcp.tool()
async def list_providers() -> str:
    """Return available VLM providers, models, and current configuration.

    Use this to discover what models can be passed as the 'model' override
    to get_interactive_elements and screenshot tools.
    """
    from interact.server.core import config

    # Extension declaratively passes which providers have keys configured
    declared = os.environ.get("INTERACT_CONFIGURED_PROVIDERS", "")
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

    result: dict = {
        "config": {
            "image_model": config.image_model or None,
            "component_model": config.component_model or None,
            "video_model": config.video_model or None,
        },
        "available_providers": sorted(available),
    }

    # Warn on configured models whose provider has no key — via the env-key check, NOT
    # litellm.validate_environment (which can hang on interactive provider auth flows).
    from interact.models import Model

    Model.load_registry()
    warnings = []
    for model_name in [config.image_model, config.component_model, config.video_model]:
        if not model_name:
            continue
        model = Model.by_id(model_name)
        provider = model.provider if model else (model_name.split("/", 1)[0] if "/" in model_name else None)
        if provider and provider not in available:
            warnings.append(f"{model_name}: provider '{provider}' has no API key set")
    if warnings:
        result["warnings"] = warnings

    return json.dumps(result, indent=2)
