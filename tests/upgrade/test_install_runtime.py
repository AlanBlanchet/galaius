"""A test install (a runtime store of its own) never shares this computer's agent registry."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install_runtime.py"


def _script():
    spec = importlib.util.spec_from_file_location("install_runtime_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("environ", "refused"), [
    ({}, False),                                                                      # the developer's own store: the real one
    ({"GALAIUS_RUNTIMES": "/tmp/store"}, True),                                       # a test store in the real registry
    ({"GALAIUS_RUNTIMES": "/tmp/store", "GALAIUS_AGENTS_DIR": "/tmp/agents"}, False),  # a throwaway home
])
def test_a_test_store_needs_a_registry_of_its_own(environ, refused):
    assert (_script().registry_refusal(environ) is not None) is refused
