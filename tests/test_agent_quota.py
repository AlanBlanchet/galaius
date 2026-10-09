"""A vendor's quota refusal, remembered past the run that heard it.

"three research agents died on your Fable 7-day quota, twice each": the refusal is a fact about
the ACCOUNT for a period, so hearing it once must spare every later launch the same dead child.
"""

import json
import time
from datetime import UTC, datetime

import pytest

from galaius.agents import quota
from galaius.agents import registry as reg
from galaius.agents.events import AgentEvent
from tests.support.agents import register_run


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
        # A real warning line (2026-10-05): the pool is nearly spent and overage is off — still
        # allowed, and the model still answers.
        ('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed_warning","resetsAt":'
         '1791237600,"rateLimitType":"seven_day","utilization":0.99,"overageStatus":"rejected",'
         '"isUsingOverage":false,"surpassedThreshold":0.75}}', False),
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
        # Claude Code names the pool it refused for (every quota-failed run on record, 2026-10), and
        # galaius's own launcher says a recorded window is still open.
        ("You've hit your weekly limit · resets Oct 6, 12am (Europe/Paris)", True),
        ("You've hit your limit · resets 9pm (Europe/Paris)", True),
        ("quota intent: claude/claude-opus-5-5 is still exhausted; the recorded window clears after 2026-10-05T22:01:00+00:00.", True),
        ("if you hit the wall, check your limits", False),
        # A child quoting the words (reading the launcher's code, a brief about it) refused nothing.
        ("the server reads « You've hit your weekly limit » and « … is still exhausted »", False),
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


HOUR = quota.DEFAULT_COOLDOWN
NOW = 1_791_096_843.0  # 2026-10-04 06:54Z, when the refusal below was heard

# The verbatim line that benched claude-opus-5-5 for 39 h (run d675c2c7, 2026-10-04 06:54:03Z): a
# GENUINE refusal — the seven-day pool at 100 %, overage off — naming the pool's reset, 2026-10-05
# 22:00Z. Hours later the same account served the same model again (utilisation back to 0.62).
SEVEN_DAY_REFUSAL = json.dumps({
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "rejected", "resetsAt": 1791237600, "rateLimitType": "seven_day",
        "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled",
        "isUsingOverage": False,
        "unifiedWindows": {"five_hour": {"utilization": 0.05, "resetsAt": 1791111000},
                           "seven_day": {"utilization": 1, "resetsAt": 1791237600}},
    },
})
# The healthy line every child opens with — `allowed`, and a five-hour reset ten minutes away.
ALLOWED_LINE = json.dumps({
    "type": "rate_limit_event",
    "rate_limit_info": {"status": "allowed", "resetsAt": NOW + 600, "rateLimitType": "five_hour"},
})


def _rejected(**fields) -> str:
    return json.dumps({"rate_limit_info": {"status": "rejected", **fields}})


@pytest.mark.parametrize(("said", "blocked_for"), [
    pytest.param(SEVEN_DAY_REFUSAL, HOUR, id="one seven-day refusal: an hour, not the 39 h to its reset"),
    pytest.param("seven_day limit: rejected", HOUR, id="a named window alone: an hour"),
    pytest.param("You've reached your Fable limit. Switch to another model.", HOUR, id="no window named"),
    pytest.param(_rejected(rateLimitType="five_hour", resetsAt=NOW + 600), 600 + quota.RESET_SLACK,
                 id="a reset sooner than an hour wins"),
    pytest.param(_rejected(resets_in_seconds=900), 900 + quota.RESET_SLACK, id="a countdown wins too"),
    pytest.param(json.dumps({"status": "rejected", "anthropic-ratelimit-tokens-reset":
                             datetime.fromtimestamp(NOW + 1800, UTC).isoformat()}),
                 1800 + quota.RESET_SLACK, id="an RFC 3339 reset header"),
    pytest.param(json.dumps({"status": "rejected", "windowDurationMins": 20}), 20 * 60,
                 id="a measured window shorter than an hour"),
    pytest.param(_rejected(resetsAt=NOW - 5000), HOUR, id="a reset already past is ignored"),
    pytest.param(_rejected(resetsAt=NOW + 99 * 86400), HOUR, id="an absurd reset is ignored"),
    pytest.param(json.dumps({"status": "rejected", "retry-after-ms": 60000}), HOUR,
                 id="a millisecond hint is not read as seconds"),
    pytest.param("\n".join([ALLOWED_LINE, "seven_day limit: rejected", ALLOWED_LINE]), HOUR,
                 id="the healthy lines around a refusal lend it no reset"),
])
def test_one_refusal_blocks_an_hour_at_most_and_sooner_if_the_vendor_says_so(said, blocked_for):
    """Owner 2026-10-05: « A warning or one refusal must never block a model for 10 h »."""
    assert quota.record_refusal("claude", "m", said=said, now=NOW) == pytest.approx(NOW + blocked_for)


