"""A build already installed hands the active runtime module names it holds in its own code: its
upgrade probe imports some from each release it is offered, and once a newer runtime is active its
`-m` launches (queue dispatcher) and the hooks it wrote start others on that runtime. Each name below
is a contract with every shipped build: moved, those computers refuse the release or fail at start."""

import importlib

import pytest

#: Probe list, `-m` targets and hook commands of every build shipped up to 2026-10-31.
SHIPPED = (
    "galaius",
    "galaius_core",
    "galaius.cli.app",
    "galaius.mcp_relay",
    "galaius.server.shared",
    "galaius.agents.agent_queue",
    "galaius.agents.spawn_line",
    "galaius.inbox_hook",
    "galaius.brief_hook",
)


@pytest.mark.parametrize("name", SHIPPED)
def test_a_module_a_shipped_build_names_imports(name: str) -> None:
    importlib.import_module(name)
