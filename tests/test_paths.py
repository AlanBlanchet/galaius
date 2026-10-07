"""The extension's usage-log resolver must agree with the Python writer.

The dashboard is a SEPARATE process from the MCP server: Python appends to
``Config.usage_log`` and the VS Code panel reads a path it computes itself. Nothing but this
test binds the two, and when they drifted (8fb56b1 moved the writer from ``<debug_dir>/logs/``
to ``<debug_dir>/``) the panel silently charted a file nobody writes. So: run the extension's
OWN resolver (``clients/vscode/src/paths.ts``, via node) and assert it lands on exactly the
file Python writes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from galaius.config import Config

PATHS_TS = Path(__file__).resolve().parents[1] / "clients" / "vscode" / "src" / "paths.ts"
AGENT_MODELS_TS = PATHS_TS.with_name("agentModels.ts")


def _node_can_strip_types() -> bool:
    """Node >= 22.6 runs TypeScript directly; older nodes can't and the test is skipped."""
    if shutil.which("node") is None:
        return False
    probe = subprocess.run(
        ["node", "--experimental-strip-types", "-e", "const x: number = 1; console.log(x)"],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


_NODE_OK = _node_can_strip_types()


@pytest.fixture(autouse=True)
def _node_required():
    """Skipping locally is fine; skipping in CI would make this whole file decorative — the
    cross-language contract would go unchecked exactly where it matters. So CI fails instead."""
    if _NODE_OK:
        return
    if os.environ.get("CI"):
        pytest.fail("node >= 22.6 is required in CI — the path-parity test must not skip here")
    pytest.skip("needs node >= 22.6 (--experimental-strip-types)")


def _extension_usage_log(base_dir: str, tmp_path: Path) -> Path:
    """Where the EXTENSION thinks the usage log is, for a given ``galaius.debug.dir`` value."""
    runner = tmp_path / "resolve.ts"
    runner.write_text(
        f'import {{ usageLogPathFor }} from {json.dumps(PATHS_TS.as_uri())};\n'
        "console.log(usageLogPathFor(process.argv[2] ?? ''));\n"
    )
    out = subprocess.run(
        ["node", "--experimental-strip-types", str(runner), base_dir],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip())


def test_extension_default_matches_python_default(monkeypatch, tmp_path):
    """With no `galaius.debug.dir` set, the panel must read the file Python writes by default."""
    monkeypatch.delenv("GALAIUS_DEBUG_DIR", raising=False)
    assert _extension_usage_log("", tmp_path) == Config().usage_log


@pytest.mark.parametrize("base", ["/tmp/galaius-out", "~/.galaius/out", "~/proj/out"])
def test_extension_matches_python_for_configured_base(monkeypatch, tmp_path, base):
    """A configured base dir must resolve identically on both sides — tilde included."""
    monkeypatch.setenv("GALAIUS_DEBUG_DIR", base)
    assert _extension_usage_log(base, tmp_path) == Config().usage_log


def test_written_record_is_visible_to_the_reader(monkeypatch, tmp_path):
    """End to end: a record written through the real writer is found where the panel looks."""
    import litellm

    from galaius import runtime
    from galaius.vision.usage import log_api_attempt

    base = tmp_path / "out"
    monkeypatch.setenv("GALAIUS_DEBUG_DIR", str(base))
    monkeypatch.setattr(runtime, "config", Config())
    monkeypatch.setattr(litellm, "completion_cost", lambda **_: 0.0012)

    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))
    log_api_attempt("gemini/gemini-3.5-flash", "gemini", response)

    written = runtime.config.usage_log
    assert written.exists(), "the writer wrote nothing — test setup is wrong, not the reader"

    seen = _extension_usage_log(str(base), tmp_path)
    assert seen == written, f"writer wrote {written}, reader looks at {seen}"
    assert json.loads(seen.read_text().strip())["model"] == "gemini/gemini-3.5-flash"