def test_refusals_in_a_row_double_the_block_and_a_quiet_spell_resets_it():
    """Refused again at the re-check, the window really is still shut: twice as long each time,
    never past what the vendor named. Refused again much later, it is a new story."""
    blocks, moment = [], NOW
    for _ in range(4):
        until = quota.record_refusal("claude", "m", said=SEVEN_DAY_REFUSAL, now=moment)
        blocks.append(until - moment)
        moment = until  # tried again the moment the block ends, refused again
    assert blocks == pytest.approx([HOUR, 2 * HOUR, 4 * HOUR, 8 * HOUR])
    # the fifth would earn 16 h; a reset the vendor names 5 h out still bounds it
    capped = quota.record_refusal("claude", "m", said=_rejected(resetsAt=moment + 5 * HOUR), now=moment)
    assert capped == pytest.approx(moment + 5 * HOUR + quota.RESET_SLACK)
    later = 1791237600 + 30 * 86400
    assert quota.record_refusal("claude", "m", said="seven_day limit: rejected", now=later) == pytest.approx(later + HOUR)


def test_a_refusal_heard_inside_its_block_neither_escalates_nor_shortens_it():
    """The error line that follows every rate-limit line says the same thing again."""
    until = quota.record_refusal("claude", "m", said=SEVEN_DAY_REFUSAL, now=NOW)
    again = quota.record_refusal("claude", "m", said=_rejected(resetsAt=NOW + 120), now=NOW + 60)
    assert again == pytest.approx(until)
    assert quota.record_refusal("claude", "m", now=until) == pytest.approx(until + 2 * HOUR)


def test_a_model_coming_off_its_block_is_on_probation_then_forgotten():
    until = quota.record_refusal("claude", "m", now=NOW)
    assert quota.blocked_until("claude", "m", now=NOW + 1) == pytest.approx(until)
    assert not quota.on_probation("claude", "m", now=NOW + 1)
    assert quota.blocked_until("claude", "m", now=until + 1) is None
    assert quota.on_probation("claude", "m", now=until + 1)
    assert not quota.on_probation("claude", "m", now=until + HOUR + 1)
    assert not quota.on_probation("claude", "never-refused", now=NOW)


def test_the_model_serving_again_clears_its_block_at_once():
    quota.record_refusal("claude", "m", said=SEVEN_DAY_REFUSAL)
    quota.record_refusal("claude", "other")
    quota.served("claude", "m")
    assert quota.blocked_until("claude", "m") is None and not quota.on_probation("claude", "m")
    assert quota.blocked_until("claude", "other") is not None


def test_only_the_model_answering_clears_its_block(tmp_path, monkeypatch):
    """A warning, or the five-hour pool allowing while the seven-day one refuses, proves nothing;
    a turn the model wrote (it read tokens; the CLI's own notice reads none) does."""
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(tmp_path / "agents"))
    register_run("r1", provider="claude", model="claude-opus-5-5")
    reg.append_event("r1", AgentEvent(kind="rate_limit", text="seven_day limit: rejected"))
    assert quota.blocked_until("claude", "claude-opus-5-5") is not None
    reg.append_event("r1", AgentEvent(kind="rate_limit", text="five_hour limit: allowed"))
    reg.append_event("r1", AgentEvent(kind="rate_limit", text="seven_day limit: allowed_warning"))
    reg.append_event("r1", AgentEvent(kind="text", text="You've hit your weekly limit", input_tokens=0))
    assert quota.blocked_until("claude", "claude-opus-5-5") is not None
    reg.append_event("r1", AgentEvent(kind="text", text="Looking at the code now.", input_tokens=1200))
    assert quota.blocked_until("claude", "claude-opus-5-5") is None


