"""Extension manifest must expose only ``interact.*`` keys."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

EXT_ROOT = Path(__file__).resolve().parents[1] / "vscode-extension"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((EXT_ROOT / "package.json").read_text())


@pytest.fixture(scope="module")
def extension_ts() -> str:
    return (EXT_ROOT / "src" / "extension.ts").read_text()


def test_commands_use_interact_namespace(manifest):
    cmds = {c["command"] for c in manifest["contributes"]["commands"]}
    assert "interact.openDashboard" in cmds
    assert "interact.selectModel" in cmds
    assert "interact.manageApiKeys" in cmds
    assert not any(c.startswith("interactMcp.") for c in cmds), cmds


def test_configuration_keys_use_interact_namespace(manifest):
    props = manifest["contributes"]["configuration"]["properties"]
    assert props, "expected configuration properties to be declared"
    for key in props:
        assert key.startswith("interact."), key
        assert not key.startswith("interactMcp."), key


def test_activates_on_startup(manifest):
    assert "onStartupFinished" in manifest.get("activationEvents", [])


@pytest.mark.parametrize(
    "needle",
    [
        "$(eye) Interact",
        "statusBar.show()",
        "interact.openDashboard",
    ],
)
def test_extension_ts_has_status_bar_wiring(extension_ts, needle):
    assert needle in extension_ts, f"missing {needle!r} in extension.ts"


_BENCH_RE = re.compile(r"bench|grounding|eval", re.I)


def test_no_benchmark_run_commands(manifest):
    """Benchmark/grounding runs are developer-only CLIs — never user-facing commands."""
    cmds = [c["command"] for c in manifest["contributes"]["commands"]]
    offenders = [c for c in cmds if _BENCH_RE.search(c)]
    assert not offenders, f"commands trigger paid evals: {offenders}"


def test_dashboard_has_no_run_grounding_bench():
    text = (EXT_ROOT / "src" / "dashboard.ts").read_text()
    assert "runGroundingBench" not in text
