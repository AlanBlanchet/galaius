"""Dependency-light durable accounting records shared by media transports and clients."""

import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from galaius_core import TokenCounts
from pydantic import BaseModel, Field, model_validator

from galaius.older_tokens import older_tokens


class UsageEntry(BaseModel):
    """Canonical on-disk media accounting row, owned by its only writer."""

    timestamp: datetime
    model: str
    usage: TokenCounts = Field(default_factory=TokenCounts)
    backend: Literal["session", "api", "none"]
    provider: str = Field(min_length=1)
    billing: Literal["session_usage", "metered_api", "none"]
    outcome: Literal["succeeded", "failed", "cancelled"]
    request_id: str | None = None
    session_id: str | None = None
    incremental_cost_usd: float | None = None
    api_equivalent_cost_usd: float | None = None
    cost: float | None = 0.0

    @model_validator(mode="before")
    @classmethod
    def older_row(cls, data: object) -> object:
        """A row an older runtime appended: its whole-prompt token fields, read as its `usage` (`older_tokens`)."""
        return older_tokens(data)

    def append(self, log: Path) -> None:
        log.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(self.model_dump_json() + "\n")
