"""What a vendor said about its own quota, remembered past the run that heard it.

A quota refusal is a fact about the ACCOUNT for a period, not about one launch: the model that
answered "you've reached your Fable limit" answers the same thing to the next agent a second
later. Without a memory, every launch pays the same tax — spawn, wait, die at $0.00, fall
through — and a refusal arriving after the probe's window kills the run outright (#181).

So the refusal is written down, and a model under cooldown is passed over BEFORE anything is
spawned. The cooldown expires on its own, so nothing has to be cleared by hand when the vendor's
window rolls over; and when EVERY candidate is under cooldown the walk ignores the memory rather
than refusing to launch, because a stale note must never be the reason an agent cannot run.

How LONG is the vendor's own answer, never ours. A refusal names the period it refuses for and
usually names the instant that period reopens; a fixed hour ignored both, so a seven-day window
was re-probed every hour until the builds under it died. Across the 60 real refusals captured in
`~/.interact/out/agents/*.raw.jsonl`, the gap between the refusal and its own `resetsAt` ran from
0.05 h to 151 h — no single constant is within two orders of magnitude of that spread.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict

#: A vendor CLI's own refusal for QUOTA or RATE LIMIT — provider-neutral by construction, keyed
#: on (provider, model) by the caller, so one shared pattern never needs to tell providers apart.
#: Claude Code: "You've reached your <model> limit. Switch to another model." Codex (source:
#: `codex-rs/protocol/src/error.rs`, `CodexErrorDetails::UsageLimitReached` display strings,
#: verified 2026-09-21): "You've hit your usage limit[. ...]", "You hit your spend cap...", "Your
#: workspace is out of credits." — worded with "hit", not "reached", so the Claude line alone
#: would miss it. OpenAI's real API body (litellm `RateLimitError.message`, verified 2026-09-21
#: against OpenAI's own GitHub issue threads + docs —
#: `~/.github/research/openai-429-insufficient-quota-message-2026-09.md`): "You exceeded your
#: current quota..." (EXCEEDED before QUOTA — the literal substring "quota exceeded" would miss
#: it), and its newer per-org/per-project 429s speak in THIRD person — "Your organization/project
#: reached ITS ... limit", "Your organization has no prepaid credits remaining" — never "reached
#: YOUR", so a pattern anchored only on second person misses those too. `ServerOverloaded`
#: ("Selected model is at capacity") and a plain rate-limit-per-minute notice ("You are sending
#: requests too quickly") are deliberately excluded: transient load, not an account quota, and
#: do not deserve an hour cooldown.
REFUSAL = re.compile(
    r"reached (?:your|its) .{0,80}\b(limit|quota)\b"
    r"|exceeded (?:your|its) current quota"
    r"|no prepaid credits remaining"
    # Both apostrophes: Codex renders its own refusal with a CURLY one ("You’ve hit your
    # usage limit"), which the straight-quote pattern missed entirely — a refusal nobody
    # recognises has no window to read and no cooldown at all.
    r"|(?:you(?:['’]ve|r| have)?|your workspace) (?:hit|is out of credits)"
    r"[^\n]{0,80}?\b(usage limit|spend cap|credits)\b"
    r"|usage limit reached"
    r"|quota exceeded"
    r"|switch to another model"
    # A vendor's own rate-limit line only counts when it says the request was REFUSED: a healthy
    # child opens its stream with `rate_limit_info: {"status":"allowed"}`, and reading that as a
    # refusal skipped every candidate on the list.
    r"|rate[ _-]?limit(?:ed)?\b[^\n]{0,80}?\b(exceeded|rejected|reached)"
    r"|\"status\"\s*:\s*\"rejected\""
    # interact's OWN normalised rate-limit line (`providers.py`), which is all a downstream
    # reader gets once the vendor payload has been folded into an `AgentEvent`. It is also the
    # only line that still NAMES the exhausted window at that point, so failing to match it here
    # is what left the window unread and every refusal on a flat hour.
    r"|\blimit:\s*rejected\b",
    re.IGNORECASE,
)

#: Nominal length of each period a vendor NAMES in its own refusal, keyed on the vendor's literal
#: token. Anthropic's Claude Code writes these in `rate_limit_info.rateLimitType` and repeats them
#: as the keys of `rate_limit_info.unifiedWindows` — read off 1831 captured lines in
#: `~/.interact/out/agents/*.raw.jsonl`, where the only values ever seen are these three.
#: `seven_day_overage_included` is the same seven-day period with the overage pool counted in:
#: every captured payload gives it and `seven_day` the SAME `resetsAt`, which is why it maps to
#: the same length rather than a guessed longer one.
#:
#: Used only when a refusal names its window but no instant — the instant is always preferred.
#: The full nominal length is the honest floor there: a rolling window's remaining time is
#: unknowable from the name alone, and the two errors are not symmetric. Blocking too long costs
#: one fall-through to the next ranked candidate (and the walk ignores the memory outright when
#: that would empty the list); blocking too short costs a dead build.
WINDOWS: dict[str, float] = {
    "five_hour": 5 * 3600.0,
    "seven_day": 7 * 86400.0,
    "seven_day_overage_included": 7 * 86400.0,
}

#: How long a refused model is passed over when the vendor named NO window at all — a bare
#: "you've reached your limit" with nothing machine-readable behind it. Guessing long is not free
#: either when there is no evidence to guess from, so this stays an hour and a named window
#: overrides it.
DEFAULT_COOLDOWN = 3600.0
COOLDOWN_ENV = "INTERACT_QUOTA_COOLDOWN_SECONDS"

#: Added past an instant the vendor named, so the next launch does not race the boundary it was
#: just refused at: their clock and this machine's are not the same clock.
RESET_SLACK = 60.0

#: No period any of these vendors names is longer than seven days, so a reset instant beyond that
#: is a malformed payload, not a reason to bench a model for a year.
MAX_COOLDOWN = max(WINDOWS.values())

#: Every key any of these vendors uses to say WHEN the exhausted period reopens, as an absolute
#: instant. Verified against primary sources 2026-09-22 (`.github/research/
#: llm-rate-limit-window-vocabulary-2026-09.md`): Anthropic's Claude Code writes `resetsAt` in
#: epoch seconds on its own stream; Codex writes `resetsAt` (app-server `RateLimitWindow`, serde
#: camelCase) and `resets_at` (core protocol), both epoch seconds, pinned at `rust-v0.155.1`;
#: Anthropic's HTTP API writes its `anthropic-ratelimit-*-reset` headers in RFC 3339. Both
#: spellings of each, because one vendor uses camelCase over its CLI and snake_case in payloads.
_RESET_AT_KEYS = ("resetsAt", "resets_at", "resetAt", "reset_at",
                  "anthropic-ratelimit-requests-reset", "anthropic-ratelimit-tokens-reset",
                  "anthropic-ratelimit-input-tokens-reset",
                  "anthropic-ratelimit-output-tokens-reset")

#: The same thing counted DOWN instead of named: seconds from now. `Retry-After` is documented in
#: seconds by both OpenAI and Anthropic; `resets_in_seconds` is Codex's own field, confirmed at
#: `rust-v0.46.0` and replaced by `resets_at` since.
#:
#: Deliberately absent, each for a stated reason rather than an oversight: `retry-after-ms`
#: (milliseconds — only the OpenAI SDK's PARSER is confirmed, not that any server emits it, and
#: reading a millisecond count as seconds would block a model for weeks); OpenAI's
#: `x-ratelimit-reset-requests` / `-tokens` family (Go duration strings like `6m0s`, and they
#: describe the per-minute rate limit `REFUSAL` deliberately excludes, not an account quota);
#: Codex's own "Try again at 3:45 PM" suffix (local display time, often without a date and always
#: without an offset — unparseable without guessing a zone, and the payload carries an epoch).
_RESET_IN_KEYS = ("resets_in_seconds", "resetsInSeconds", "retry_after", "retryAfter",
                  "retry-after", "Retry-After")

#: How long the exhausted period IS, stated by the vendor as a number of minutes instead of a
#: name — Codex's `windowDurationMins` (app-server) and `window_minutes` (core protocol), whose
#: real values are 300 and 10080. Better evidence than `WINDOWS` below, which only exists because
#: Anthropic names its periods instead of measuring them.
_WINDOW_MINUTES_KEYS = ("windowDurationMins", "window_minutes", "windowMinutes")


def _key_pattern(keys: tuple[str, ...], value: str) -> re.Pattern[str]:
    """One regex per key family, built FROM the key tuples above so a token is spelled once."""
    return re.compile(rf"(?:{'|'.join(re.escape(k) for k in keys)})\W{{0,4}}({value})",
                      re.IGNORECASE)


#: The same keys again, for a line too mangled to parse as JSON — `_child_output` reads the TAIL
#: of the child's stream, so the first line it hands over is routinely cut in half.
_RESET_AT_IN_TEXT = _key_pattern(_RESET_AT_KEYS, r"\d{9,11}")
_RESET_IN_IN_TEXT = _key_pattern(_RESET_IN_KEYS, r"\d{1,7}")
#: An ISO-8601 instant anywhere in the line — how interact renders a reset into its own event
#: text, and how an HTTP-dated refusal spells one.
_ISO_INSTANT = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")
#: Longest first, so `seven_day_overage_included` is never read as a bare `seven_day`.
_WINDOW_IN_TEXT = re.compile(
    "|".join(re.escape(name) for name in sorted(WINDOWS, key=len, reverse=True)), re.IGNORECASE)


class Refusal(BaseModel):
    """What ONE line of a vendor's refusal said about the period it is refusing for.

    Read straight out of text, because text is the only substrate every call site shares: a JSONL
    line off the child's own stream, a provider exception's message, interact's own rendered
    event line. A caller hands over whatever it has and gets back one answer.
    """

    model_config = ConfigDict(extra="forbid")

    #: The vendor's LITERAL token for the exhausted period ("seven_day"); empty when it named none.
    window: str = ""
    #: The instant the vendor said that period reopens, absolute epoch seconds. None when it named
    #: only the period, or nothing at all.
    reopens_at: float | None = None
    #: How long the period IS, when the vendor stated its length as a number instead of a name.
    #: Beats `WINDOWS`, which can only look a name up in a table this code maintains.
    window_seconds: float | None = None

    @classmethod
    def read(cls, said: str, *, now: float) -> Self | None:
        """The longest block anything in `said` justifies, or None when nothing in it refused.

        Per LINE, never over the whole blob: a healthy child's stream is mostly `allowed` lines,
        each carrying a five-hour `resetsAt` an hour away, and grepping the blob for a reset
        instant finds one of those first — cutting a live seven-day block back to an hour, which
        is the bug in a new costume. A line that did not itself refuse is never read for a reset.
        """
        refusals = [found for line in said.splitlines()
                    if (found := cls._from_line(line, now=now)) is not None]
        if not refusals:
            return None
        return max(refusals, key=lambda found: found.until(now=now, default=0.0))

    def until(self, *, now: float, default: float) -> float:
        """When this refusal says the model may be tried again.

        The instant the vendor NAMED wins over the length its window name implies: a rolling
        window reopens when usage falls under the cap, not one full period after the refusal —
        measured over the captured runs, anywhere from three minutes to six days later.
        """
        if self.reopens_at is not None and self.reopens_at > now:
            return min(self.reopens_at + RESET_SLACK, now + MAX_COOLDOWN)
        length = self.window_seconds
        if length is None:
            length = WINDOWS.get(self.window, default)
        return now + min(length, MAX_COOLDOWN)

    @classmethod
    def _from_line(cls, line: str, *, now: float) -> Self | None:
        if not REFUSAL.search(line):
            return None
        refused = cls._refused_object(line)
        if refused is None:
            return cls(window=cls._window_in(line), reopens_at=cls._instant_in(line, now=now))
        window = refused.get("rateLimitType") or refused.get("rate_limit_type") or ""
        return cls(window=str(window), reopens_at=cls._instant_of(refused, now=now),
                   window_seconds=cls._length_of(refused))

    @staticmethod
    def _refused_object(line: str) -> dict[str, Any] | None:
        """The object that itself says it was refused, anywhere in a JSON line.

        Its OWN `resetsAt` is the one that matters. The same payload also carries a
        `unifiedWindows` map holding every window's reset, including pools that are nowhere near
        exhausted — borrowing a reset from those blocks a model for days it never refused for.
        """
        try:
            pending: list[Any] = [json.loads(line)]
        except ValueError:
            return None
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                if str(node.get("status", "")).lower() == "rejected":
                    return node
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)
        return None

    @classmethod
    def _instant_of(cls, refused: dict[str, Any], *, now: float) -> float | None:
        for key in _RESET_AT_KEYS:
            if (moment := cls._as_instant(refused.get(key))) is not None:
                return moment
        for key in _RESET_IN_KEYS:
            if (seconds := cls._as_number(refused.get(key))) is not None:
                return now + seconds
        return None

    @classmethod
    def _length_of(cls, refused: dict[str, Any]) -> float | None:
        for key in _WINDOW_MINUTES_KEYS:
            if (minutes := cls._as_number(refused.get(key))) is not None and minutes > 0:
                return minutes * 60.0
        return None

    @classmethod
    def _instant_in(cls, line: str, *, now: float) -> float | None:
        if (found := _RESET_AT_IN_TEXT.search(line)) is not None:
            return cls._as_instant(found.group(1))
        if (found := _RESET_IN_IN_TEXT.search(line)) is not None:
            return now + float(found.group(1))
        if (found := _ISO_INSTANT.search(line)) is not None:
            return cls._as_instant(found.group(1))
        return None

    @staticmethod
    def _window_in(line: str) -> str:
        found = _WINDOW_IN_TEXT.search(line)
        return found.group(0).lower() if found else ""

    @staticmethod
    def _as_number(value: Any) -> float | None:
        """A non-negative count of seconds, however the vendor typed it."""
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value) if value >= 0 else None
        if isinstance(value, str):
            with suppress(ValueError):
                return max(0.0, float(value.strip()))
        return None

    @staticmethod
    def _as_instant(value: Any) -> float | None:
        """An absolute epoch second, from a number or an ISO-8601 string.

        A naive timestamp is read as UTC: every vendor observed writes an offset, and guessing
        this machine's zone for one that does not would move the block by hours.
        """
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            # Epoch seconds, not milliseconds and not a duration that wandered into this field.
            return float(value) if value > 1e9 else None
        if isinstance(value, str):
            with suppress(ValueError):
                moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
                return (moment if moment.tzinfo is not None
                        else moment.replace(tzinfo=UTC)).timestamp()
        return None


def _path() -> Path:
    return Path.home() / ".interact" / "out" / "agents" / "quota-cooldowns.json"


def _cooldown() -> float:
    """The fallback length, for a refusal that named no window this code can read."""
    raw = os.environ.get(COOLDOWN_ENV)
    if raw is None:
        return DEFAULT_COOLDOWN
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_COOLDOWN


def _key(provider: str, model: str | None) -> str:
    return f"{provider}/{model or ''}"


def _read() -> dict[str, float]:
    with suppress(Exception):
        raw = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}
    return {}


def _write(entries: dict[str, float]) -> None:
    path = _path()
    with suppress(Exception):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, sort_keys=True), encoding="utf-8")
        tmp.replace(path)


def blocked_until(provider: str, model: str | None, *, now: float | None = None) -> float | None:
    """When this model may be tried again, or None when nothing is remembered against it."""
    moment = time.time() if now is None else now
    until = _read().get(_key(provider, model))
    return until if until is not None and until > moment else None


def record_refusal(provider: str, model: str | None, *, said: str = "",
                   now: float | None = None, cooldown: float | None = None) -> float:
    """Remember that this model refused for quota; returns when it may be tried again.

    `said` is whatever the vendor wrote — the child's raw stream, an exception message, one
    event's text. The window it names decides the length, so a call site that has the text is
    never reduced to the unnamed default; a call site that has none still records the refusal.
    """
    moment = time.time() if now is None else now
    if cooldown is not None:
        until = moment + cooldown
    elif (refusal := Refusal.read(said, now=moment)) is not None:
        until = refusal.until(now=moment, default=_cooldown())
    else:
        until = moment + _cooldown()
    entries = {k: v for k, v in _read().items() if v > moment}
    key = _key(provider, model)
    # A WEAKER signal never shortens a live block. A five-hour refusal arriving under a live
    # seven-day one says nothing about the seven-day pool, and neither does the generic error
    # line that follows every refusal a moment later carrying no window at all. Whatever is
    # written stands until IT expires; a new refusal may only push the block further out.
    # Clearing one early is `forget`'s job, deliberately, not a side effect of being refused.
    until = max(until, entries.get(key, until))
    entries[key] = until
    _write(entries)
    return until


def forget(provider: str | None = None, model: str | None = None) -> None:
    """Drop one remembered refusal, or all of them when called bare."""
    if provider is None:
        _write({})
        return
    entries = _read()
    entries.pop(_key(provider, model), None)
    _write(entries)
