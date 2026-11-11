"""Which models a `models` off-switch (`~/.galaius/agents.json`) covers: one rule for the launcher, the
PreToolUse guard and the watchdog, so a model is off for all three or for none.

Owner 2026-10-05: « Woow, fable was used. Please make sure we don't activate it. It costs too much... » ;
« Fable should not even come out in the formula ». A model switched off switches off its later versions
too: `claude-fable-5-1: false` covers `claude-fable-5-2`, `anthropic/claude-fable-6`, `claude-fable-latest`
and the bare alias `fable` (the newest one), never `claude-fable-5`. An entry naming the model itself wins
over its family: `claude-fable-5-2: true` turns that one back on, however it is spelled.

Standard library only: the guard and the watchdog run outside galaius's interpreter and load this file
by path."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache

#: Parts of a model id: `claude-opus-4.5`, `gpt-5.1-codex`, `claude-3-5-sonnet-20241022`.
_PART = re.compile(r"[-._]")
#: A part from which the rest of an id says how it is served, never which model: a release date (4+ digits:
#: `20241022`, `2024-08-06`), a channel (`-preview-06-05`), a mode (`-thinking`), a Bedrock revision (`-v1:0`).
_QUALIFIERS = frozenset({"latest", "preview", "exp", "experimental", "thinking", "beta", "alpha", "rc"})
#: `-latest` names the newest version: later than any version written.
_LATEST = (10**9,)
#: A model-id-shaped word in a command line, and the values a command gives a model under a name that says so:
#: `--model X`, `ANTHROPIC_MODEL=X`, `"model": "X"`, jq's `.model="X"`, codex's `-m X`.
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]*")
_VALUE = re.compile(r"""(?:--model(?:=|\s+)|\b[A-Z][A-Z0-9_]*MODEL[A-Z0-9_]*=|["']model["']\s*:\s*|\.model\s*=\s*|\bcodex\b[^\n;&|]*?\s-m\s+)["']?"""
                    r"""([A-Za-z0-9][\w.:/@-]*)""")


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

    def kin(self, other: ModelName) -> bool:
        """One family: the shorter's words end the longer's (`fable` and `us anthropic claude fable` are `claude fable`)."""
        short, long = sorted((self.words, other.words), key=len)
        return bool(short) and long[len(long) - len(short):] == short

    def covers(self, other: ModelName) -> bool:
        """`other` is this model, a later version of it, or its family's bare alias (the newest)."""
        return self.kin(other) and (not other.version or other.version >= self.version)

    def same(self, other: ModelName) -> bool:
        return self.kin(other) and other.version == self.version


@dataclass(frozen=True)
class ModelSwitches:
    """The `models` switches of one policy file: `{"claude/claude-fable-5-1": false, …}`."""

    models: Mapping[str, bool] = field(default_factory=dict)

    @classmethod
    def read(cls, models: Iterable[tuple[str, object]]) -> ModelSwitches:
        """From a policy file's raw `models` pairs: only a literal true / false is a switch."""
        return cls({str(name): on for name, on in models if isinstance(on, bool)})

    def off(self) -> tuple[str, ...]:
        """The entries switched off, as written."""
        return tuple(entry for entry, on in self.models.items() if on is False)

    def off_by(self, *names: str) -> str | None:
        """The switched-off entry that covers a model known under `names` (`claude/<id>`, `anthropic/<id>`, the
        bare id), or None when it may run. An entry naming the model itself, in any spelling, decides first."""
        read = [ModelName.of(name) for name in names if name]
        exact = [(entry, on) for entry, on in self.models.items() if any(ModelName.of(entry).same(name) for name in read)]
        if exact:
            return next((entry for entry, on in exact if on is False), None)
        return next((entry for entry in self.off() if any(ModelName.of(entry).covers(name) for name in read)), None)

    def named_in(self, text: str) -> str | None:
        """The first switched-off model a command line names, or None. Anywhere: a versioned id in any wrapping
        (`us.anthropic.<id>-v1:0`, `<id>.log`, `<alias>-5-2`); as a model value (`--model`, `…MODEL=`, `"model":`):
        a bare alias too (`--model fable[1m]`). A guard reading, linear in the text (it runs on every command)."""
        words = {word for entry in self.off() for word in ModelName.of(entry).words}
        if not words:
            return None
        for token in _WORD.findall(text):
            parts = [part for part in re.split(r"[-._@:/]", token.lower()) if part]
            for start, part in enumerate(parts):
                if part not in words or (start and parts[start - 1] in words):
                    continue
                end = start
                while end < len(parts) and (parts[end] in words or parts[end].isdigit() or _qualifier(parts[end])):
                    end += 1
                named = "-".join(parts[start:end])
                if ModelName.of(named).version and self.off_by(named):
                    return named
        return next((value for value in _VALUE.findall(text) if self.off_by(value)), None)
