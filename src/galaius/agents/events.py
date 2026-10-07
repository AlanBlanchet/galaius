"""One normalised event shape across every agent provider.

Each vendor CLI streams its own JSON dialect; supervisor and dashboard must not learn all of
them. Providers translate into :class:`AgentEvent`, so a Claude run and a Codex run render
identically, and a new provider costs one adapter, not a new UI.

Unrecognised line becomes ``kind="other"`` rather than dropped — a vendor adding an event type
must never make a run look idle.
"""

from collections.abc import Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from galaius.models import TokenMix

EventKind = Literal[
    "started",     # session is up (id, cwd, tools known)
    "text",        # the agent said something
    "thinking",    # model's own reasoning, kept distinct so a reader can fold it away
    "tool",        # agent used a tool — the live "what is it doing" line
    "tool_result", # what that tool gave back
    "message",     # one agent addressing another — the edge in a sequence/graph view
    "rate_limit",  # the account's pooled limit spoke; a run can die here
    "done",        # terminal: carries the run's cost and token totals
    "error",       # terminal: it failed
    "prompt",      # what was asked OF the agent — the other half of the conversation
    "spawn",       # this agent started a subagent — the team growing a branch
    "interaction", # a provider needs typed human input
    "interaction_resolved", # typed human input was returned to the provider
    "cancelled",   # a turn was interrupted and acknowledged
    "other",       # recognised as valid, not specially handled — never silently dropped
]
InteractionKind = Literal[
    "command_approval", "file_change_approval", "user_input", "permission_approval"
]
InteractionFieldKind = Literal["choice", "text", "boolean"]
EventStatus = Literal[
    "starting",
    "running",
    "waiting",
    "completed",
    "failed",
    "cancelled",
    "provider_failed",
    "interaction_required",
    "unknown",
]
_INTERACTION_KEY = r"^[A-Za-z0-9._:@+-]+$"
_UNSAFE_INTERACTION_KEYS = frozenset({".", "..", "__proto__", "prototype", "constructor"})


class InteractionField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=160, pattern=_INTERACTION_KEY)
    kind: InteractionFieldKind
    label: str = Field(min_length=1, max_length=500)
    required: bool = True
    options: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.key in _UNSAFE_INTERACTION_KEYS:
            raise ValueError("interaction field key is unsafe")
        if self.kind == "choice":
            if (
                not self.options
                or len(self.options) != len(set(self.options))
                or any(not option.strip() for option in self.options)
            ):
                raise ValueError("choice fields require unique nonblank options")
        elif self.options:
            raise ValueError("only choice fields may declare options")
        return self


class ConversationInteraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    kind: InteractionKind
    title: str = Field(min_length=1, max_length=500)
    fields: list[InteractionField] = Field(min_length=1)
    disclosure: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_fields(self) -> Self:
        keys = [field.key for field in self.fields]
        if len(keys) != len(set(keys)):
            raise ValueError("interaction field keys must be unique")
        return self

    def validate_submission(
        self, values: Mapping[str, object]
    ) -> dict[str, str | bool]:
        fields = {field.key: field for field in self.fields}
        if set(values).difference(fields):
            raise ValueError("interaction submission has unknown fields")
        if any(field.required and field.key not in values for field in self.fields):
            raise ValueError("interaction submission is missing required fields")
        validated: dict[str, str | bool] = {}
        for key, value in values.items():
            field = fields[key]
            if field.kind == "boolean":
                if type(value) is not bool:
                    raise ValueError("interaction boolean field has the wrong value kind")
                validated[key] = value
                continue
            if not isinstance(value, str):
                raise ValueError("interaction string field has the wrong value kind")
            if field.kind == "choice" and value not in field.options:
                raise ValueError("interaction choice is outside its advertised options")
            if field.kind == "text" and not value.strip():
                if field.required:
                    raise ValueError("required interaction text must not be blank")
                continue
            validated[key] = value
        return validated