def test_the_memory_is_per_model_not_per_vendor():
    """Falling through to another model of the same vendor is the whole point."""
    quota.record_refusal("claude", "claude-fable-5-1")
    assert quota.blocked_until("claude", "claude-fable-5-1") is not None
    assert quota.blocked_until("claude", "claude-opus-5") is None


@pytest.mark.parametrize(("setting", "block"), [("120", 120.0), ("soon please", HOUR)])
def test_the_first_block_is_configurable_per_machine(monkeypatch, setting, block):
    monkeypatch.setenv(quota.COOLDOWN_ENV, setting)
    assert quota.record_refusal("claude", "m", now=NOW) == pytest.approx(NOW + block)


def test_forgotten_notes_do_not_accumulate():
    """The file is rewritten from what is still worth remembering, so it cannot grow without bound."""
    quota.record_refusal("claude", "stale", now=time.time() - 10 * HOUR)
    quota.record_refusal("claude", "fresh")
    assert set(quota._read(time.time())) == {"claude/fresh"}


@pytest.mark.parametrize("stored", ["{not json", '{"claude/m": 1891237660.0}', '{"claude/m": {"until": "soon"}}'])
def test_anything_but_a_block_in_the_store_reads_as_nothing(stored):
    """A corrupt file, or an older galaius's bare deadline — the 39 h kind — is no evidence."""
    quota._path().parent.mkdir(parents=True, exist_ok=True)
    quota._path().write_text(stored, encoding="utf-8")
    assert quota.blocked_until("claude", "m", now=NOW) is None
    quota.record_refusal("claude", "m")
    assert quota.blocked_until("claude", "m") is not None


def test_text_that_never_refused_is_not_a_refusal():
    """`said` is whatever the vendor wrote; a healthy stream names nothing to remember."""
    assert quota.Refusal.read(ALLOWED_LINE, now=NOW) is None


@pytest.mark.parametrize(("said", "now", "reopens"), [
    # 18:00 Paris (16:00 UTC) on 5 Oct: « 9pm » is tonight; « 12am » on Oct 6 is midnight Paris.
    ("You've hit your weekly limit · resets 9pm (Europe/Paris)", "2026-10-05T16:00:00+00:00", "2026-10-05T19:00:00+00:00"),
    ("You've hit your weekly limit · resets Oct 6, 12am (Europe/Paris)", "2026-10-05T16:00:00+00:00", "2026-10-05T22:00:00+00:00"),
    # Past 9pm, « 9pm » is tomorrow's; a date already gone this year is next year's.
    ("You've hit your limit · resets 9pm (Europe/Paris)", "2026-10-05T20:30:00+00:00", "2026-10-06T19:00:00+00:00"),
    ("You've hit your limit · resets 3:30pm (UTC)", "2026-10-05T10:00:00+00:00", "2026-10-05T15:30:00+00:00"),
    # « Jan 2 » said on Dec 30 is next year's.
    ("You've hit your weekly limit · resets Jan 2, 9am (UTC)", "2026-12-30T10:00:00+00:00", "2027-01-02T09:00:00+00:00"),
])
def test_a_spoken_reset_in_a_named_zone_is_read_as_its_instant(said, now, reopens):
    """Claude Code's refusal names neither a window this code knows nor an epoch, only a time in
    a zone — read as the instant it says, the latest the block may last."""
    moment = datetime.fromisoformat(now).timestamp()
    assert quota.Refusal.read(said, now=moment).latest(now=moment) == pytest.approx(
        datetime.fromisoformat(reopens).timestamp() + quota.RESET_SLACK)


