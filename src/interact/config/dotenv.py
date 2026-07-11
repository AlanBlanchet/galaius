"""Shared `.env` loader for CLI entry points and tests.

`.env` is for tests/CLI only — production env vars come from the host.
"""

from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger(__name__)


def load_dotenv_for_cli() -> Path | None:
    """Walk up to 3 levels from cwd for a `.env` and load it.

    Uses ``override=False`` so existing env vars win. Returns the loaded
    path or ``None`` if no `.env` was found or ``python-dotenv`` is missing.
    """
    try:
        from dotenv import load_dotenv  # noqa: PLC0415 — optional dep, gracefully skipped when missing
    except ImportError:
        return None
    cwd = Path.cwd().resolve()
    for parent in [cwd, *list(cwd.parents)[:3]]:
        candidate = parent / ".env"
        if candidate.exists():
            load_dotenv(candidate, override=False)
            _log.info("loaded .env from %s", candidate)
            return candidate
    return None
