"""A vendor's quota refusal, remembered past the run that heard it.

"three research agents died on your Fable 7-day quota, twice each": the refusal is a fact about
the ACCOUNT for a period, so hearing it once must spare every later launch the same dead child.
"""

import json
import time
from datetime import UTC, datetime

import pytest

from interact.agents import quota


@pytest.fixture(autouse=True)
def _own_store(tmp_path, monkeypatch):
    """Every test writes its own cooldown file, never the developer's."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(quota.COOLDOWN_ENV, raising=False)
    quota.forget()


@pytest.mark.parametrize(
    ("said", "refusal"),
    [
        ("You've reached your Fable limit. Switch to another model", True),
        ('{"rate_limit_info":{"status":"rejected"}}', True),
        ("Error: usage limit reached", True),
        ("openai: rate limit exceeded, retry later", True),
        ('{"rate_limit_info":{"status":"allowed"},"type":"system"}', False),
        ('{"type":"system","subtype":"rate_limit_event"}', False),
        ("check the rate limit dashboard when you have a moment", False),
        # Codex's own wording (`codex-rs/protocol/src/error.rs`, `UsageLimitReached` display
        # strings, verified 2026-09-21): "hit", never "reached" — the Claude line alone misses it.
        ("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
         "visit https://chatgpt.com/codex/settings/usage to purchase more credits Try again later.", True),
        ("You've hit your usage limit for gpt-5.6-luna. Switch to another model now, "
         "or try again at 3:45 PM.", True),
        # Codex writes a CURLY apostrophe; the straight-quote pattern matched neither the
        # refusal nor, therefore, the window behind it.
        ("You’ve hit your usage limit. Upgrade to Pro to continue.", True),
        ("You’ve reached your Fable limit. Switch to another model.", True),
        ("Your workspace is out of credits. Add credits to continue.", True),
        ("You hit your spend cap set in your workspace. Increase your spend cap to continue.", True),
        ("Quota exceeded. Check your plan and billing details.", True),
        # OpenAI's own real wording (litellm RateLimitError.message; verified 2026-09-21 against
        # OpenAI's own GitHub issue thread + docs — `~/.github/research/
        # openai-429-insufficient-quota-message-2026-09.md`): EXCEEDED before QUOTA, and the
        # newer per-org/per-project 429s say "reached ITS ... limit", never "reached YOUR" —
        # third person, not second — which the original pattern only matched in second person.
        ("You exceeded your current quota, please check your plan and billing details.", True),
        ("Your organization reached its enforced spend limit.", True),
        ("Your project reached its enforced spend limit.", True),
        ("Your organization reached its OpenAI-assigned usage limit.", True),
        ("Your organization has no prepaid credits remaining.", True),
        # Transient server-side overload, not an account quota — must not earn an hour cooldown.
        ("Selected model is at capacity. Please try a different model.", False),
        ("You are sending requests too quickly.", False),
        ("Your request rate increased too quickly.", False),
    ],
)
def test_only_a_refusal_reads_as_one(said, refusal):
    """A healthy child OPENS its stream with a rate-limit line saying `allowed`; reading that as
    a refusal passed over every candidate on the ranked list."""
    assert bool(quota.REFUSAL.search(said)) is refusal


def test_a_refused_model_is_remembered_then_forgotten_on_its_own():
    now = time.time()
    quota.record_refusal("claude", "claude-fable-5-1", now=now, cooldown=60)
    assert quota.blocked_until("claude", "claude-fable-5-1", now=now) == pytest.approx(now + 60)
    assert quota.blocked_until("claude", "claude-fable-5-1", now=now + 61) is None


def test_the_memory_is_per_model_not_per_vendor():
    """Falling through to another model of the same vendor is the whole point."""
    quota.record_refusal("claude", "claude-fable-5-1", cooldown=60)
    assert quota.blocked_until("claude", "claude-fable-5-1") is not None
    assert quota.blocked_until("claude", "claude-opus-5") is None


def test_the_cooldown_length_is_configurable(monkeypatch):
    monkeypatch.setenv(quota.COOLDOWN_ENV, "120")
    now = time.time()
    assert quota.record_refusal("claude", "m", now=now) == pytest.approx(now + 120)


def test_a_nonsense_cooldown_setting_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(quota.COOLDOWN_ENV, "soon please")
    now = time.time()
    assert quota.record_refusal("claude", "m", now=now) == pytest.approx(now + quota.DEFAULT_COOLDOWN)


def test_expired_notes_do_not_accumulate():
    """The file is rewritten from what is still true, so it cannot grow without bound."""
    old = time.time() - 10_000
    quota.record_refusal("claude", "stale", now=old, cooldown=1)
    quota.record_refusal("claude", "fresh", cooldown=600)
    assert set(quota._read()) == {"claude/fresh"}


def test_a_corrupt_store_is_read_as_empty(tmp_path):
    quota._path().parent.mkdir(parents=True, exist_ok=True)
    quota._path().write_text("{not json", encoding="utf-8")
    assert quota.blocked_until("claude", "m") is None
    quota.record_refusal("claude", "m", cooldown=60)
    assert quota.blocked_until("claude", "m") is not None


# A verbatim `rate_limit_event` line from a run that really died this way
# (`~/.interact/out/agents/d03383b9-bc08-4425-8dc2-56bb5b21bd42.raw.jsonl`): the seven-day pool is
# the one exhausted, and the payload says so AND says when it reopens.
SEVEN_DAY_REFUSAL = json.dumps({
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "rejected",
        "resetsAt": 1790535600,
        "rateLimitType": "seven_day_overage_included",
        "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled",
        "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.46, "resetsAt": 1789999800},
            "seven_day": {"utilization": 0.56, "resetsAt": 1790535600},
            "seven_day_overage_included": {"utilization": 1, "resetsAt": 1790535600},
        },
    },
})
# The healthy line every child opens with — same shape, `allowed`, and a five-hour `resetsAt` an
# hour away. Reading a reset instant off THIS is how a seven-day block would get cut to an hour.
ALLOWED_LINE = json.dumps({
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed", "resetsAt": 1789999800, "rateLimitType": "five_hour",
        "overageStatus": "rejected", "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.46, "resetsAt": 1789999800},
            "seven_day": {"utilization": 0.56, "resetsAt": 1790535600},
        },
    },
})


def test_a_seven_day_refusal_blocks_until_the_seven_day_window_reopens():
    """The refusal NAMES which pool is exhausted and when it reopens; an hour is not it.

    Measured over the 60 real refusals in `~/.interact/out/agents/*.raw.jsonl`, the gap between
    a refusal and its own `resetsAt` ran 0.05 h to 151 h. A flat hour walked straight back into
    the wall, three crashed overnight builds deep.
    """
    now = 1790535600 - 100 * 3600
    until = quota.record_refusal("claude", "fable", said=SEVEN_DAY_REFUSAL, now=now)
    assert until == pytest.approx(1790535600 + quota.RESET_SLACK)
    assert quota.blocked_until("claude", "fable", now=now + 99 * 3600) is not None


def test_a_five_hour_refusal_blocks_only_for_the_five_hour_window():
    """The same parser must not round every refusal up to the longest window it knows."""
    now = 1789999800 - 4 * 3600
    said = json.dumps({"rate_limit_info": {
        "status": "rejected", "resetsAt": 1789999800, "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": 1, "resetsAt": 1789999800},
                           "seven_day": {"utilization": 0.3, "resetsAt": 1790535600}},
    }})
    until = quota.record_refusal("claude", "fable", said=said, now=now)
    assert until == pytest.approx(1789999800 + quota.RESET_SLACK)
    # The seven-day pool is NOT exhausted — its far-off reset must not be borrowed.
    assert quota.blocked_until("claude", "fable", now=1789999800 + 3600) is None


def test_an_explicit_reset_beats_the_window_its_name_implies():
    """A concrete instant is the vendor's own answer; the nominal length is only a guess."""
    now = 1_800_000_000.0
    said = json.dumps({"rate_limit_info": {
        "status": "rejected", "rateLimitType": "seven_day", "resetsAt": now + 900,
    }})
    assert quota.record_refusal("claude", "m", said=said, now=now) == pytest.approx(
        now + 900 + quota.RESET_SLACK)


