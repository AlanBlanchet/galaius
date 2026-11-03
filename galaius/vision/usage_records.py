"""Dependency-light durable accounting records shared by media transports and clients."""

import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class UsageEntry(BaseModel):
    """Canonical on-disk media accounting row, owned by its only writer."""

    timestamp: datetime
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    backend: Literal["session", "api", "none"]
    provider: str = Field(min_length=1)
    billing: Literal["session_usage", "metered_api", "none"]
    outcome: Literal["succeeded", "failed", "cancelled"]
    request_id: str | None = None
    session_id: str | None = None
    incremental_cost_usd: float | None = None
    api_equivalent_cost_usd: float | None = None
    cost: float | None = 0.0

    def append(self, log: Path) -> None:
        log.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(self.model_dump_json() + "\n")
