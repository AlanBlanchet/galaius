"""Token counts as each source spells them — a vendor's usage report, what an older runtime wrote on this PC — read once,
at the edge receiving them, into `galaius_core.TokenCounts`: the shape every reader, the panel and the server see. A
report read as « nothing counted » would undercount; a cached token kept inside a whole-prompt figure would be counted
twice."""

from collections.abc import Mapping
from typing import Any, ClassVar

from galaius_core import TokenCounts
from pydantic import BaseModel, ConfigDict, model_validator

#: A figure's spellings in one report, first found wins; a dotted name reaches into a nested object
#: (`prompt_tokens_details.cached_tokens`).
Spellings = tuple[str, ...]


class UsageDialect(BaseModel):
    """How one source spells a token count: each figure's names, and whether its prompt figure is the WHOLE prompt (cache
    reads and writes inside it: OpenAI's, Codex's, an older galaius runtime's) or only its uncached part (Anthropic's)."""

    model_config = ConfigDict(frozen=True)

    input: Spellings
    output: Spellings
    cache_read: Spellings = ()
    cache_write: Spellings = ()
    whole_prompt: bool

    @staticmethod
    def _figure(report: object, spellings: Spellings) -> int | None:
        """The first of `spellings` `report` (a mapping or an object) holds as a count; a bool is no count."""
        for spelling in spellings:
            value: Any = report
            for part in spelling.split("."):
                value = value.get(part) if isinstance(value, Mapping) else getattr(value, part, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    def read(self, report: object) -> TokenCounts:
        """`report`'s counts, each figure it does not name left unreported (`TokenCounts.reported` False when it names
        none)."""
        prompt, output, cache_read, cache_write = (self._figure(report, spellings) for spellings in (self.input, self.output, self.cache_read, self.cache_write))
        if self.whole_prompt:
            return TokenCounts.of_prompt(prompt, output, cache_read, cache_write)
        return TokenCounts(input=prompt, output=output, cache_read=cache_read, cache_write=cache_write)

    @property
    def names(self) -> frozenset[str]:
        """Every top-level key it reads."""
        return frozenset(spelling.split(".")[0] for spellings in (self.input, self.output, self.cache_read, self.cache_write) for spelling in spellings)


#: Codex `exec`'s spelling (`turn.completed.usage`), which galaius's runtime wrote on this PC until it held `TokenCounts`.
WHOLE_PROMPT_SNAKE = UsageDialect(input=("input_tokens",), output=("output_tokens",), cache_read=("cached_input_tokens",),
                                  cache_write=("cache_write_input_tokens",), whole_prompt=True)
#: A litellm completion response (the object or its dict), its `usage` in OpenAI's shape: the whole prompt in
#: `prompt_tokens`, its cache reads inside it.
LITELLM = UsageDialect(input=("usage.prompt_tokens", "usage.input_tokens"), output=("usage.completion_tokens", "usage.output_tokens"),
                       cache_read=("usage.prompt_tokens_details.cached_tokens",), whole_prompt=True)


class OlderTokens(BaseModel):
    """A model an older runtime wrote on this PC (run record, event line, media usage-log row): its whole-prompt token
    fields read as its `usage` at validation, so it is written back in the one spelling. `_older` is the hook a model
    with more of the older shape to move runs first."""

    OLDER: ClassVar[UsageDialect] = WHOLE_PROMPT_SNAKE

    @classmethod
    def _older(cls, data: dict[str, Any]) -> dict[str, Any]:
        return data

    @model_validator(mode="before")
    @classmethod
    def _older_tokens(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        data = cls._older(data)
        if not data.keys() & cls.OLDER.names:
            return data
        value = {key: item for key, item in data.items() if key not in cls.OLDER.names}
        if not isinstance(data.get("usage"), dict | TokenCounts) and (counts := cls.OLDER.read(data)).reported:
            value["usage"] = counts.model_dump(mode="json", exclude_none=True)
        return value

    @classmethod
    def written(cls, data: dict[str, Any]) -> dict[str, Any]:
        """`data` as it is written back: in the one spelling."""
        return cls._older_tokens(data)