@pytest.mark.parametrize(("said", "now", "blocked_for"), [
    pytest.param("You've hit your limit · resets 3:30pm (UTC)", "2026-10-05T15:10:00+00:00",
                 20 * 60 + quota.RESET_SLACK, id="a spoken reset sooner than an hour wins"),
    pytest.param("You've hit your weekly limit · resets Oct 6, 12am (Europe/Paris)", "2026-10-05T22:02:00+00:00",
                 HOUR, id="a dated reset just gone is never next year's"),
    pytest.param("You've hit your weekly limit · resets 9pm (Mars/Olympus)", "2026-10-05T10:00:00+00:00",
                 HOUR, id="an unknown zone names nothing"),
])
def test_a_spoken_reset_only_ever_shortens_the_block(said, now, blocked_for):
    moment = datetime.fromisoformat(now).timestamp()
    assert quota.record_refusal("claude", "m", said=said, now=moment) == pytest.approx(moment + blocked_for)


# Claude Code's five-hour refusal of 2026-10-09, the three lines its stream ends on (usage trimmed):
# the pool reopened at 15:20:00Z (« 5:20pm » Paris) and the model answered again right then.
SESSION_RESET = 1791559200.0
SESSION_SAID = "You've hit your session limit · resets 5:20pm (Europe/Paris)"
SESSION_LINES = {
    "rate-limit line": json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": 1791559200, "rateLimitType": "five_hour",
        "overageStatus": "rejected", "isUsingOverage": False,
        "unifiedWindows": {"five_hour": {"utilization": 1, "resetsAt": 1791559200},
                           "seven_day": {"utilization": 0.61, "resetsAt": 1791842400}}}}),
    # The CLI's own notice: the line's `timestamp` is when it was written, not when anything resets.
    "synthetic assistant line": json.dumps({
        "type": "assistant", "message": {"model": "<synthetic>", "role": "assistant",
                                         "content": [{"type": "text", "text": SESSION_SAID}]},
        "timestamp": "2026-10-09T15:15:34.961Z", "error": "rate_limit", "is_api_error_message": True}),
    "result line": json.dumps({"type": "result", "subtype": "success", "is_error": True,
                               "api_error_status": 429, "terminal_reason": "api_error",
                               "result": SESSION_SAID}),
    "galaius event text": SESSION_SAID,
    # A refusing object that names no reset of its own: the words beside it still do.
    "refusal object without a reset key": json.dumps({
        "type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"},
        "message": SESSION_SAID}),
    "ISO reset beside a keyless refusal object": json.dumps({
        "rate_limit_info": {"status": "rejected"}, "text": "five_hour limit: rejected; resets 2026-10-09T15:20:00+00:00"}),
}


@pytest.mark.parametrize("heard", [
    pytest.param("2026-10-09T15:15:35+00:00", id="heard when refused"),
    # The block that benched the model until 18:20 Paris: heard 25 s after the instant it names.
    pytest.param("2026-10-09T15:20:25+00:00", id="heard at the reset it names"),
])
@pytest.mark.parametrize("said", SESSION_LINES.values(), ids=SESSION_LINES.keys())
def test_a_refusal_naming_its_reset_blocks_until_then_never_the_flat_hour(said, heard):
    moment = datetime.fromisoformat(heard).timestamp()
    assert quota.record_refusal("claude", "m", said=said, now=moment) == pytest.approx(
        SESSION_RESET + quota.RESET_SLACK)


def test_a_probe_refused_for_quota_never_blocks_the_owners_own_runs(tmp_path, monkeypatch):
    """A measurement run in its own registry (`GALAIUS_AGENTS_DIR`) keeps its cooldown there."""
    owner, probe = tmp_path / "owner-agents", tmp_path / "probe-agents"
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(probe))
    quota.record_refusal("claude", "claude-opus-5-5", said="You've reached your limit")
    assert quota.blocked_until("claude", "claude-opus-5-5") is not None

    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(owner))
    assert quota.blocked_until("claude", "claude-opus-5-5") is None
    assert (probe / "quota-cooldowns.json").exists() and not (owner / "quota-cooldowns.json").exists()
