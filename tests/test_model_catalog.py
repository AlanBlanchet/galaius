"""Live model metadata, so the panel shows what is TRUE today rather than what was baked in.

The shipped `models.json` is a snapshot that ages silently — a user reads a context window or a
price that stopped being right months ago and has no way to tell. This fetches OpenRouter's
public catalog (no key, no signup) and falls back to LiteLLM's MIT-licensed static file, which is
already a dependency.

The rule these tests encode: **the catalog always says where it came from and how old it is.**
Data whose provenance is invisible is exactly the problem being fixed, so a stale cache is
allowed to be served — but never allowed to pretend it is fresh.
"""

import json
import time
from pathlib import Path

import pytest

from interact import model_catalog as mc
from tests.support.models import catalog_of


_OPENROUTER = {
    "data": [
        {
            "id": "anthropic/claude-sonnet-5",
            "name": "Claude Sonnet 5",
            "context_length": 1_000_000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "architecture": {"input_modalities": ["text", "image"]},
        },
        {
            "id": "google/gemini-3-pro",
            "name": "Gemini 3 Pro",
            "context_length": 2_000_000,
            "pricing": {"prompt": "0.00000125", "completion": "0.00001"},
            "architecture": {"input_modalities": ["text", "image", "video"]},
        },
    ]
}


def _serve(monkeypatch, payload=_OPENROUTER, fail=False):
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        if fail:
            raise OSError("no network")

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return payload

        return R()

    monkeypatch.setattr(mc.httpx, "get", fake_get)
    return calls


def test_a_live_fetch_is_parsed_and_marked_live(monkeypatch):
    _serve(monkeypatch)
    cat = mc.load_catalog()
    assert cat.source == "openrouter"
    assert cat.age_seconds < 5
    ids = {m.id for m in cat.models}
    assert "anthropic/claude-sonnet-5" in ids
    sonnet = next(m for m in cat.models if m.id == "anthropic/claude-sonnet-5")
    assert sonnet.context_length == 1_000_000
    assert sonnet.input_cost_per_token == pytest.approx(3e-6)
    assert "image" in sonnet.input_modalities


def test_no_network_falls_back_and_says_so(monkeypatch):
    _serve(monkeypatch, fail=True)
    cat = mc.load_catalog()
    assert cat.source in ("litellm", "bundled")
    assert cat.models, "the fallback must actually carry models"
    # The whole point: the caller can tell this is not live.
    assert cat.is_live is False


def test_a_fresh_cache_is_not_refetched(monkeypatch):
    calls = _serve(monkeypatch)
    mc.load_catalog()
    mc.load_catalog()  # a second call, as a second process would: same cache file on disk
    assert len(calls) == 1, "a fresh cache must not hit the network again"


def test_refresh_bypasses_the_ttl_so_a_long_lived_server_does_not_serve_startup_data(monkeypatch):
    """`load_catalog` used to be `@lru_cache`d, which made any periodic refresher a silent no-op
    after its first call — the server would keep serving whatever it read the day it started."""
    calls = _serve(monkeypatch)
    mc.load_catalog()
    mc.load_catalog(refresh=True)
    assert len(calls) == 2, "refresh must re-fetch even while the cache is still inside its TTL"


def test_a_stale_cache_is_served_when_the_network_is_down_but_reports_its_age(monkeypatch):
    _serve(monkeypatch)
    mc.load_catalog()
    # Age the cache well past its TTL, then take the network away.
    raw = json.loads(mc.cache_path().read_text())
    raw["fetched_at"] = time.time() - (mc.TTL_SECONDS * 10)
    mc.cache_path().write_text(json.dumps(raw))
    _serve(monkeypatch, fail=True)

    cat = mc.load_catalog()
    assert cat.models, "a stale cache beats no data"
    assert cat.age_seconds > mc.TTL_SECONDS
    assert cat.is_live is False, "stale data must never claim to be current"


