"""Register a run for the registry and messaging test suites.

`agents/test_agent_registry.py` and `agents/test_agent_messaging.py` each carried their own `_record` factory
around `galaius.agents.registry.register`, with drifted defaults (the registry suite cared about
`pid`/`cwd`, the messaging suite about `pid`/`agent`/`provider_session_id`) but the same shape.
One typed factory covers both call sites; a caller only names what its scenario needs.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import EllipsisType

from galaius.agents import messaging
from galaius.agents import registry as reg
from galaius.agents import agent_queue
from galaius.agents import run as _run_module
from galaius.agents.policy import Policy
from galaius.agents.providers import AgentProvider, ClaudeCodeProvider
from galaius.agents.catalog.relations import CARD_HEADING
from tests.support.signals import alive_pid

def said(prompt: str) -> str:
    """What a turn's prompt says past the turn card (`relations.card`) it opens with, for a test judging the message."""
    return prompt.split("\n\n", 1)[1] if prompt.startswith(CARD_HEADING) else prompt


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


def claude_definitions(argv: list[str]) -> dict[str, dict]:
    """The role definitions a Claude command line hands its CLI (`--agents`): inline JSON, or the file it names (on
    Windows always, `ClaudeCodeProvider.agents_argument`)."""
    value = argv[argv.index("--agents") + 1]
    return json.loads(value if value.lstrip().startswith("{") else Path(value).read_text(encoding="utf-8"))


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
    pid: int | None | EllipsisType = ...,
    provider: str = "claude",
    name: str = "tester",
    task: str = "do it",
    cwd: str = tempfile.gettempdir(),
    agent: str | None = None,
    provider_session_id: str | None = None,
    **rest,
) -> reg.AgentRun:
    """A run record; its process, unless `pid` names one, a running child of this test process (`signals.alive_pid`):
    a stop of it ends that child alone (2026-11-08: a default pid 1 made a stop `kill(-1)`, every process of the user)."""
    return reg.register(
        run_id=run_id, pid=alive_pid() if pid is ... else pid, provider=provider, name=name, task=task, cwd=cwd,
        agent=agent, provider_session_id=provider_session_id, **rest,
    )


class StreamProvider(ScriptedProvider):
    """Emits two events then exits — the shape of a real stream, none of the cost."""

    name = "fake"
    script = (
        'import json,sys\n'
        'print(json.dumps({"type":"system","subtype":"init","cwd":"/tmp","tools":[],'
        '"session_id":"SID"}), flush=True)\n'
        'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
        '"total_cost_usd":0.5,"usage":{"output_tokens":7},"session_id":"SID"}), flush=True)\n'
    )


class QueueProvider(ScriptedProvider):
    """Resumable provider whose resumed turn prints one reply and exits; records each message."""

    name = "queue-fake"
    can_resume = True

    def __init__(self, script: str | None = None):
        self.script = script or (
            "import json; "
            "print(json.dumps({'type':'assistant','session_id':'vendor',"
            "'message':{'content':[{'type':'text','text':'reply'}]}})); "
            "print(json.dumps({'type':'result','is_error':False,'session_id':'vendor'}))"
        )
        self.messages: list[str] = []

    def command(self, *args, **kwargs):
        return [sys.executable, "-c", "pass"]

    def resume_command(self, session_id, message, **kwargs):
        self.messages.append(said(message))
        return [sys.executable, "-c", self.script]

    def valid_definition(self, agent):
        return True

    def permission_modes(self):
        return []


def setup_queue(monkeypatch, provider=None):
    """Run `r1` registered against `provider`, the queue dispatcher stubbed to in-process delivery."""
    provider = provider or QueueProvider()
    install_provider(monkeypatch, provider)
    monkeypatch.setattr(messaging, "provider_for", lambda _: provider)
    use_policy(monkeypatch, messaging, agents={"tester": "queue-model"}, providers={provider.name: True})
    # Delivery tests run the same dispatcher entry point in-process. The child process itself is
    # still real: launch_continuation uses Popen and its reaper owns waitpid/finish.
    monkeypatch.setattr(agent_queue, "ensure_dispatcher_locked", lambda *args, **kwargs: 1)
    monkeypatch.setattr(agent_queue, "RESUME_BACKOFF", (0.01, 0.02))
    reg.register(
        run_id="r1", pid=999999, provider=provider.name, name="worker", task="t",
        agent="tester", provider_session_id="vendor",
    )
    return provider
