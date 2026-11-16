"""A model id read as family + version (`ModelName`), and the id a key's vendor runs a catalog row under (`listed_as`).

Standard library only."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

#: Parts of a model id: `claude-opus-4.5`, `gpt-5.1-codex`, `claude-3-5-sonnet-20241022`.
_PART = re.compile(r"[-._]")
#: A part from which the rest of an id says how it is served, never which model: a release date (4+ digits:
#: `20241022`, `2024-08-06`), a channel (`-preview-06-05`), a mode (`-thinking`), a Bedrock revision (`-v1:0`).
_QUALIFIERS = frozenset({"latest", "preview", "exp", "experimental", "thinking", "beta", "alpha", "rc"})
#: `-latest` names the newest version: later than any version written.
_LATEST = (10**9,)


def _qualifier(part: str) -> bool:
    return (part.isdigit() and len(part) >= 4) or re.fullmatch(r"v\d+", part) is not None or part.rstrip("0123456789") in _QUALIFIERS


@dataclass(frozen=True)
class ModelName:
    """A model id read as family + version: the last run of number parts is the version, the words around it
    the family (`gemini-2.5-pro` → `gemini pro` 2.5; `claude-3-5-sonnet` and `claude-sonnet-4` → `claude sonnet`).
    Provider prefixes (`anthropic/`, `us.anthropic.`), `@tag` / `:tag` suffixes and every qualifier part on
    (`_QUALIFIERS`) are neither. No version written: the bare alias of the family's newest (`fable`, `opus`)."""

    words: tuple[str, ...]
    version: tuple[int, ...]

    @classmethod
    @lru_cache(maxsize=4096)
    def of(cls, name: str) -> ModelName:
        bare = re.split(r"[@:]", re.sub(r"\[[^\]]*\]", "", name).rsplit("/", 1)[-1].lower(), maxsplit=1)[0]  # `[1m]`: a context size
        parts = [part for part in _PART.split(bare) if part]
        cut = next((index for index, part in enumerate(parts) if _qualifier(part)), len(parts))
        latest, parts = "latest" in parts[cut:cut + 1], parts[:cut]
        end = next((index + 1 for index in range(len(parts) - 1, -1, -1) if parts[index].isdigit()), 0)
        start = end
        while start > 0 and parts[start - 1].isdigit():
            start -= 1
        version = tuple(int(part) for part in parts[start:end])
        return cls(words=tuple(parts[:start] + parts[end:]), version=version or (_LATEST if latest else ()))


def listed_as(name: str, listed: Iterable[str]) -> str | None:
    """The id a vendor that lists `listed` runs `name` under: `name` itself, else its family's `-latest` alias (« mistral-medium-3-1-2508 »
    → « mistral-medium-latest », the vendor's newest of that family); None when it lists neither. A catalog row a key's vendor
    renamed still runs, ranked on the score the catalog holds for it (2026-11-14: Mistral lists `mistral-medium-latest`, the
    catalog scores its dated row)."""
    ids = frozenset(listed)
    if name in ids:
        return name
    # Only the exact `<family>-latest` id: a listed id carrying more (`…-latest[text]`, `a/…-latest`) is never launched (threat r3).
    words = ModelName.of(name).words
    alias = "-".join(words) + "-latest"
    return alias if words and alias in ids else None  # a name with no words has no family