def test_a_corrupt_cache_never_raises(monkeypatch):
    mc.cache_path().parent.mkdir(parents=True, exist_ok=True)
    mc.cache_path().write_text("{ not json")
    _serve(monkeypatch, fail=True)
    assert mc.load_catalog().models  # degrades to the bundled fallback


def test_garbage_entries_are_skipped_not_fatal(monkeypatch):
    _serve(monkeypatch, payload={"data": [{"no_id": True}, _OPENROUTER["data"][0]]})
    cat = mc.load_catalog()
    assert [m.id for m in cat.models] == ["anthropic/claude-sonnet-5"]


def test_freshness_renders_for_a_human():
    assert "just now" in mc.describe_age(2).lower()
    assert "h" in mc.describe_age(7200) or "hour" in mc.describe_age(7200)
    assert "d" in mc.describe_age(86400 * 3) or "day" in mc.describe_age(86400 * 3)


def test_the_catalog_carries_each_model_s_capability_score_for_the_pickers():
    """The panel's model picker reads this catalog, so the score has to travel on it — otherwise
    a picker row can only show a name, and the choice cannot be a comparison."""
    from interact.model_catalog import ModelInfo, _with_scores
    from interact.models import Model

    with catalog_of(
        Model(id="known", provider="anthropic", capabilities=set(), intelligence_score=42.0),
    ):
        rows = _with_scores([ModelInfo(id="known"), ModelInfo(id="stranger")])
    assert [(r.id, r.intelligence_score) for r in rows] == [("known", 42.0), ("stranger", None)]


def test_live_leaderboard_scores_outrank_the_baked_snapshot(tmp_path, monkeypatch):
    """The picker labelled its number `aa.intelligence` — Artificial Analysis — and ranked on the
    snapshot BAKED into the package, while the live Artificial Analysis fetch sat on the same disk
    saying something else. One tab of the panel called a model first at 60.2 while the tab beside
    it had that model at 38.6 and a different leader. A displayed external fact reads from the live
    source when there is one; the baked value is the fallback, never the contradiction."""
    from interact.model_catalog import live_scores

    board = tmp_path / "benchmark_scores.json"
    board.write_text(json.dumps({"source": "artificial_analysis", "fetched_at": 1.0, "scores": [
        {"name": "GPT-5.5 (xhigh)", "intelligence": 38.6},
        {"name": "GPT-5.5 (low)", "intelligence": 30.7},
        {"name": "Claude Fable 5.1 (Adaptive Reasoning, Max Effort)", "intelligence": 53.4},
        {"name": "Claude Opus 4.7", "intelligence": 44.0},
        {"name": "", "intelligence": 99.0},
    ]}))
    scores = live_scores(board)
    # A model's own name, however the board dresses the effort level, and its BEST measured effort.
    assert scores["gpt-5-5"] == 38.6, "the strongest effort speaks for the model"
    assert scores["claude-fable-5-1"] == 53.4
    assert scores["claude-opus-4-7"] == 44.0
    assert "" not in scores
    assert live_scores(tmp_path / "absent.json") == {}, "no board is not an empty board"


def test_catalog_scores_follow_the_live_board_not_the_shipped_snapshot(tmp_path, monkeypatch):
    """End to end: a model the live board measures takes THAT number, whatever the package baked.
    Ranking on the snapshot is how the picker came to call a model first at 60.2 while the panel's
    own Benchmarks tab, reading the live fetch, had it at 38.6 behind a different leader."""
    from interact import model_catalog as mcat
    from interact.models import Model

    board = tmp_path / "benchmark_scores.json"
    board.write_text(json.dumps({"scores": [
        {"name": "GPT-5.5 (xhigh)", "intelligence": 38.6},
        {"name": "Claude Fable 5.1 (Max Effort)", "intelligence": 53.4},
    ]}))
    monkeypatch.setattr(mcat, "leaderboard_path", lambda: board)
    fresh = Model.rescored([
        Model(id="gpt-5.5", provider="openai", capabilities=set(), intelligence_score=60.2),
        Model(id="azure/claude-fable-5.1", provider="azure", capabilities=set()),
        Model(id="only-baked", provider="x", capabilities=set(), intelligence_score=11.0),
    ])
    by_id = {m.id: m.intelligence_score for m in fresh}
    assert by_id["gpt-5.5"] == 38.6, "the live board overrides a stale baked number"
    assert by_id["azure/claude-fable-5.1"] == 53.4, "a reseller's id still meets the board's name"
    assert by_id["only-baked"] == 11.0, (
        "the registry keeps what it knows — selection and criteria need it; it is the printed "
        "RANKING that is scoped to one vintage, in `agents models`"
    )


