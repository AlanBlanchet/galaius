"""Durable accounting records shared by API and session media transports."""

import logging
from datetime import UTC, datetime
from typing import Literal

import litellm

from interact import runtime
from interact.config import Config
from interact.vision.types import VLMResult
from interact.vision.usage_records import UsageEntry

_log = logging.getLogger(__name__)


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
        usage = (
            response.get("usage")
            if isinstance(response, dict)
            else getattr(response, "usage", None)
        )
        try:
            cost = (
                litellm.completion_cost(completion_response=response)
                if response is not None
                else None
            )
        except Exception:  # noqa: BLE001 — third-party response shapes are not trustworthy
            cost = None
        input_tokens = (
            usage.get("prompt_tokens", usage.get("input_tokens", 0))
            if isinstance(usage, dict)
            else getattr(usage, "prompt_tokens", 0) if usage else 0
        )
        output_tokens = (
            usage.get("completion_tokens", usage.get("output_tokens", 0))
            if isinstance(usage, dict)
            else getattr(usage, "completion_tokens", 0) if usage else 0
        )
        UsageEntry(
            timestamp=datetime.now(UTC), model=model, backend="api",
            provider=provider,
            billing="metered_api", outcome=outcome, input_tokens=input_tokens,
            output_tokens=output_tokens, incremental_cost_usd=cost,
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
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            incremental_cost_usd=None,
            api_equivalent_cost_usd=result.api_equivalent_cost_usd, cost=None,
            request_id=result.request_id, session_id=result.session_id,
        ).append(config.usage_log)
    except Exception as exc:  # noqa: BLE001 — usage telemetry must not break a model result
        _log.debug("session usage-log write failed (%s)", type(exc).__name__)