def test_a_relative_reset_is_read_too():
    """Some vendors count down instead of naming an instant."""
    now = 1_800_000_000.0
    assert quota.record_refusal(
        "codex", "m", said='{"status":"rejected","resets_in_seconds":7200}', now=now,
    ) == pytest.approx(now + 7200 + quota.RESET_SLACK)


def test_a_named_window_with_no_instant_falls_back_to_that_window_s_length():
    """interact's own event line keeps the window name after the payload is clipped away."""
    now = 1_800_000_000.0
    until = quota.record_refusal("claude", "m", said="seven_day limit: rejected", now=now)
    assert until == pytest.approx(now + quota.WINDOWS["seven_day"])


def test_a_refusal_naming_no_window_gets_the_default():
    """Guessing long on an unnamed window is not free either — the default stays the default."""
    now = 1_800_000_000.0
    said = "You've reached your Fable limit. Switch to another model, or manage usage credits."
    assert quota.record_refusal("claude", "m", said=said, now=now) == pytest.approx(
        now + quota.DEFAULT_COOLDOWN)


def test_a_weaker_refusal_never_shortens_a_live_block():
    """The bug that cost three overnight builds: the hour expired, the seven-day window did not.

    A five-hour refusal arriving under a live seven-day block says nothing about the seven-day
    pool, so it may extend the block, never cut it.
    """
    now = 1_800_000_000.0
    long_until = quota.record_refusal("claude", "fable", said="seven_day limit: rejected", now=now)
    later = quota.record_refusal(
        "claude", "fable", said="five_hour limit: rejected", now=now + 60)
    assert later == pytest.approx(long_until)
    assert quota.blocked_until("claude", "fable", now=now + 6 * 3600) == pytest.approx(long_until)