def test_the_id_normalizer_is_held_to_the_table_its_typescript_twin_reads():
    """`bare_model_name` and the client's `bareModelName` are one rule in two languages.

    They were two hand-kept copies of the same pairs, and they drifted: the suffix strippers ran
    in a FIXED ORDER, so a date sitting behind a hosting tag (`...-20250929-v1:0`) was never
    reached — 74 real litellm ids kept their date, which made every Bedrock-style id invisible to
    the score lookup and to the merge that fills the catalog. Both suites now read THIS file, so a
    pair can only be changed for both at once.
    """
    from interact.model_catalog import bare_model_name

    table = json.loads((Path(__file__).parent / "data" / "bare_model_names.json").read_text())
    for model_id, expected in table["pairs"].items():
        assert bare_model_name(model_id) == expected, model_id
    for left, right in table["distinct"]:
        assert bare_model_name(left) != bare_model_name(right), (
            f"{left} and {right} are different models and must not collapse together"
        )


def test_the_board_is_keyed_the_same_way_it_is_looked_up(tmp_path, monkeypatch):
    """One join, one normalizer — or the join silently does not happen.

    The board index was built with `_leaderboard_key` and read with `bare_model_name`, and the two
    disagree about a maturity word: "Gemini 3.1 Pro Preview" became `gemini-3-1-pro-preview` while
    the model id `gemini/gemini-3.1-pro-preview` became `gemini-3-1-pro`. They never met, so that
    model kept the SNAPSHOT's stale 57.2 while the live board said 30.4 — and a criterion asking
    for the top of the board picked it on a number no board has printed since.
    """
    board = tmp_path / "board.json"
    board.write_text(json.dumps({"scores": [
        {"name": "Gemini 3.1 Pro Preview", "intelligence": 30.4},
        {"name": "Claude 4.5 Haiku", "intelligence": 17.6},
        {"name": "Claude Opus 5 (Adaptive Reasoning, Max Effort)", "intelligence": 50.7},
    ]}))
    monkeypatch.setattr(mc, "leaderboard_path", lambda: board)
    live = mc.live_scores()
    for model_id, expected in [("gemini/gemini-3.1-pro-preview", 30.4),
                               ("anthropic/claude-opus-5[1m]", 50.7)]:
        assert live.get(mc.bare_model_name(model_id)) == expected, model_id


def test_the_board_is_read_once_per_version_not_once_per_question(tmp_path, monkeypatch):
    """A percentile bar asks the board whether it measured EACH candidate, so a re-read per model
    turned one `explain()` into 19 seconds — inside a picker that validates as you type, behind a
    30-second CLI timeout. Read once per version of the file, and notice when it changes."""
    board = tmp_path / "board.json"
    board.write_text(json.dumps({"scores": [{"name": "One", "intelligence": 10.0}]}))
    monkeypatch.setattr(mc, "leaderboard_path", lambda: board)
    mc._BOARD_MEMO.clear()

    reads = {"n": 0}
    real = Path.read_text

    def counted(self, *a, **k):
        if self == board:
            reads["n"] += 1
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counted)
    for _ in range(50):
        assert mc.live_scores()["one"] == 10.0
    assert reads["n"] == 1, f"the same board was parsed {reads['n']} times"

    # A CHANGED board is a different version and must be picked up, never served from the memo.
    import os
    board.write_text(json.dumps({"scores": [{"name": "One", "intelligence": 42.0}]}))
    os.utime(board, (0, 0))
    assert mc.live_scores()["one"] == 42.0, "an edited board is re-read"
