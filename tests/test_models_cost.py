"""Direct tests for the cost-handling free functions in models.py."""

from __future__ import annotations

import pytest

from interact.models import Model


@pytest.mark.parametrize(
    "input_cost, output_cost, expected",
    [
        (1.0, 2.0, 3.0),
        (None, 5.0, 5.0),
        (5.0, None, 5.0),
        (None, None, 0.0),
        (0.0, 0.0, 0.0),
    ],
)
def test_cost_score(input_cost, output_cost, expected):
    assert Model.cost_of(input_cost, output_cost) == expected


@pytest.mark.parametrize(
    "score, cost, expected",
    [
        (0.5, 2.0, 0.25),
        (0.5, 0.0, None),
        (0.5, None, None),
        (0.0, 1.0, 0.0),
    ],
)
def test_quality_per_dollar(score, cost, expected):
    assert Model.quality_per_dollar(score, cost) == expected


def test_model_cost_score_uses_helper():
    """Model.cost_score delegates to _cost_score."""
    from interact.models import Model, ModelCapability

    m = Model(
        id="x/y",
        provider="x",
        capabilities={ModelCapability.VLM},
        input_cost_per_million=1.0,
        output_cost_per_million=3.0,
    )
    assert m.cost_score == Model.cost_of(1.0, 3.0) == 4.0


def test_recommendation_quality_per_dollar_uses_helper():
    from interact.models import (
        Benchmark,
        BenchmarkRecommendation,
        Model,
        ModelCapability,
    )

    m = Model(
        id="x/y",
        provider="x",
        capabilities={ModelCapability.VLM},
        input_cost_per_million=2.0,
        output_cost_per_million=2.0,
    )
    bench = Benchmark(id="bx", name="b", description="b")
    rec = BenchmarkRecommendation(
        benchmark=bench, model=m, source="published", rank=1, score=0.8
    )
    assert rec.quality_per_dollar == Model.quality_per_dollar(0.8, 4.0) == 0.2

    zero = Model(id="z/z", provider="z", capabilities={ModelCapability.VLM})
    rec2 = BenchmarkRecommendation(
        benchmark=bench, model=zero, source="published", rank=1, score=0.5
    )
    assert rec2.quality_per_dollar is None
