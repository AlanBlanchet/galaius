"""Cooldown for models that just failed: a tripped model is skipped until its TTL passes."""

import time


class CircuitBreaker:
    """Track failed models with TTL-based cooldown."""

    def __init__(self, ttl: float = 300.0):
        self._trips: dict[str, float] = {}
        self._ttl = ttl

    def tripped(self, model_id: str) -> bool:
        ts = self._trips.get(model_id)
        if ts is None:
            return False
        if time.monotonic() - ts >= self._ttl:
            del self._trips[model_id]
            return False
        return True

    def trip(self, model_id: str) -> None:
        self._trips[model_id] = time.monotonic()

    def clear(self) -> None:
        self._trips.clear()
