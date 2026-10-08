"""Register a run for the registry and messaging test suites.

`test_agent_registry.py` and `test_agent_messaging.py` each carried their own `_record` factory
around `galaius.agents.registry.register`, with drifted defaults (the registry suite cared about
`pid`/`cwd`, the messaging suite about `pid`/`agent`/`provider_session_id`) but the same shape.
One typed factory covers both call sites; a caller only names what its scenario needs.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from galaius.agents import registry as reg
from galaius.agents import run as _run_module
from galaius.agents.policy import Policy
from galaius.agents.providers import AgentProvider, ClaudeCodeProvider

#: A real init+result stream, the shape every scripted provider below emits. A subclass with its
#: own payload (cost, extra fields) sets its own `script`; this is just the common default.
SUCCESS_SCRIPT = (
    'import json\n'
    'print(json.dumps({"type":"system","subtype":"init","session_id":"SID"}), flush=True)\n'
    'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
    '"usage":{"output_tokens":1},"session_id":"SID"}), flush=True)\n'
)


class ScriptedProvider(AgentProvider):
    """An `AgentProvider` that spawns a real subprocess printing a scripted JSON stream, parsed by
    the real `ClaudeCodeProvider` dialect — exercising the whole spawn/stream/parse path without a
    vendor binary or a token. Four test files each rebuilt this (`_FakeProvider`, `_Provider`,
    `_Cli`, `_DeliveryProvider`) with the same `command`/`parse`/`available` bodies; a subclass
    here only names what actually differs — its `name`, its `script`, or an overridden
    `command`/`resume_command` for a scenario that doesn't just run the script directly."""

    binary = sys.executable
    script = SUCCESS_SCRIPT

    def available(self) -> bool:
        return True

    def command(self, task, *, cwd, model, mcp_config, run_id, agent=None,
                permission_mode=None, allowed_tools=None, reasoning=None, image_paths=(),
                coarse_accepted=False, base_url=None, **_ignored):
        return [sys.executable, "-c", self.script]

    def parse(self, line, ledger=None):
        return ClaudeCodeProvider().parse(line, ledger)


#: npm's own launcher for a `#!/usr/bin/env node` bin (cmd-shim 7.0.0, generated, never hand-written).
NPM_SHIM = Path(__file__).parent.parent / "fixtures" / "agents" / "npm" / "codex.cmd"


def install_fake_cli(directory: Path, name: str, script: Path) -> Path:
    """Put the Python program `script` in `directory` as the command `name`, installed the way that
    system installs a vendor CLI: POSIX an executable file run by its shebang; Windows beside it the
    `.cmd` launcher npm writes (here for this interpreter as the bin's `node`), which is what `codex` /
    `claude` are there. Returns the program to start by path; by bare name `directory` must lead PATH."""
    program = directory / name
    shutil.copy2(script, program)
    if sys.platform == "win32":
        shim = NPM_SHIM.read_bytes().replace(rb"node_modules\@openai\codex\bin\codex.js", name.encode()).replace(b"node", b"python")
        launcher = directory / f"{name}.cmd"
        launcher.write_bytes(shim.replace(b'SET "_prog=python"', f'SET "_prog={sys.executable}"'.encode()))
        return launcher
    program.chmod(0o755)
    return program


def install_provider(monkeypatch, provider: AgentProvider) -> None:
    """Register `provider` into the live `PROVIDERS` registry for the test's duration. A run's
    raw stream is parsed by the provider named on its record, looked up in that global registry,
    so a test double has to be registered exactly like a real provider is. Four files reached the
    module via `__import__("galaius.agents.providers", fromlist=["PROVIDERS"]).PROVIDERS` to
    dodge a stale-binding import; `monkeypatch.setitem` on the module's own dict never goes stale,
    so a plain import serves every call site."""
    from galaius.agents.providers import PROVIDERS

    monkeypatch.setitem(PROVIDERS, provider.name, provider)


def use_policy(monkeypatch, *targets, **fields) -> Policy:
    """Patch `load_policy` to return `Policy(**fields)`, in every module in `targets` (the
    `galaius.agents.run` module that defines it by default; pass `galaius.agents.messaging` too
    when the exercised code path reads its own separately-imported name — patching one binding
    never moves the other). Eight files rebuilt the same `lambda: Policy(...)` stub; this covers
    the static single-value case every one of them actually needed."""
    policy = Policy(**fields)
    for target in targets or (_run_module,):
        monkeypatch.setattr(target, "load_policy", lambda: policy)
    return policy


def register_run(
    run_id: str = "r1",
    *,
    pid: int | None = 1,
    provider: str = "claude",
    name: str = "tester",
    task: str = "do it",
    cwd: str = tempfile.gettempdir(),
    agent: str | None = None,
    provider_session_id: str | None = None,
    **rest,
) -> reg.AgentRun:
    return reg.register(
        run_id=run_id, pid=pid, provider=provider, name=name, task=task, cwd=cwd,
        agent=agent, provider_session_id=provider_session_id, **rest,
    )
