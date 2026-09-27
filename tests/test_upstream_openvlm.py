"""Parsing the OpenVLM leaderboards.

Two defects found live on 2026-08-18, which together kept every image/video benchmark on a
hand-written fallback while looking like a working live source:

1. The payload gained a wrapper — `{"time": ..., "results": {model: {...}}}` — and the parser
   iterated the TOP level, so it matched nothing and returned zero entries. Zero entries is a
   silent failure: the panel just keeps serving the packaged snapshot.
2. Each fetch stamped `retrieved` with TODAY, so a leaderboard that itself stopped updating in
   September 2025 would have claimed to be current.
"""

import json

import pytest

from interact.benchmarks.upstream import GroundingLeaderboardJS

_PAYLOAD = json.dumps({
    "time": "20250917132916",
    "results": {
        "GPT-4o (0513, detail-high)": {"MMMU_VAL": {"Overall": 70.7}},
        "Some-Other-VLM": {"MMMU_VAL": {"Overall": 62.5}},
        "No-Score-Model": {"MMBench_TEST_EN": {"Overall": 80.0}},
    },
})


def _source():
    return GroundingLeaderboardJS(
        id="mmmu_openvlm", name="MMMU", url="https://example.test/OpenVLM.json",
        benchmark_id="mmmu", root_path=("results",), score_path=("MMMU_VAL", "Overall"),
        score_scale=0.01,
    )


def test_models_are_read_through_the_results_wrapper():
    table = _source().parse(_PAYLOAD)
    assert [e.model_name for e in table.entries] == ["GPT-4o (0513, detail-high)", "Some-Other-VLM"]
    assert table.entries[0].score == pytest.approx(0.707)


def test_a_model_without_this_benchmark_is_skipped_not_zeroed():
    """Scoring it 0 would rank an unmeasured model below every measured one, as if it were bad."""
    assert all(e.model_name != "No-Score-Model" for e in _source().parse(_PAYLOAD).entries)


def test_the_leaderboards_OWN_timestamp_becomes_the_retrieved_date():
    """Stamping today's date on a table that stopped updating in 2025 is how stale data passes
    for current — the exact defect the visible date label exists to prevent."""
    assert _source().parse(_PAYLOAD).retrieved == "2025-09-17"


def test_a_payload_with_no_timestamp_falls_back_to_the_fetch_date():
    payload = json.dumps({"results": {"M": {"MMMU_VAL": {"Overall": 50.0}}}})
    assert _source().parse(payload).retrieved  # a date, not empty


def test_a_missing_root_is_an_empty_table_rather_than_a_crash():
    assert _source().parse(json.dumps({"unexpected": {}})).entries == []


# The two OpenVLM leaderboards stamp `time` differently — the image one 14 digits
# (YYYYMMDDHHMMSS), the video one 12 (YYMMDDHHMMSS). Slicing the first eight characters of both
# turned 250625130006 into "2506-25-13", a date that does not exist, printed straight at the user.


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("20250917132916", "2025-09-17"),  # image leaderboard, 14 digits
        ("250625130006", "2025-06-25"),    # video leaderboard, 12 digits
        ("", ""),
        ("not-a-date", ""),
        ("20259917132916", ""),            # month 99 — a plausible-looking slice that is not a date
        ("2025", ""),
    ],
)
def test_a_leaderboard_stamp_becomes_a_real_date_or_nothing(stamp, expected):
    from interact.benchmarks.upstream import _leaderboard_date

    assert _leaderboard_date({"time": stamp}) == expected
