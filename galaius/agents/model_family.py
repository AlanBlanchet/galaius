"""Which models a `models` off-switch (`~/.galaius/agents.json`) covers: one rule for the launcher, the
PreToolUse guard and the watchdog, so a model is off for all three or for none.

Owner 2026-10-05: « Woow, fable was used. Please make sure we don't activate it. It costs too much... » ;
« Fable should not even come out in the formula ». A model switched off switches off its later versions
too: `claude-fable-5-1: false` covers `claude-fable-5-2`, `anthropic/claude-fable-6`, never
`claude-fable-5`. An entry naming the model itself wins over its family: `claude-fable-5-2: true` turns
that one back on.

Standard library only: the guard and the watchdog run outside galaius's interpreter and load this file
by path."""

from __future__ import annotations

import re
from functools import lru_cache
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

#: Parts of a model id: `claude-opus-4.5`, `gpt-5.1-codex`, `claude-3-5-sonnet-20241022`.
_PART = re.compile(r"[-._]")
#: A part this long and all digits is a release date, never a version.
_DATE_DIGITS = 6
#: A model-id-shaped word in a command line.
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]*")


@dataclass(frozen=True, order=True)
class ModelName:
    """A model id read as family + version: the last run of number parts is the version, the words
    around it the family (`gemini-2.5-pro` → family `gemini…pro`, version 2.5). Provider prefixes
    (`anthropic/`, `claude/`), `@tag` / `:tag` suffixes and release dates are not part of either."""

    family: tuple[str, str]
    version: tuple[int, ...]

    @classmethod
    @lru_cache(maxsize=4096)
    def of(cls, name: str) -> ModelName:
        bare = re.split(r"[@:]", name.rsplit("/", 1)[-1].lower(), maxsplit=1)[0]
        parts = [part for part in _PART.split(bare) if part and not (part.isdigit() and len(part) >= _DATE_DIGITS)]
        end = next((index + 1 for index in range(len(parts) - 1, -1, -1) if parts[index].isdigit()), 0)
        start = end
        while start > 0 and parts[start - 1].isdigit():
            start -= 1
        return cls(family=("-".join(parts[:start]), "-".join(parts[end:])),
                   version=tuple(int(part) for part in parts[start:end]))

    def covers(self, other: ModelName) -> bool:
        """`other` is this model or a later version of it."""
        return other.family == self.family and other.version >= self.version


@dataclass(frozen=True)
class ModelSwitches:
    """The `models` switches of one policy file: `{"claude/claude-fable-5-1": false, …}`."""

    models: Mapping[str, bool] = field(default_factory=dict)

    def off_by(self, *names: str) -> str | None:
        """The switched-off entry that covers a model known under `names` (`claude/<id>`, `anthropic/<id>`,
        the bare id), or None when it may run. An entry for one of its own names decides first."""
        bare = {name.rsplit("/", 1)[-1].lower() for name in names if name}
        exact = [(entry, on) for entry, on in self.models.items() if entry.rsplit("/", 1)[-1].lower() in bare]
        if exact:
            return next((entry for entry, on in exact if on is False), None)
        read = [ModelName.of(name) for name in names if name]
        return next((entry for entry, on in self.models.items() if on is False
                     and any(ModelName.of(entry).covers(name) for name in read)), None)

    def named_in(self, text: str) -> str | None:
        """The first switched-off model a command line names, read in every run of a word's parts
        (`us.anthropic.<id>-v1:0` and `<id>.log` both name `<id>`); None when it names none. A guard
        reading: it may refuse a word the launcher would run (`gpt-5.2-codex` holds `gpt-5.2`), never
        let one through the launcher refuses."""
        # Only a word holding every word of a switched-off family is read part by part (a guard runs on every command).
        needles = {tuple(filter(None, ModelName.of(entry).family)) for entry in self.off()}
        if not needles:
            return None
        for word in _WORD.findall(text):
            parts = re.split(r"[-._@:]", word.rsplit("/", 1)[-1])
            joined = "-".join(parts).lower()
            if not any(all(needle in joined for needle in family) for family in needles):
                continue
            for start in range(len(parts)):
                for end in range(len(parts), start, -1):
                    if not parts[start].isdigit() and self.off_by(named := "-".join(parts[start:end])):
                        return named
        return None

    def off(self) -> tuple[str, ...]:
        """The entries switched off, as written."""
        return tuple(entry for entry, on in self.models.items() if on is False)

    @classmethod
    def read(cls, models: Iterable[tuple[str, object]]) -> ModelSwitches:
        """From a policy file's raw `models` pairs: only a literal true / false is a switch."""
        return cls({str(name): on for name, on in models if isinstance(on, bool)})