class TokenUsage(BaseModel):
    """Token counts in the ONE meaning every provider is normalised to.

    ``input_tokens`` is every prompt token the model processed, cache reads AND cache writes
    included; ``cached_input_tokens`` and ``cache_write_input_tokens`` are subsets of it.
    ``output_tokens`` includes reasoning / thinking.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(**{f: getattr(self, f) + getattr(other, f) for f in TOKEN_FIELDS})

    def __sub__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(**{f: getattr(self, f) - getattr(other, f) for f in TOKEN_FIELDS})

    def covers(self, other: "TokenUsage") -> bool:
        """Every counter at least ``other``'s — true of a cumulative report and its past."""
        return all(getattr(self, f) >= getattr(other, f) for f in TOKEN_FIELDS)

    def since(self, previous: "TokenUsage") -> "TokenUsage":
        """Growth of a cumulative counter; a counter that went BACKWARDS restarted from zero."""
        return self - previous if self.covers(previous) else self

    def mix(self) -> TokenMix | None:
        """These tokens as a `TokenMix` — how a workload splits between fresh input, cache reads,
        cache writes and output; None before the first counted token."""
        if self.input_tokens + self.output_tokens <= 0:
            return None
        fresh = self.input_tokens - self.cached_input_tokens - self.cache_write_input_tokens
        return TokenMix(input=max(fresh, 0), cache_read=self.cached_input_tokens,
                        cache_write=self.cache_write_input_tokens, output=self.output_tokens)


#: The token fields shared by :class:`TokenUsage`, :class:`AgentEvent` and the run record. Every
#: fold, heal and persisted-field list iterates THIS tuple, so a field cannot be summed on one
#: path and dropped on another.
TOKEN_FIELDS: tuple[str, ...] = tuple(TokenUsage.model_fields)


class _UsageCheckpoint(BaseModel):
    usage: dict[str, TokenUsage]
    cost_usd: float | None


class UsageLedger(BaseModel):
    """One vendor stream's usage reports folded into per-event figures whose SUM is the truth.

    Vendors report usage twice over, and neither report alone can be summed per line:

    - LIVE figures (``observe``): a per-message usage the vendor may repeat — Claude re-sends
      one message's usage on every content-block line. Counted once per message key, so a
      running run shows its consumption before it ends.
    - CHECKPOINTS (``settle``): the vendor's authoritative CUMULATIVE counters per model
      (Claude ``result.modelUsage`` + ``total_cost_usd``, Codex ``turn.completed.usage``),
      including internal calls no live line shows. On a resumed session the vendor may restore
      its counters from an earlier invocation, or restart them from zero.

    A checkpoint event carries what the counters grew (``cumulative - restored baseline``) beyond
    the live figures counted since the last checkpoint, never below zero per field: streamed
    messages and vendor counters are each a LOWER bound of real use — a killed process's
    messages were really processed even when the next process's restored counters lost them,
    and counted twice when those counters kept them. The baseline is the newest earlier checkpoint the new report still covers once
    the invocation's own exactly-known part (``local``, keyed by ``primary``) is removed — a
    restored counter can only have grown from it — or that it repeats exactly (one process
    printing its counters once per queued turn); else zero. A single line parsed on its own is
    a stream of one line.
    """

    #: Checkpoint key the invocation-local figure belongs to — the session's main model.
    primary: str | None = None
    seen: set[str] = Field(default_factory=set)
    live: TokenUsage = TokenUsage()
    checkpoints: list[_UsageCheckpoint] = Field(default_factory=list)

    def observe(self, key: str | None, usage: TokenUsage) -> TokenUsage | None:
        """A live figure, or None when this message's usage was already counted."""
        if key is not None:
            if key in self.seen:
                return None
            self.seen.add(key)
        self.live += usage
        return usage

    def settle(
        self, cumulative: dict[str, TokenUsage], cost_usd: float | None,
        local: TokenUsage | None = None,
    ) -> tuple[TokenUsage, float | None]:
        """This checkpoint's share of the stream's truth: tokens and API-equivalent cost."""
        floor = {
            key: usage - local if local is not None and key == self.primary else usage
            for key, usage in cumulative.items()
        }

        def restored_from(past: _UsageCheckpoint) -> bool:
            if cost_usd is not None and past.cost_usd is not None \
                    and cost_usd < past.cost_usd - 1e-9:
                return False
            if past.usage == cumulative:
                return True  # one process reporting again (queued turns), nothing grew
            return all(key in floor and floor[key].covers(usage)
                       for key, usage in past.usage.items())

        baseline = next(
            (past for past in reversed(self.checkpoints) if restored_from(past)),
            _UsageCheckpoint(usage={}, cost_usd=None),
        )
        grown = sum(
            (usage - baseline.usage.get(key, TokenUsage()) for key, usage in cumulative.items()),
            TokenUsage(),
        )
        cost = None if cost_usd is None else cost_usd - (baseline.cost_usd or 0.0)
        self.checkpoints.append(_UsageCheckpoint(usage=cumulative, cost_usd=cost_usd))
        delta = TokenUsage(**{
            field: max(0, getattr(grown, field) - getattr(self.live, field))
            for field in TOKEN_FIELDS
        })
        self.live = TokenUsage()
        return delta, cost