def _extension_agents_dir(tmp_path: Path) -> Path:
    """Where the EXTENSION thinks the agent registry lives."""
    runner = tmp_path / "resolve_agents.ts"
    runner.write_text(
        f'import {{ agentsDir }} from {json.dumps(PATHS_TS.as_uri())};\n'
        "console.log(agentsDir());\n"
    )
    out = subprocess.run(
        ["node", "--experimental-strip-types", str(runner)],
        capture_output=True, text=True, check=True,
    )
    return Path(out.stdout.strip())


def test_the_agents_registry_dir_matches_python(monkeypatch, tmp_path):
    """The supervisor is cross-process IPC — the CLI writes run records, the extension reads them.
    A disagreement means the panel watches a directory nothing writes: the metering bug of 0ef5fa4
    one level up. Note both sides pin it OUTSIDE debug_dir deliberately."""
    from galaius.agents.registry import agents_dir

    monkeypatch.delenv("GALAIUS_AGENTS_DIR", raising=False)
    assert _extension_agents_dir(tmp_path) == agents_dir()


def test_the_agents_registry_ignores_a_debug_dir_override(monkeypatch, tmp_path):
    """Setting GALAIUS_DEBUG_DIR must NOT move the registry — a process that never saw the
    override still has to find it."""
    from galaius.agents.registry import agents_dir

    monkeypatch.delenv("GALAIUS_AGENTS_DIR", raising=False)
    monkeypatch.setenv("GALAIUS_DEBUG_DIR", str(tmp_path / "elsewhere"))
    assert _extension_agents_dir(tmp_path) == agents_dir()
    assert "elsewhere" not in str(agents_dir())


def test_the_agents_registry_uses_the_harness_isolation_path(monkeypatch, tmp_path):
    """A launched extension and its Python child must share the harness-owned store."""
    from galaius.agents import registry

    isolated = tmp_path / "harness" / "agents"
    monkeypatch.setenv("GALAIUS_AGENTS_DIR", str(isolated))

    assert registry.agents_dir() == isolated
    assert _extension_agents_dir(tmp_path) == isolated
    registry.register(
        run_id="isolated-harness-run",
        pid=None,
        provider="codex",
        name="test harness",
        model="openai/example-model",
    )
    assert (isolated / "isolated-harness-run.json").is_file()
    assert not (tmp_path / ".galaius" / "out" / "agents" / "isolated-harness-run.json").exists()


def _extension_policy_path(tmp_path: Path) -> Path:
    """Where the EXTENSION thinks the agents policy is."""
    runner = tmp_path / "policy.ts"
    runner.write_text(
        f'import {{ agentsPolicyPath }} from {json.dumps(AGENT_MODELS_TS.as_uri())};\n'
        "console.log(agentsPolicyPath());\n"
    )
    out = subprocess.run(
        ["node", "--experimental-strip-types", str(runner)], capture_output=True, text=True, check=True
    )
    return Path(out.stdout.strip())


@pytest.mark.parametrize("debug_dir", [None, "/tmp/galaius-out", "~/proj/out"])
def test_the_agents_policy_is_one_file_for_both_sides(monkeypatch, tmp_path, debug_dir):
    """The panel WRITES an agent's model choice into the policy and the spawn READS it — a
    different answer on either side is a choice that silently never bites. It lives beside
    `config.env`, never under the debug dir: on a box that relocates its dumps
    (`GALAIUS_DEBUG_DIR`), the CLI looked for `<repo>/out/agents.json` while the panel wrote
    `~/.galaius/agents.json`."""
    from galaius.agents.policy import policy_path
    from galaius.config import UserConfig

    if debug_dir is None:
        monkeypatch.delenv("GALAIUS_DEBUG_DIR", raising=False)
    else:
        monkeypatch.setenv("GALAIUS_DEBUG_DIR", debug_dir)
    assert _extension_policy_path(tmp_path) == policy_path()
    assert policy_path().parent == UserConfig.PATH.parent
