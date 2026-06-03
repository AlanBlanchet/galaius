#!/usr/bin/env python3
"""Snapshot Benchmark.registry() to src/benchmarks.json for the VS Code extension.

Sibling of generate-models.py. Runs as part of `npm run compile`. Writes a
JSON document with each benchmark's metadata, published table, and any
measured snapshot loaded from grounding_results.json.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from interact.data import PackageData  # noqa: E402
from interact.models import Benchmark, Model  # noqa: E402

# Benchmark scores come from published online leaderboards; optional measured scores
# can be injected via INTERACT_GROUNDING_JSON (we never run our own paid eval).
Model.load_registry()

payload = []
for bench in Benchmark.registry():
    pub = bench.published
    lib_rec_model = bench.lib_recommendation_model()
    recs = bench.recommend(prefer="both", available_only=False, top_n=5)
    recommendations = [
        {
            "model_id": r.model.id,
            "score": r.score,
            "source": r.source,
            "rank": r.rank,
            "cost_per_million": r.cost_per_million,
            "quality_per_dollar": r.quality_per_dollar,
        }
        for r in recs
    ]
    payload.append(
        {
            "id": bench.id,
            "name": bench.name,
            "description": bench.description,
            "url": bench.url,
            "metric": bench.metric,
            "published": {
                "source_url": pub.source_url,
                "retrieved": pub.retrieved,
                "lib_recommendation": pub.lib_recommendation,
                "entries": [
                    {"model_name": e.model_name, "score": e.score} for e in pub.entries
                ],
            }
            if pub is not None
            else None,
            "lib_recommendation_model_id": lib_rec_model.id if lib_rec_model else None,
            "measured": bench.measured_scores(),
            "recommendations": recommendations,
        }
    )

out_path = PackageData.path(PackageData.BENCHMARKS)
out_path.write_text(json.dumps({"benchmarks": payload}, indent=2) + "\n")
print(f"Wrote {len(payload)} benchmarks to {out_path}")