class AgentEvent(BaseModel):
    """A single thing that happened inside an agent run, provider-agnostic."""

    kind: EventKind
    #: Stable normalized identity. Provider replay is ignored by this key, never by text equality.
    event_id: str = ""
    #: Monotonic within one run after normalization; provider order is retained separately below.
    sequence: int | None = None
    #: Stable provider-side cursor/item identity when one exists.
    provider_cursor: str = ""
    parent_event_id: str | None = None
    turn_id: str | None = None
    #: The discrete run this event describes. For collaboration events this is the child run.
    agent_run_id: str | None = None
    text: str = ""
    session_id: str | None = None
    tool: str | None = None
    #: Compact rendering of the tool's arguments. A view showing "used Bash" without the
    #: command is a status line, not a transcript — this is what makes it readable.
    tool_input: str = ""
    #: Vendor's tool_use id, carried on BOTH the call and its result. Summarised event is
    #: clipped by design; this is the stable key a viewer uses to pull the FULL input/output
    #: for one call out of the raw stream — prefix-matching breaks on two identical commands.
    tool_id: str = ""
    #: For a "message": which run sent it, which received it. Both sides record the same
    #: exchange, so a sequence view can draw the arrow from either transcript.
    from_run: str | None = None
    to_run: str | None = None
    #: Where this sits in the vendor's raw stream — for a MESSAGE, how many raw lines existed
    #: when sent. Vendor writes no timestamps, so this lets a message show where it actually
    #: happened, not dumped after every reply it caused.
    raw_index: int | None = None
    # API-equivalent cost estimates usage VALUE, not billed spend. Charge path and account
    # impact remain separate typed facts; subscription usage may be included, limited, credited, or charged.
    cost_usd: float | None = None
    #: Token figures in :class:`TokenUsage`'s meaning. Summed over a run's events they give the
    #: run's totals: a checkpoint event carries only what live events had not yet counted.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    raw_type: str = ""
    interaction: ConversationInteraction | None = None
    status: EventStatus | None = None
    #: True when a terminal event itself carries the provider's final response (Claude
    #: structured output), not only a stop reason. Media execution must prefer this over an
    #: earlier free-form assistant block.
    final_text: bool = False
    #: When galaius FIRST OBSERVED this line, not when the agent produced it — vendor writes
    #: no timestamp, we watch the stream, so this is the one honestly-available clock. Lets a
    #: view tell a working agent from a stopped one, order a conversation, and fire a message
    #: animation once at the right moment instead of guessing.
    at: float | None = None

    def summary(self, viewer: str | None = None) -> str:
        """One line for a supervisor row — what this agent is doing right now."""
        if self.kind == "tool":
            return f"using {self.tool}" if self.tool else "using a tool"
        if self.kind == "rate_limit":
            return self.text or "rate limited"
        if self.kind == "message":
            return self._message_summary(viewer)
        if self.kind == "interaction":
            return "waiting for input"
        if self.kind == "interaction_resolved":
            return "input answered"
        if self.kind == "cancelled":
            return "cancelled"
        if self.kind == "done":
            return "done"
        if self.kind == "error":
            return self.text or "failed"
        return " ".join(self.text.split())[:120]

    def _message_summary(self, viewer: str | None) -> str:
        """A message, from the reading agent's point of view.

        Used to render "→ <to_run>" always, so a message the agent RECEIVED showed an arrow
        pointing at its own id — indistinguishable from the agent-to-agent traffic the panel
        exists to show, while showing none of it. Direction is relative to whoever is reading;
        the other end is named, not hashed.
        """
        body = (self.text or "")[:80]
        if not self.to_run:
            return body
        if viewer and self.to_run == viewer:
            return f"← {_who(self.from_run)}: {body}"
        if viewer and self.from_run == viewer:
            return f"→ {_who(self.to_run)}: {body}"
        return f"{_who(self.from_run)} → {_who(self.to_run)}: {body}"


def _who(run_id: str | None) -> str:
    """A run's name, for a reader. "operator" is a person, not a run; an id we can't resolve
    falls back to its short form, not a bare empty string.

    Reads the stored record DIRECTLY, not through ``list_runs``: that derives each run's
    `last` line, which summarises an event, which asks who it was from — a loop that recursed
    until the stack ran out.
    """
    if not run_id:
        return "?"
    if run_id == "operator":
        return "operator"
    # Deliberately local: registry imports AgentEvent from this module, so a module-level
    # import would create the events<->registry cycle before either side declared its models.
    from galaius.agents import registry as reg

    run = reg._read_record(run_id)
    return run.name if run is not None else run_id[:8]
