"""Both live data sources — the model catalog (prices) and the benchmark board (scores) — are
written to a fixed cache path that the VS Code extension reads DIRECTLY. Each had a loader with
the same TTL semantics and, in production, NO CALLER: the cache files only ever existed because a
developer ran the loader by hand. The panel therefore showed data that could never refresh, and a
fresh install showed none at all.

They are two instances of one shape — a live external source behind a TTL cache a front end
reads — so they refresh through one path, and adding a third source must not need a new call site.
"""

import threading
import time

import pytest

import interact.live_sources as live
from interact.ttl_cache import RefreshFailed, TTLCache


def _source(name, load=lambda **_: None, filename=None):
    return live.LiveSource(name=name, noun="copy", load=load, cache=TTLCache(filename or f"{name}.json"))


def _rate_limited(**_):
    raise RefreshFailed("rate-limited by Vendor (HTTP 429)", retry_at=time.time() + 3600,
                        cached_at=time.time() - 8 * 3600)


def test_a_failing_source_is_reported_with_its_reason_and_never_stops_the_others():
    """Best-effort by construction — but a source that did not refresh is SAID, never counted:
    `interact refresh` once printed "Refreshed" while every call to the vendor answered 429."""
    outcomes = live.refresh_all([_source("broken", _rate_limited), _source("ok")])
    assert outcomes == {
        "broken": "rate-limited by Vendor (HTTP 429), retry in 1h, copy from 8h ago",
        "ok": None,
    }


def test_interact_refresh_prints_why_a_source_did_not_refresh(monkeypatch, capsys):
    from interact.cli.app_commands import refresh_live_data

    monkeypatch.setattr(live, "SOURCES", (_source("benchmark scores", _rate_limited), _source("model catalog")))
    with pytest.raises(SystemExit) as exited:
        refresh_live_data()
    assert exited.value.code == 1
    assert capsys.readouterr().out.splitlines() == [
        "Refreshed: model catalog",
        "benchmark scores NOT refreshed: rate-limited by Vendor (HTTP 429), retry in 1h, copy from 8h ago",
    ]


def test_the_real_sources_are_all_registered():
    """The regression this file exists for: a producer nobody calls. Every loader must be here."""
    assert {s.name for s in live.SOURCES} == {"model catalog", "benchmark scores", "benchmark tables"}


def test_the_refresher_fetches_only_what_is_due_and_retries_a_failure_before_the_ttl(monkeypatch):
    """One server runs per editor window; forcing every source at each startup spent the vendor's
    daily quota. A fresh cache is left alone; a failed source comes back after its retry window."""
    calls = []
    fresh = _source("fresh", lambda **_: calls.append("fresh"))
    fresh.cache.write({"fetched_at": time.time()})
    failing = _source("failing", lambda **_: calls.append("failing") or 1 / 0)
    stop, waits = threading.Event(), []
    monkeypatch.setattr(stop, "wait", lambda seconds: waits.append(seconds) or stop.set())
    live.refresh_when_due(stop, [fresh, failing])
    assert calls == ["failing"]
    assert waits and waits[0] == pytest.approx(live.FAILURE_RETRY_SECONDS, abs=5)


def test_a_vendor_retry_after_is_shared_through_the_cache(tmp_path):
    cache = TTLCache("vendor.json")
    cache.write({"fetched_at": time.time() - 10 * 24 * 3600})
    with pytest.raises(RefreshFailed):
        cache.refetch("Vendor", lambda: (_ for _ in ()).throw(
            RefreshFailed("rate-limited by Vendor (HTTP 429)", retry_at=time.time() + 600)))
    assert cache.due_at() == pytest.approx(time.time() + 600, abs=5)
    assert TTLCache("vendor.json").deferral() is not None, "another process must see the wait too"


def test_refreshing_actually_WRITES_every_cache(monkeypatch, tmp_path):
    """Asserting the registry's key names proves nothing — an entry can be present and still be a
    no-op (it was: `load_catalog` was `@lru_cache`d, so the refresher re-read memory and never
    refetched). The only evidence that counts is both cache files on disk, freshly stamped.
    """
    import interact.benchmark_source as bs
    import interact.benchmark_tables as bt
    import interact.model_catalog as mc

    monkeypatch.setattr(mc, "_from_openrouter", lambda payload: [mc.ModelInfo(id="x/y", name="Y")])
    monkeypatch.setattr(mc.httpx, "get", lambda *a, **k: _Response({"data": [{}]}))
    monkeypatch.setattr(bs, "_fetch", lambda: bs.Board(
        scores=[bs.Score(name="Y", creator="Z", intelligence=1.0)],
        source="artificial_analysis", fetched_at=time.time()))

    monkeypatch.setattr(
        "interact.benchmarks.upstream.fetch_all",
        lambda *a, **k: {"mmmu": _published_table()},
    )
    assert live.refresh_all() == {"model catalog": None, "benchmark scores": None, "benchmark tables": None}
    for path in (mc.cache_path(), bs.cache_path(), bt.cache_path()):
        assert path.exists(), f"{path.name} was never written — the producer did not run"


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_refresh_in_background_does_not_raise_when_a_source_fails(monkeypatch):
    monkeypatch.setattr(live, "SOURCES", (_source("broken", lambda **_: 1 / 0),))
    stop = threading.Event()
    thread = live.refresh_in_background(stop)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_a_cache_write_is_atomic_so_a_concurrent_reader_never_sees_a_half_file(tmp_path, monkeypatch):
    """Several interact servers run at once (one per editor window) and each refreshes on startup,
    so N writers share one path. A truncating write caught mid-flight leaves a corrupt file, and
    both readers swallow the parse error — the panel silently shows nothing."""
    import json

    import interact.model_catalog as mc

    mc._write_cache(mc.Catalog(models=[mc.ModelInfo(id="a/b")], source="openrouter", fetched_at=1.0))
    target = mc.cache_path()
    assert json.loads(target.read_text())["models"]
    # Nothing may be left beside it: a temp file that survives is a leak, and one that IS the
    # target path means the write was not atomic.
    strays = [p.name for p in target.parent.iterdir() if p.name != target.name]
    assert strays == [], f"write left temp files behind: {strays}"


def test_the_startup_refresh_can_be_turned_off():
    """It is the only outbound request a server makes on its own initiative, so it needs an
    off switch — an air-gapped or privacy-conscious install must be able to say no."""
    from interact.config import Config

    assert Config().refresh_live_data is True
    assert Config(refresh_live_data=False).refresh_live_data is False


def _published_table():
    from interact.benchmarks.published import PublishedEntry, PublishedTable

    return PublishedTable(source_url="https://example.test", retrieved="2026-08-18",
                          entries=[PublishedEntry(model_name="M", score=0.9)])
