"""Stubbing `interact.server.capture`'s async entry points with fixed data."""

from __future__ import annotations


def async_capture(data):
    """An async function that always returns ``data`` — what a monkeypatched capture call
    (``_capture_target_png`` and friends) is stood in for, without spinning up a real backend."""

    async def _capture(*_a, **_k):
        return data

    return _capture
