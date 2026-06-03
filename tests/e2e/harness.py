"""E2E harness — test-only result types + helpers.

Provider/Model discovery lives in :mod:`interact.models`. This module
deliberately holds nothing that duplicates a domain class from ``src/``.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel

from interact.models import Model, ModelCapability
from interact.probe import Comparison, MatchedElement, UnmatchedElement

load_dotenv()

_SESSION_TS = datetime.now().strftime("%Y%m%d_%H%M%S")
# One dated run folder groups every test type: out/tests/{session}/e2e/
OUT_DIR = Path("out") / "tests" / _SESSION_TS / "e2e"

__all__ = ["Comparison", "MatchedElement", "UnmatchedElement"]


def ensure_registry_loaded() -> None:
    """Populate the model registry from the catalog bundled in :mod:`interact.data`.

    ``Model.load_registry`` already resolves ``INTERACT_MODELS_JSON`` (set by hosts)
    before falling back to the bundled ``models.json``, so tests need no path of their own.
    """
    if Model.registry():
        return
    Model.load_registry()


def grounding_models(provider: str | None = None) -> list[Model]:
    """Available models that can do GUI grounding, optionally filtered by provider.

    Filtering / sorting is delegated to :class:`Model` — no re-implementation here.
    """
    ensure_registry_loaded()
    models = Model.by_capability(ModelCapability.GUI_GROUNDING, available_only=True)
    if provider:
        models = [m for m in models if m.provider == provider]
    return models


def grounding_providers() -> list[str]:
    """Unique provider names with at least one available grounding model."""
    return sorted({m.provider for m in grounding_models()})


def cheapest_grounding_model(provider: str) -> Model | None:
    models = grounding_models(provider)
    return models[0] if models else None


# ─── Test Result Tracking ─────────────────────────────────────────────────────


class E2EResult(BaseModel):
    provider: str
    model: str
    test_name: str
    passed: bool
    skipped: bool = False
    skip_reason: str = ""
    elapsed: float = 0.0
    cost_estimate: float = 0.0
    details: dict[str, Any] = {}
    artifacts: list[str] = []

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "test_name": self.test_name,
            "passed": self.passed,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "elapsed": round(self.elapsed, 2),
            "cost_estimate": self.cost_estimate,
            "details": self.details,
            "artifacts": self.artifacts,
            "timestamp": datetime.now().isoformat(),
        }


class ResultCollector:
    """Accumulates test results, writes to out/tests/{session}/e2e/results.json."""

    def __init__(self):
        self.results: list[E2EResult] = []
        OUT_DIR.mkdir(parents=True, exist_ok=True)

    def add(self, result: E2EResult):
        self.results.append(result)
        self._persist()

    def _persist(self):
        path = OUT_DIR / "results.json"
        path.write_text(json.dumps([r.to_dict() for r in self.results], indent=2))

    def print_summary(self):
        if not self.results:
            print("\n  No results collected.\n")
            return

        print(f"\n{'═' * 100}")
        print(
            f"  {'Provider':<15} {'Model':<35} {'Test':<25} {'Time':<8} {'Cost':<10} {'Status'}"
        )
        print(f"{'─' * 100}")
        for r in self.results:
            if r.skipped:
                status = f"⊘ SKIP ({r.skip_reason[:30]})"
            elif r.passed:
                status = "✓ PASS"
            else:
                status = "✗ FAIL"
            print(
                f"  {r.provider:<15} {r.model:<35} {r.test_name:<25} "
                f"{r.elapsed:<8.1f} ${r.cost_estimate:<9.5f} {status}"
            )

        passed = sum(1 for r in self.results if r.passed)
        failed = sum(1 for r in self.results if not r.passed and not r.skipped)
        skipped = sum(1 for r in self.results if r.skipped)
        total_cost = sum(r.cost_estimate for r in self.results)
        print(f"{'─' * 100}")
        print(
            f"  Total: {passed} passed, {failed} failed, {skipped} skipped | Cost: ${total_cost:.5f}"
        )
        print(f"{'═' * 100}\n")


# ─── Graceful Skip Helpers ────────────────────────────────────────────────────

_BUDGET_ERRORS = ("rate_limit", "quota", "budget", "insufficient_quota", "billing")
_NOT_FOUND_ERRORS = ("not_found", "does not exist", "not available", "not enabled")


def is_budget_or_rate_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _BUDGET_ERRORS)


def is_model_not_found(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _NOT_FOUND_ERRORS)


def should_skip(exc: Exception) -> str | None:
    if is_budget_or_rate_error(exc):
        return f"budget/rate: {type(exc).__name__}"
    if is_model_not_found(exc):
        return f"model unavailable: {type(exc).__name__}"
    return None


# ─── Output Helpers ───────────────────────────────────────────────────────────


def _sanitize(component: str) -> str:
    """Filesystem-safe name fragment (preserves model ids like ``openai/gpt-4o``)."""
    return component.replace("/", "_").replace(":", "_")


def make_output_dir(provider: str, model_id: str, test_name: str) -> Path:
    """Create ``out/tests/{session_ts}/e2e/{provider}/{HHMMSS}_{test_name}/`` and return it."""
    ts = datetime.now().strftime("%H%M%S")
    d = OUT_DIR / _sanitize(provider) / f"{ts}_{test_name}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_artifact(
    out_dir: Path, name: str, data: str | bytes, ext: str = "txt"
) -> Path:
    fname = f"{name}.{ext}"
    path = out_dir / fname
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)
    return path
