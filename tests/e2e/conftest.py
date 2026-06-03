"""E2E conftest — provider fixture injection, graceful skip orchestration."""

import os

import pytest

from interact.models import Model

from .harness import (
    ResultCollector,
    cheapest_grounding_model,
    ensure_registry_loaded,
    grounding_models,
    grounding_providers,
)


def pytest_addoption(parser):
    parser.addoption(
        "--provider",
        action="store",
        default=None,
        help="Run e2e tests for a specific provider only (e.g. --provider openai)",
    )
    parser.addoption(
        "--all-providers",
        action="store_true",
        default=False,
        help="Run against ALL available providers (default: openai only)",
    )


@pytest.fixture(scope="session", autouse=True)
def _load_registry():
    ensure_registry_loaded()


@pytest.fixture(scope="session")
def results() -> ResultCollector:
    collector = ResultCollector()
    yield collector
    collector.print_summary()


@pytest.fixture(scope="session")
def providers(request) -> list[str]:
    """Provider names with at least one available grounding model, filtered by CLI."""
    specific = request.config.getoption("--provider")
    all_mode = request.config.getoption("--all-providers")

    available = grounding_providers()

    if specific:
        if specific in available:
            return [specific]
        # Distinguish "provider unknown" from "provider known but key missing".
        all_known = {m.provider for m in Model.registry()}
        if specific in all_known:
            pytest.skip(f"Provider '{specific}' exists but no API key set")
        pytest.skip(f"Provider '{specific}' not found in models.json")

    if all_mode:
        if not available:
            pytest.skip("No providers available (no API keys configured)")
        return available

    # Default: openai only
    if "openai" in available:
        return ["openai"]
    if os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY set but no openai grounding model available")
    pytest.skip("OPENAI_API_KEY not set (default provider)")


@pytest.fixture
def models_for_provider() -> "callable":
    """Helper: pick the cheapest grounding model for a provider name."""
    return cheapest_grounding_model


@pytest.fixture
def all_grounding_models() -> "callable":
    """Helper: list all available grounding models for a provider."""
    return grounding_models
