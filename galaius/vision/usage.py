"""Durable accounting records shared by API and session media transports."""

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from galaius_core import TokenCounts

from galaius import runtime
from galaius.config.settings import Config
from galaius.lazy import deferred
from galaius.vision.types import VLMResult
from galaius.vision.usage_records import UsageEntry

litellm = deferred("litellm")

_log = logging.getLogger(__name__)


def _figure(source: Any, *keys: str) -> int | None:
    """The first of `keys` `source` (an object or a dict) holds as a count."""
    for key in keys:
        value = source.get(key) if isinstance(source, dict) else getattr(source, key, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def response_usage(response: Any) -> TokenCounts:
    """What a litellm completion `response` (the object or its dict) reports of its tokens, in OpenAI's shape: the whole
    prompt in `prompt_tokens`, its cache reads inside it (`prompt_tokens_details.cached_tokens`); empty when it reports
    none."""
    usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    if usage is None:
        return TokenCounts()
    details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else getattr(usage, "prompt_tokens_details", None)
    return TokenCounts.of_prompt(_figure(usage, "prompt_tokens", "input_tokens"), _figure(usage, "completion_tokens", "output_tokens"),
                                 None if details is None else _figure(details, "cached_tokens"))


def log_api_attempt(
    model: str,
    provider: str,
    response,
    config: Config | None = None,
    *,
    outcome: Literal["succeeded", "failed", "cancelled"] = "succeeded",
) -> float | None:
    """Persist an API dispatch even when usage/cost metadata is absent or the call failed."""
    try:
        try:
            cost = (
                litellm.completion_cost(completion_response=response)
                if response is not None
                else None
            )
        except Exception:  # noqa: BLE001 — third-party response shapes are not trustworthy
            cost = None
        UsageEntry(
            timestamp=datetime.now(UTC), model=model, backend="api",
            provider=provider,
            billing="metered_api", outcome=outcome, usage=response_usage(response),
            incremental_cost_usd=cost,
            api_equivalent_cost_usd=cost, cost=cost,
        ).append((config or runtime.config).usage_log)
        return cost
    except Exception as exc:  # noqa: BLE001 — usage telemetry must not break a model result
        _log.debug("usage-log write failed (%s)", type(exc).__name__)
        return None


def log_session_attempt(
    result: VLMResult,
    config: Config,
    outcome: Literal["succeeded", "failed", "cancelled"],
) -> None:
    """Record one dispatched CLI turn; prompts, media, errors, and credentials are excluded."""
    try:
        UsageEntry(
            timestamp=datetime.now(UTC), model=result.model, backend="session",
            provider=result.provider, billing="session_usage", outcome=outcome,
            usage=result.usage,
            incremental_cost_usd=None,
            api_equivalent_cost_usd=result.api_equivalent_cost_usd, cost=None,
            request_id=result.request_id, session_id=result.session_id,
        ).append(config.usage_log)
    except Exception as exc:  # noqa: BLE001 — usage telemetry must not break a model result
        _log.debug("session usage-log write failed (%s)", type(exc).__name__)
