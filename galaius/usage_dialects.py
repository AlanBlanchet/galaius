"""Token counts as each source spells them — a vendor's usage report, what an older runtime wrote on this PC — read once,
at the edge receiving them, into `galaius_core.TokenCounts`: the shape every reader, the panel and the server see. A
report read as « nothing counted » would undercount; a cached token kept inside a whole-prompt figure would be counted
twice."""

from collections.abc import Mapping
from functools import cached_property
from typing import Annotated, Any, ClassVar

from galaius_core import TokenCounts
from pydantic import BaseModel, ConfigDict, Field, model_validator

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

    @cached_property
    def names(self) -> frozenset[str]:
        """Every top-level key it reads: once per dialect, every event line validated asks it once per key it holds
        (`OlderTokens`)."""
        return frozenset(spelling.split(".")[0] for spellings in (self.input, self.output, self.cache_read, self.cache_write) for spelling in spellings)


#: What galaius's runtime wrote on this PC before it held `TokenCounts`: the whole prompt as `input_tokens`, its cache
#: reads and writes inside it.
OLDER_RUNTIME = UsageDialect(input=("input_tokens",), output=("output_tokens",), cache_read=("cached_input_tokens",),
                             cache_write=("cache_write_input_tokens",), whole_prompt=True)
#: A litellm completion response (the object or its dict), its `usage` in OpenAI's shape: the whole prompt in
#: `prompt_tokens` (`input_tokens` in the Responses format), its cache reads and — for Anthropic models, litellm 1.100
#: `anthropic/chat/transformation.py` — its cache writes inside it.
LITELLM = UsageDialect(input=("usage.prompt_tokens", "usage.input_tokens"), output=("usage.completion_tokens", "usage.output_tokens"),
                       cache_read=("usage.prompt_tokens_details.cached_tokens", "usage.input_tokens_details.cached_tokens"),
                       cache_write=("usage.cache_creation_input_tokens",), whole_prompt=True)

#: A model's token count: empty when nothing reported it, and then left out of what it writes.
CountedTokens = Annotated[TokenCounts, Field(default_factory=TokenCounts, exclude_if=lambda value: not value.reported)]


class OlderTokens(BaseModel):
    """A model an older runtime wrote on this PC (run record, event line, media usage-log row): its whole-prompt token
    fields read as its `usage` at validation, so it is written back in the one spelling. `_older` is the hook a model
    with more of the older shape to move runs first. This edge goes, with the VS Code client's `olderWritten`, once no
    PC holds a run folder or usage log an older runtime wrote (event lines and the usage log are only appended to:
    never rewritten in place while agents write them)."""

    OLDER: ClassVar[UsageDialect] = OLDER_RUNTIME

    @classmethod
    def _older(cls, data: dict[str, Any]) -> dict[str, Any]:
        return data

    @model_validator(mode="before")
    @classmethod
    def _older_tokens(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        data, names = cls._older(data), cls.OLDER.names
        if not data.keys() & names:
            return data
        value = {key: item for key, item in data.items() if key not in names}
        if not isinstance(data.get("usage"), dict | TokenCounts) and (counts := cls.OLDER.read(data)).reported:
            value["usage"] = counts.model_dump(mode="json", exclude_none=True)
        return value

    @classmethod
    def written(cls, data: dict[str, Any]) -> dict[str, Any]:
        """`data` as it is written back: in the one spelling."""
        return cls._older_tokens(data)
