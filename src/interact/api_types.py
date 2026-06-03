"""Public type aggregator for TypeScript codegen.

`pydantic2ts --module interact.api_types` walks this module and emits
the union of all Pydantic models referenced in ``__all__``. Anything not
listed here will NOT appear in the generated TS bindings.

Keep this file flat — no behavior, just re-exports.
"""

from __future__ import annotations

from interact.benchmarks.published import PublishedEntry, PublishedTable
from interact.benchmarks.upstream import UpstreamSource
from interact.formats import BoxOrder, CoordFormat
from interact.models import (
    Benchmark,
    BenchmarkRecommendation,
    Model,
    ModelCapability,
    ModelsConfig,
    ModelSpec,
    ProviderSpec,
)

__all__ = [
    "Benchmark",
    "BenchmarkRecommendation",
    "BoxOrder",
    "CoordFormat",
    "Model",
    "ModelCapability",
    "ModelSpec",
    "ModelsConfig",
    "ProviderSpec",
    "PublishedEntry",
    "PublishedTable",
    "UpstreamSource",
]