def test_a_bare_record_refusal_never_shortens_a_live_block_either():
    """Every call site is covered, including the ones that have no text to hand."""
    now = 1_800_000_000.0
    long_until = quota.record_refusal("claude", "fable", said=SEVEN_DAY_REFUSAL, now=now)
    assert quota.record_refusal("claude", "fable", now=now + 60) == pytest.approx(long_until)


def test_a_longer_block_still_replaces_a_shorter_live_one():
    now = 1_800_000_000.0
    quota.record_refusal("claude", "fable", said="five_hour limit: rejected", now=now)
    until = quota.record_refusal("claude", "fable", said="seven_day limit: rejected", now=now)
    assert until == pytest.approx(now + quota.WINDOWS["seven_day"])


def test_the_healthy_lines_around_a_refusal_are_not_read_as_its_reset():
    """A child's stream is mostly `allowed` lines, each carrying a five-hour `resetsAt`.

    Grepping the whole blob for a reset instant finds those first and cuts a seven-day block down
    to an hour — the same failure in a new costume.
    """
    now = 1790535600 - 100 * 3600
    stream = "\n".join([ALLOWED_LINE, ALLOWED_LINE, SEVEN_DAY_REFUSAL, ALLOWED_LINE])
    assert quota.record_refusal("claude", "fable", said=stream, now=now) == pytest.approx(
        1790535600 + quota.RESET_SLACK)


def test_a_reset_already_in_the_past_is_ignored():
    """A stale line from a finished run must not write a block that is already over."""
    now = 1_800_000_000.0
    said = json.dumps({"rate_limit_info": {
        "status": "rejected", "rateLimitType": "five_hour", "resetsAt": now - 5_000}})
    assert quota.record_refusal("claude", "m", said=said, now=now) == pytest.approx(
        now + quota.WINDOWS["five_hour"])


def test_an_absurd_reset_is_clamped_to_the_longest_window_anyone_names():
    """No window these vendors name is longer than seven days; a wilder number is a bug, not a
    reason to bench a model for a year."""
    now = 1_800_000_000.0
    said = json.dumps({"rate_limit_info": {"status": "rejected", "resetsAt": now + 99 * 86400}})
    assert quota.record_refusal("claude", "m", said=said, now=now) == pytest.approx(
        now + quota.MAX_COOLDOWN)


def test_an_explicit_cooldown_argument_still_wins():
    """Callers that already know the answer (tests, an operator override) are not second-guessed."""
    now = 1_800_000_000.0
    assert quota.record_refusal(
        "claude", "m", said=SEVEN_DAY_REFUSAL, now=now, cooldown=30) == pytest.approx(now + 30)


def test_text_that_never_refused_is_not_a_refusal():
    """`said` is whatever the vendor wrote; a healthy stream must write no block at all."""
    now = 1_800_000_000.0
    assert quota.Refusal.read(ALLOWED_LINE, now=now) is None


def test_a_window_the_vendor_MEASURES_beats_the_one_this_code_can_name():
    """Codex states the period's length in minutes (`windowDurationMins`, real values 300 and
    10080) instead of naming it. The vendor's own number needs no lookup table.

    Source: `codex-rs/app-server-protocol/src/protocol/v2/account.rs`, `RateLimitWindow`, pinned
    at `rust-v0.155.1`, checked 2026-09-22.
    """
    now = 1_800_000_000.0
    said = json.dumps({"status": "rejected", "windowDurationMins": 300})
    assert quota.record_refusal("codex", "m", said=said, now=now) == pytest.approx(
        now + 300 * 60)


def test_an_rfc_3339_reset_header_is_read_as_an_instant():
    """Anthropic's HTTP API dates its reset headers instead of counting seconds."""
    now = datetime(2026, 9, 27, 18, 0, tzinfo=UTC).timestamp()
    said = json.dumps({"status": "rejected",
                       "anthropic-ratelimit-tokens-reset": "2026-09-27T19:00:00Z"})
    assert quota.record_refusal("claude", "m", said=said, now=now) == pytest.approx(
        datetime(2026, 9, 27, 19, 0, tzinfo=UTC).timestamp() + quota.RESET_SLACK)


def test_a_millisecond_retry_hint_is_left_unparsed_rather_than_read_as_seconds():
    """`retry-after-ms` is confirmed only in OpenAI's SDK PARSER, never as something a server
    sends. Reading 60000 of them as seconds would bench a model for sixteen hours."""
    now = 1_800_000_000.0
    said = json.dumps({"status": "rejected", "retry-after-ms": 60000})
    assert quota.record_refusal("claude", "m", said=said, now=now) == pytest.approx(
        now + quota.DEFAULT_COOLDOWN)
