"""String parsing helpers shared by VLM response handlers.

All functions live on :class:`Parse` as ``@staticmethod`` — they are pure
string→data utilities with no owning instance.
"""

from __future__ import annotations

import json
import re
from typing import Any


class Parse:
    """Namespace of pure VLM-response parsing helpers (all staticmethod)."""

    _FENCE_OPEN_RE = re.compile(r"^```[a-zA-Z0-9_+-]*\n?")
    _FENCE_CLOSE_RE = re.compile(r"\n?```\s*$")

    @staticmethod
    def strip_markdown_fences(s: str) -> str:
        """Remove leading/trailing triple-backtick fences (and any language tag)."""
        if not s:
            return s
        t = s.strip()
        t = Parse._FENCE_OPEN_RE.sub("", t)
        t = Parse._FENCE_CLOSE_RE.sub("", t)
        return t.strip()

    @staticmethod
    def try_json(s: str) -> Any | None:
        """Parse ``s`` as JSON after stripping fences. Returns None on failure."""
        if not s:
            return None
        try:
            return json.loads(Parse.strip_markdown_fences(s))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def extract_json_array(text: str) -> list | None:
        """Best-effort extraction of a JSON array (or single dict) from text."""
        if not text:
            return None
        cleaned = Parse.strip_markdown_fences(text)
        parsed = Parse.try_json(cleaned)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
        start = cleaned.find("[")
        if start == -1:
            return None
        end = cleaned.rfind("]")
        fragment = cleaned[start : end + 1] if end > start else cleaned[start:]
        for attempt in (fragment, fragment.rstrip(",\n ") + "]"):
            try:
                result = json.loads(attempt)
                if isinstance(result, list):
                    return result
            except ValueError:
                continue
        return None

    @staticmethod
    def extract_point(text: str) -> tuple[float, float] | None:
        """Extract an ``(x, y)`` point from a JSON-shaped response."""
        obj = Parse.try_json(text)
        if isinstance(obj, dict) and "x" in obj and "y" in obj:
            try:
                return float(obj["x"]), float(obj["y"])
            except (ValueError, TypeError):
                return None
        if isinstance(obj, list) and len(obj) >= 2:
            try:
                return float(obj[0]), float(obj[1])
            except (ValueError, TypeError):
                return None
        return None
