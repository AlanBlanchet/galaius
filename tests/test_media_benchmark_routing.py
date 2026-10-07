"""A published benchmark table's state — refreshed, stale, non-authoritative, cache-expired —
routes which model actually gets dispatched for media analysis. The table is not decorative: a
model that no longer qualifies must never reach the provider, and a genuinely better model must
be picked up the moment its score changes.
"""

import json
from pathlib import Path

import pytest

import galaius.benchmark_tables as benchmark_tables
import galaius.vision.core as vision
from galaius.benchmarks.published import PublishedEntry, PublishedTable
from galaius.benchmarks.upstream import GroundingLeaderboardJS, UpstreamSource
from galaius.config import Config
from galaius.models import Benchmark, Model, ModelCapability
from galaius.vision import MediaItem
from galaius.vision.core import VLMResult, analyze_media
from tests.support import solid_png


@pytest.mark.asyncio
async def test_refreshed_published_table_changes_actual_media_dispatch(monkeypatch) -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/refreshed_published_table.json").read_text()
    )
    models = [
        Model(id=model_id, provider="openai", capabilities={ModelCapability.VLM})
        for model_id in ("openai/visual-a", "openai/visual-b")
    ]
    monkeypatch.setattr(Model, "catalog", lambda: models)
    monkeypatch.setattr(Model, "_registry", models)
    monkeypatch.setattr(Model, "is_available", lambda self: True)
    calls: list[str] = []

    async def api(
        media, context, config, prompt, max_tokens, response_format, model, _dispatch_state,
    ):
        calls.append(model)
        return VLMResult(text="selected", elapsed=0, model=model, backend="api")

    monkeypatch.setattr(vision, "_api_media_completion", api)
    config = Config(
        media_backend="api", media_billing="api_allowed", image_criteria="cap.vlm",
        criteria_weights="aa.mmmu_pro=1",
    )
    for revision in ("before", "after"):
        table = PublishedTable.model_validate(fixture[revision])
        monkeypatch.setattr(
            "galaius.criteria.benchmark_tables.load_tables", lambda table=table: {"mmmu_pro": table},
        )
        await analyze_media([MediaItem.from_bytes(solid_png(12, 8, (0, 0, 128)))], "context", config, role="image")

    assert calls == ["openai/visual-a", "openai/visual-b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "stale", "approximate", "unverified"])
async def test_shared_non_authoritative_table_stops_before_media_dispatch(
    monkeypatch, state: str,
) -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/refreshed_published_table.json").read_text()
    )
    model = Model(id="openai/visual-a", provider="openai", capabilities={ModelCapability.VLM})
    monkeypatch.setattr(Model, "catalog", lambda: [model])
    monkeypatch.setattr(Model, "_registry", [model])
    monkeypatch.setattr(Model, "is_available", lambda self: True)
    table = (
        PublishedTable(
            source_url="https://example.test", retrieved="2026-09-06", freshness="current",
            entries=[PublishedEntry(model_name="visual-a", model_id=model.id, score=0.99)],
        )
        if state == "unverified"
        else PublishedTable.model_validate(fixture["excluded"][state])
    )
    monkeypatch.setattr(
        "galaius.criteria.benchmark_tables.load_tables", lambda: {"mmmu_pro": table},
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("non-authoritative score reached provider")

    monkeypatch.setattr(vision, "_api_media_completion", forbidden)
    with pytest.raises(RuntimeError, match="no model available|no candidate qualifies"):
        await analyze_media(
            [MediaItem.from_bytes(solid_png(12, 8, (0, 0, 128)))], "context",
            Config(media_backend="api", media_billing="api_allowed",
                   image_criteria="aa.mmmu_pro > 0.5"),
            role="image",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("producer_status", "dispatches"),
    [("approximate", False), ("unverified", False), ("eligible", True)],
)
async def test_source_authority_survives_cache_recommendation_and_media_dispatch(
    monkeypatch, producer_status: str, dispatches: bool,
) -> None:
    model = Model(id="openai/exact", provider="openai", capabilities={ModelCapability.VLM})
    monkeypatch.setattr(Model, "catalog", lambda: [model])
    monkeypatch.setattr(Model, "_registry", [model])
    monkeypatch.setattr(Model, "is_available", lambda self: True)
    source = GroundingLeaderboardJS(
        id="authority-test", name="authority test", url="https://example.test/table",
        benchmark_id="mmmu_pro",
    )
    produced = PublishedTable(
        source_url=source.url, retrieved="2026-09-06", freshness="current",
        entries=[PublishedEntry(model_name="exact", score=0.91, status=producer_status)],
    )
    monkeypatch.setattr(GroundingLeaderboardJS, "fetch", lambda self: produced)
    monkeypatch.setattr(UpstreamSource, "_registry", [source])

    loaded = benchmark_tables.load_tables(refresh=True)["mmmu_pro"]
    benchmark = Benchmark.by_id("mmmu_pro").model_copy(update={"published": loaded})
    assert bool(benchmark.recommend(available_only=False)) is dispatches
    calls: list[str] = []

    async def api(media, context, config, prompt, max_tokens, response_format, model, _dispatch_state):
        calls.append(model)
        return VLMResult(text="selected", elapsed=0, model=model, backend="api")

    monkeypatch.setattr(vision, "_api_media_completion", api)
    config = Config(media_backend="api", media_billing="api_allowed",
                    image_criteria="aa.mmmu_pro > 0.5")
    if dispatches:
        await analyze_media([MediaItem.from_bytes(solid_png(12, 8, (0, 0, 128)))], "context", config, role="image")
    else:
        with pytest.raises(RuntimeError, match="no model available|no candidate qualifies"):
            await analyze_media([MediaItem.from_bytes(solid_png(12, 8, (0, 0, 128)))], "context", config, role="image")
    assert bool(calls) is dispatches


@pytest.mark.asyncio
async def test_expired_cached_table_reloads_stale_and_stops_before_dispatch(monkeypatch) -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/refreshed_published_table.json").read_text()
    )
    model = Model(id="openai/visual-b", provider="openai", capabilities={ModelCapability.VLM})
    monkeypatch.setattr(Model, "catalog", lambda: [model])
    monkeypatch.setattr(Model, "_registry", [model])
    monkeypatch.setattr(Model, "is_available", lambda self: True)
    now = 2_000_000_000.0
    monkeypatch.setattr(benchmark_tables.time, "time", lambda: now)
    benchmark_tables.CACHE.write({
        "schema_version": 1,
        "fetched_at": now - benchmark_tables.TTL_SECONDS - 1,
        "tables": {"mmmu_pro": fixture["after"]},
    })

    def unavailable():
        raise RuntimeError("offline")

    monkeypatch.setattr("galaius.benchmarks.upstream.fetch_all", unavailable)
    loaded = benchmark_tables.load_tables()
    assert loaded["mmmu_pro"].freshness == "stale"

    async def forbidden(*args, **kwargs):
        raise AssertionError("expired score reached provider")

    monkeypatch.setattr(vision, "_api_media_completion", forbidden)
    with pytest.raises(RuntimeError, match="no model available|no candidate qualifies"):
        await analyze_media(
            [MediaItem.from_bytes(solid_png(12, 8, (0, 0, 128)))], "context",
            Config(media_backend="api", media_billing="api_allowed",
                   image_criteria="aa.mmmu_pro > 0.5"),
            role="image",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("weights", "expected"),
    [("aa.mmmu_pro=0.8,gui.screenspot=0.2", "openai/visual-a"),
     ("aa.mmmu_pro=0.2,gui.screenspot=0.8", "openai/visual-b")],
)
async def test_normalized_weights_flip_the_actual_configured_media_route(
    monkeypatch, weights: str, expected: str,
) -> None:
    first = Model(id="openai/visual-a", provider="openai", capabilities={ModelCapability.VLM})
    second = Model(id="openai/visual-b", provider="openai", capabilities={ModelCapability.VLM})
    scores = {"mmmu_pro": (0.9, 0.6), "screenspot": (0.5, 0.95)}
    monkeypatch.setattr(Model, "catalog", lambda: [first, second])
    monkeypatch.setattr(Model, "_registry", [first, second])
    monkeypatch.setattr(Model, "is_available", lambda self: True)
    monkeypatch.setattr(
        "galaius.criteria.benchmark_tables.load_tables",
        lambda: {
            benchmark_id: PublishedTable(
                source_url="https://example.test", retrieved="2026-09-06", freshness="current",
                entries=[PublishedEntry(model_name=first.id.split("/", 1)[1], model_id=first.id,
                                        score=values[0], status="eligible"),
                         PublishedEntry(model_name=second.id.split("/", 1)[1], model_id=second.id,
                                        score=values[1], status="eligible")],
            )
            for benchmark_id, values in scores.items()
        },
    )
    calls: list[str] = []

    async def api(
        media, context, config, prompt, max_tokens, response_format, model, _dispatch_state,
    ):
        calls.append(model)
        return VLMResult(text="selected", elapsed=0, model=model, backend="api")

    monkeypatch.setattr(vision, "_api_media_completion", api)
    await analyze_media(
        [MediaItem.from_bytes(solid_png(12, 8, (0, 0, 128)))], "context",
        Config(media_backend="api", media_billing="api_allowed", image_criteria="cap.vlm",
               criteria_weights=weights),
        role="image",
    )
    assert calls == [expected]
