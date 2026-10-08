"""Spawning and supervising a run end to end, against a REAL subprocess.

The fake provider emits the same JSON dialect the captured Claude stream uses, so this exercises
the whole path — spawn, stream, normalise, persist, reap — without spending a token or depending
on a vendor binary being installed.
"""

import asyncio
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from galaius.agents import quota, registry as reg, run as run_module
from galaius.agents.events import AgentEvent
from galaius.agents.providers import ClaudeCodeProvider, CodexProvider
from galaius.agents.run import ChildLaunch, mesh_config, run_agent, validate_image_paths
from galaius.agents.warm import WarmStart
from tests.support.agents import ScriptedProvider, install_provider, use_policy


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    use_policy(monkeypatch, agents={"tester": "fixture-model"}, reasoning={"tester": "medium"})
    install_provider(monkeypatch, _FakeProvider())
    install_provider(monkeypatch, _CrashingProvider())
    install_provider(monkeypatch, _LateRefusalProvider())
    install_provider(monkeypatch, _UnsupportedModelProvider())
    install_provider(monkeypatch, _AheadProvider())
    yield


class _FakeProvider(ScriptedProvider):
    """Emits two events then exits — the shape of a real stream, none of the cost."""

    name = "fake"
    script = (
        'import json,sys\n'
        'print(json.dumps({"type":"system","subtype":"init","cwd":"/tmp","tools":[],'
        '"session_id":"SID"}), flush=True)\n'
        'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
        '"total_cost_usd":0.5,"usage":{"output_tokens":7},"session_id":"SID"}), flush=True)\n'
    )


class _AheadProvider(_FakeProvider):
    """Starts ahead as Claude Code does: its task arrives as the first stream-json line on its stdin.
    Each child says how it got its task, and a child given its task as an argument says what its
    stdin is."""

    name = "ahead"
    starts_ahead = True
    said = (
        'print(json.dumps({"type":"system","subtype":"init","session_id":"SID"}), flush=True)\n'
        'print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":said}]}}), flush=True)\n'
        'print(json.dumps({"type":"result","subtype":"success","is_error":False,"usage":{"output_tokens":1},"session_id":"SID"}), flush=True)\n'
    )

    def command(self, task, **_arguments):
        return [sys.executable, "-c", 'import json,os,sys\nsaid=f"cold {os.path.realpath(\'/proc/self/fd/0\')} {sys.argv[1]}"\n' + self.said, task]

    def ahead_command(self, **_arguments):
        return [sys.executable, "-c", 'import json,sys\nsaid="ahead " + json.loads(sys.stdin.readline())["message"]["content"]\n' + self.said]

    def task_message(self, task):
        return ClaudeCodeProvider().task_message(task)


class _UnsupportedModelProvider(_FakeProvider):
    """Says what Codex says to a ChatGPT login asked for an API-only model (run 699e3e56), then exits."""
    name = "unsupported"
    script = (
        'import json\n'
        'print(json.dumps({"type":"error","message":json.dumps({"type":"error","status":400,"error":{"type":"invalid_request_error",'
        '"message":"The \'gpt-6-astra\' model is not supported when using Codex with a ChatGPT account."}})}), flush=True)\n'
        'raise SystemExit(1)\n'
    )


class _CrashingProvider(_FakeProvider):
    name = "crash"

    def command(self, task, *, cwd, model, mcp_config, run_id, agent=None,
                permission_mode=None, allowed_tools=None, reasoning=None, coarse_accepted=False,
                base_url=None, **_ignored):
        return [sys.executable, "-c", "import sys; sys.exit(3)"]


class _LateRefusalProvider(_FakeProvider):
    """Stays alive past the post-spawn quota probe's window (and its grace period), THEN emits a
    Fable-style refusal and exits — the real shape of #reopened: minutes of working output, then
    a refusal `_quota_probe` was never watching for any more."""

    name = "late-refuse"
    script = (
        'import json,sys,time\n'
        'print(json.dumps({"type":"system","subtype":"init","cwd":"/tmp","tools":[],'
        '"session_id":"SID"}), flush=True)\n'
        'time.sleep(2)\n'
        'print(json.dumps({"type":"result","is_error":True,"session_id":"SID",'
        '"result":"You have reached your Fable limit. Switch to another model, or manage usage credits."}), flush=True)\n'
        'sys.exit(1)\n'
    )


@pytest.mark.asyncio
async def test_a_late_refusal_past_the_probe_window_still_cools_the_model_down(tmp_path, monkeypatch):
    """LIVE, end to end: a REAL subprocess, past the post-spawn probe's window and grace period,
    dying on a refusal the probe was never watching for by then. `run_agent` must still commit to
    this candidate (a late refusal is this run's own outcome, never grounds to retry another
    candidate) — and once the process actually exits, `_reap` -> `finish` must record the
    cooldown so the NEXT launch does not pick the same model and die again."""
    quota.forget()
    assert quota.blocked_until("late-refuse", "fixture-model") is None

    provider = _LateRefusalProvider()
    handle = await run_agent(provider, "do a thing", agent="tester", cwd=str(tmp_path),
                             quota_window=0.2)
    # The early probe cleared it (script sleeps 2s, well past window=0.2s + the default 1.5s
    # grace) — this run already committed to the candidate before the refusal ever arrived.
    assert reg.get_run(handle.run_id).provider == "late-refuse"

    await asyncio.wait_for(handle.wait(), timeout=30)

    finished = reg.get_run(handle.run_id)
    assert finished.status == "failed"
    assert quota.blocked_until("late-refuse", "fixture-model") is not None, \
        "a refusal that arrived after the probe window never cooled this model down"


@pytest.mark.asyncio
@pytest.mark.parametrize("mesh", [True, False])
async def test_a_run_streams_its_events_and_records_its_cost(tmp_path, mesh):
    run = await run_agent(_FakeProvider(), "do a thing", agent="tester", name="worker", cwd=str(tmp_path), mesh=mesh)
    await asyncio.wait_for(run.wait(), timeout=30)

    events = reg.read_events(run.run_id)
    assert [e.kind for e in events] == ["started", "done"]
    listed = [r for r in reg.list_runs() if r.run_id == run.run_id][0]
    assert listed.status == "done" and listed.exit_code == 0
    assert listed.cost_usd == pytest.approx(0.5)
    assert listed.last == "done"
    assert listed.provider_session_id == "SID"
    assert listed.requested_criterion == "fixture-model" and listed.reasoning == "medium"
    assert listed.mesh_enabled is mesh


@pytest.mark.asyncio
async def test_a_failing_run_is_recorded_as_failed(tmp_path):
    run = await run_agent(_CrashingProvider(), "boom", agent="tester", name="w", cwd=str(tmp_path))
    await asyncio.wait_for(run.wait(), timeout=30)
    assert [r for r in reg.list_runs() if r.run_id == run.run_id][0].status == "failed"


@pytest.mark.asyncio
async def test_the_run_appears_in_the_registry_while_still_alive(tmp_path):
    """The supervisor must see a run BEFORE it finishes — otherwise there is nothing to watch."""
    slow = _FakeProvider()
    slow.command = lambda *a, **k: [sys.executable, "-c", "import time; time.sleep(5)"]
    run = await run_agent(slow, "slow", agent="tester", name="w", cwd=str(tmp_path))
    try:
        listed = [r for r in reg.list_runs() if r.run_id == run.run_id]
        assert listed and listed[0].status == "running"
    finally:
        reg.stop(run.run_id)


@pytest.mark.asyncio
async def test_a_child_is_attributed_to_its_parent(tmp_path):
    parent = await run_agent(_FakeProvider(), "p", agent="tester", name="parent", cwd=str(tmp_path))
    child = await run_agent(_FakeProvider(), "c", agent="tester", name="child", cwd=str(tmp_path),
                            parent_run_id=parent.run_id)
    await asyncio.wait_for(parent.wait(), timeout=30)
    await asyncio.wait_for(child.wait(), timeout=30)
    assert [c.run_id for c in reg.children_of(parent.run_id)] == [child.run_id]


# ── the mesh config: how a spawned agent reaches back into galaius ──────────────────────────


def test_the_mesh_config_declares_galaius_as_an_mcp_server():
    import json

    cfg = json.loads(mesh_config(run_id="r1"))
    assert "galaius" in cfg["mcpServers"]
    entry = cfg["mcpServers"]["galaius"]
    assert entry["command"] and isinstance(entry["args"], list)


def test_the_mesh_config_tags_the_child_so_its_own_spawns_are_attributed():
    import json

    cfg = json.loads(mesh_config(run_id="r1"))
    env = cfg["mcpServers"]["galaius"]["env"]
    # Without this the tree is flat and "who started this agent" is unanswerable.
    assert env["GALAIUS_PARENT_RUN_ID"] == "r1"


def test_the_mesh_config_carries_no_credential():
    cfg = mesh_config(run_id="r1").lower()
    for banned in ("api_key", "apikey", "token", "oauth", "secret"):
        assert banned not in cfg, f"{banned!r} must never be handed to a spawned agent"


@pytest.fixture
def codex_configuration(monkeypatch):
    """Provider-resolved entries, with no duplicate interpretation of config layers."""
    # An installed program's full path: Windows resolves a bare name on PATH before spawning.
    monkeypatch.setattr(CodexProvider, "executable", lambda self: sys.executable)

    def configure(entry):
        rows = [] if entry is None else [entry]
        monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(rows), "",
        ))

    return configure


@pytest.mark.asyncio
@pytest.mark.parametrize("mesh,entry", [
    (True, None), (False, None),
    (True, {"name": "galaius", "enabled": True, "transport": {"command": "system-wrapper"}}),
    (True, {"name": "galaius", "enabled": False, "transport": {"command": "system-wrapper"}}),
    (True, {"name": "galaius", "transport": {"url": "https://example.invalid/mcp"}}),
])
async def test_codex_launcher_mesh_reaches_spawn_boundary_without_overriding_opt_out(
    tmp_path, monkeypatch, codex_configuration, mesh, entry
):
    codex_configuration(entry)
    provider = CodexProvider()
    monkeypatch.setattr(provider, "available", lambda: True)
    monkeypatch.setattr(provider, "_inject_definition", lambda agent, task, agent_prompt=None: task)
    monkeypatch.setattr(run_module, "resolve_model", lambda *args, **kwargs: ({}, "fixture-model"))
    monkeypatch.setattr(provider, "authenticated", AsyncMock(return_value=True))
    captured = []

    async def capture(*argv, **kwargs):
        captured.extend(argv)
        raise RuntimeError("fixture spawn boundary")

    monkeypatch.setattr(run_module.asyncio, "create_subprocess_exec", capture)
    with pytest.raises(RuntimeError, match="fixture spawn boundary"):
        await run_agent(provider, "read", agent="tester", cwd=str(tmp_path), mesh=mesh)
    overrides = [captured[i + 1] for i, value in enumerate(captured) if value == "-c"]
    data = tomllib.loads("\n".join(overrides))
    assert ("mcp_servers" in data) == (mesh and entry is None)
    assert "approval_policy" not in data and "sandbox_mode" not in data


@pytest.mark.parametrize("mesh,entry", [
    (True, None), (False, None), (None, None),
    (True, {"name": "galaius", "enabled": True, "transport": {"command": "system-wrapper"}}),
    (True, {"name": "galaius", "enabled": False, "transport": {"command": "system-wrapper"}}),
    (True, {"name": "galaius", "transport": {"url": "https://example.invalid/mcp"}}),
])
def test_codex_resume_replays_only_recorded_mesh_opt_in(tmp_path, monkeypatch, codex_configuration, mesh, entry):
    codex_configuration(entry)
    kwargs = {} if mesh is None else {"mesh_enabled": mesh}
    run = reg.register(run_id="fixture-run", pid=None, provider="codex", name="tester",
                       agent="tester", cwd=str(tmp_path), session_id="fixture-owner", **kwargs)
    provider = CodexProvider()
    monkeypatch.setattr(provider, "_inject_definition", lambda agent, task, agent_prompt=None: task)
    captured = []

    def capture(argv, **kwargs):
        captured.extend(argv)
        assert kwargs["env"]["GALAIUS_PARENT_RUN_ID"] == run.run_id
        raise RuntimeError("fixture resume boundary")

    monkeypatch.setattr(run_module.subprocess, "Popen", capture)
    with pytest.raises(RuntimeError, match="fixture resume boundary"):
        run_module.launch_continuation(provider, reg.get_run(run.run_id), "vendor-thread", "read",
                                       model="fixture-model", criterion=None, reasoning="medium", raw_index=0)
    overrides = [captured[i + 1] for i, value in enumerate(captured) if value == "-c"]
    data = tomllib.loads("\n".join(overrides))
    assert ("mcp_servers" in data) == (mesh is True and entry is None)
    if mesh is True and entry is None:
        assert data["mcp_servers"]["galaius"]["env"] == {"GALAIUS_PARENT_RUN_ID": run.run_id}
    assert "approval_policy" not in data and "sandbox_mode" not in data
    assert reg.get_run(run.run_id).session_id == "fixture-owner"


def test_image_paths_are_absolute_supported_readable_and_bounded(tmp_path):
    image = tmp_path / "screen.JPEG"
    image.write_bytes(b"jpeg")
    assert validate_image_paths((image,)) == (image,)

    with pytest.raises(ValueError, match="absolute"):
        validate_image_paths((Path("relative.png"),))
    with pytest.raises(ValueError, match="unsupported image attachment type"):
        validate_image_paths((tmp_path / "notes.txt",))

    too_many = []
    for index in range(9):
        path = tmp_path / f"screen-{index}.png"
        path.write_bytes(b"png")
        too_many.append(path)
    with pytest.raises(ValueError, match="at most 8"):
        validate_image_paths(too_many)


def test_unknown_model_is_refused_for_image_attachments(monkeypatch):
    import galaius.agents.run as run_mod

    monkeypatch.setattr(run_mod.Model, "catalog", lambda: [])
    monkeypatch.setattr(run_mod.Model, "match_published", lambda model: None)
    with pytest.raises(run_mod.ModelUnavailable, match="not known.*cap.vlm"):
        run_mod._require_vlm_model("unknown-model")


def test_non_vlm_model_is_refused_for_image_attachments(monkeypatch):
    import galaius.agents.run as run_mod
    from galaius.models import Model, ModelCapability

    candidate = Model(id="text-model", provider="openai", capabilities={ModelCapability.LLM})
    monkeypatch.setattr(run_mod.Model, "catalog", lambda: [candidate])
    monkeypatch.setattr(run_mod.Model, "match_published", lambda model: candidate)
    with pytest.raises(run_mod.ModelUnavailable, match="does not meet cap.vlm"):
        run_mod._require_vlm_model("text-model")


@pytest.mark.asyncio
async def test_events_survive_the_spawning_loop_ending(tmp_path):
    """The defect this replaced: the event stream ran on the CALLER's event loop, so a caller that
    spawned and returned lost every event — and the run then looked HEALTHY (status done, exit 0,
    no cost, no activity), which is worse than looking crashed. The child writes its own stream to
    disk now, so nothing is lost when the supervising coroutine goes away."""
    run = await run_agent(_FakeProvider(), "t", agent="tester", name="w", cwd=str(tmp_path))
    run.pump.cancel()  # the caller went away mid-run
    await asyncio.wait_for(run.process.wait(), timeout=30)

    events = reg.read_events(run.run_id)
    assert [e.kind for e in events] == ["started", "done"], "the stream did not survive"
    listed = [r for r in reg.list_runs() if r.run_id == run.run_id][0]
    assert listed.cost_usd == pytest.approx(0.5), "cost was lost with the supervising task"
    assert listed.last == "done"


# ── Seeing a response as it arrives ─────────────────────────────────────────────────────────
# The child writes only the RAW vendor stream; the normalised copy the VS Code panel reads is
# produced on demand. So while an agent was actually working, the panel saw nothing at all — a
# live run showed "waiting" from start to finish, and "view responses as they come in" was
# impossible by construction.


@pytest.mark.asyncio
async def test_the_normalised_stream_is_kept_current_while_a_run_is_alive(tmp_path, monkeypatch):
    from galaius.agents import registry as reg
    from galaius.agents.run import _mirror_while_alive

    reg.register(run_id="r1", name="a", provider="claude", task="t", pid=None)
    raw = reg.raw_events_path("r1")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps({
        "type": "assistant", "session_id": "s",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "first"}]},
    }) + "\n")

    alive = {"value": True}
    task = asyncio.create_task(_mirror_while_alive("r1", lambda: alive["value"], interval=0.02))
    await asyncio.sleep(0.1)
    assert "first" in reg.events_path("r1").read_text(), "the panel reads this file"

    with raw.open("a") as f:
        f.write(json.dumps({
            "type": "assistant", "session_id": "s",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "second"}]},
        }) + "\n")
    await asyncio.sleep(0.1)
    assert "second" in reg.events_path("r1").read_text(), "a later turn must appear without a poke"

    alive["value"] = False
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_the_pump_stops_when_the_run_does(tmp_path, monkeypatch):
    from galaius.agents import registry as reg
    from galaius.agents.run import _mirror_while_alive

    reg.register(run_id="r1", name="a", provider="claude", task="t", pid=None)
    task = asyncio.create_task(_mirror_while_alive("r1", lambda: False, interval=0.01))
    await asyncio.wait_for(task, timeout=2)  # must not spin forever on a finished run


@pytest.mark.asyncio
async def test_a_broken_read_never_kills_the_pump(tmp_path, monkeypatch):
    """It runs beside a live agent; a transient read failure must not take the stream down."""
    from galaius.agents import registry as reg
    from galaius.agents.run import _mirror_while_alive

    calls = {"n": 0}

    def _boom(run_id):
        calls["n"] += 1
        raise OSError("transient")

    monkeypatch.setattr(reg, "read_events", _boom)
    alive = {"value": True}
    task = asyncio.create_task(_mirror_while_alive("r1", lambda: alive["value"], interval=0.01))
    await asyncio.sleep(0.08)
    alive["value"] = False
    await asyncio.wait_for(task, timeout=2)
    assert calls["n"] > 1, "it kept going after the failure"


# ── Starting an agent without waiting for it ────────────────────────────────────────────────
# `agents run` streams until the agent finishes, which is right at a terminal and useless to a
# UI: the panel needs the run id NOW so it can show the agent working. The child already writes
# its own stream and leads its own session, so nothing is lost by letting go of it.


def test_spawn_returns_the_run_id_without_waiting(monkeypatch, tmp_path, capsys):
    import importlib

    # `galaius.cli.app` is BOTH a module and the cyclopts App object the package re-exports; the
    # attribute shadows the module, so import it explicitly rather than by attribute access.
    cli = importlib.import_module("galaius.cli.app")

    class _Handle:
        run_id = "abcd1234-0000-0000-0000-000000000000"

    async def _fake_run(provider, task, **kw):
        assert kw["agent"] == "code-reviewer"
        return _Handle()

    monkeypatch.setattr(cli, "_run_agent_for_cli", _fake_run, raising=False)
    cli.agents_spawn("review the diff", agent="code-reviewer")
    assert "abcd1234" in capsys.readouterr().out


# ── Keeping every live run's stream current, not only the ones we spawned ───────────────────
# `agents spawn` detaches, so the pump started beside it dies with the parent. The child keeps
# writing its RAW stream, but the normalised copy the VS Code panel reads is only produced on
# demand — so a detached agent's activity froze in the panel until something happened to call
# Python. The server is the process alive whenever the user is working, so it keeps them all fresh.


async def _mirror_once(monkeypatch, read):
    from galaius.agents import registry as reg
    from galaius.agents.run import _mirror_running_runs

    monkeypatch.setattr(reg, "read_events", read)
    alive = {"value": True}
    task = asyncio.create_task(_mirror_running_runs(lambda: alive["value"], interval=0.02))
    await asyncio.sleep(0.08)
    alive["value"] = False
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_every_running_run_is_kept_current_and_nothing_else_is_read(monkeypatch):
    """The loop ticks every second in every MCP server. Deriving the whole history there re-parsed
    1.6 GB of transcripts per tick: each idle server held ~70% of a core."""
    from tests.support import register_run

    for run_id in ("live-1", "live-2"):
        register_run(run_id, name=run_id, provider="claude", task="t", pid=os.getpid())
    register_run("ended", name="ended", provider="claude", task="t", pid=None)
    seen: list[str] = []
    await _mirror_once(monkeypatch, lambda run_id: seen.append(run_id) or [])

    assert {"live-1", "live-2"} <= set(seen)
    assert "ended" not in seen, "a settled run costs nothing to leave alone"


@pytest.mark.asyncio
async def test_an_unchanged_stream_is_not_reparsed_on_every_tick(monkeypatch):
    """A live agent's transcript grows to megabytes; re-parsing it every second while nothing was
    written kept each idle MCP server at ~20% of a core."""
    from galaius.agents import registry as reg
    from tests.support import register_run

    register_run("quiet", name="quiet", provider="claude", task="t", pid=os.getpid())
    reg.raw_events_path("quiet").write_text("{}\n")
    seen: list[str] = []
    await _mirror_once(monkeypatch, lambda run_id: seen.append(run_id) or [])

    assert seen == ["quiet"]


@pytest.mark.asyncio
async def test_one_unreadable_run_never_stops_the_others(monkeypatch):
    from tests.support import register_run

    for run_id in ("bad", "good"):
        register_run(run_id, name=run_id, provider="claude", task="t", pid=os.getpid())
    seen: list[str] = []

    def _read(run_id):
        if run_id == "bad":
            raise OSError("half-written")
        seen.append(run_id)
        return []

    await _mirror_once(monkeypatch, _read)
    assert "good" in seen


@pytest.mark.asyncio
async def test_a_model_the_vendor_refuses_for_this_login_is_passed_over_and_remembered(tmp_path):
    """Codex answering "model not supported with a ChatGPT account" a second after start is not this
    run's outcome: the launch passes the model over (`model_capability_unsupported`, like a quota
    refusal) and remembers it for a day, so the next ranked model of that CLI runs and no later launch
    pays the same dead child. With one candidate only, the launch refuses by name."""
    quota.forget()
    with pytest.raises(run_module.ModelUnavailable, match="model_capability_unsupported"):
        await run_agent(_UnsupportedModelProvider(), "do a thing", agent="tester", cwd=str(tmp_path), quota_window=0.5)
    until = quota.blocked_until("unsupported", "fixture-model")
    assert until is not None and 23 * 3600 < until - __import__("time").time() <= 24 * 3600
    assert run_module._startup_refusal("The 'gpt-6-astra' model is not supported when using Codex with a ChatGPT account.") == "model_capability_unsupported"
    assert run_module._startup_refusal("You have reached your weekly limit") == "quota_exceeded"
    assert run_module._startup_refusal("Selected model is at capacity") is None


async def _held(warm: WarmStart) -> str:
    for _ in range(200):
        if warm.holding:
            return warm.holding[-1]
        await asyncio.sleep(0.05)
    raise AssertionError("no child was started ahead")


def _said(run_id: str) -> str:
    return next(event.text for event in reg.read_events(run_id) if event.kind == "text")


@pytest.mark.asyncio
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the child reads its stdin through /proc")
async def test_the_next_start_of_the_same_launch_runs_on_the_child_started_ahead(tmp_path):
    warm = WarmStart(delay=0, check_every=0.05)
    other = tmp_path / "other"
    other.mkdir()
    try:
        # The launcher's stdin is a pipe, as a galaius MCP server's is (its protocol).
        read, write = os.pipe()
        saved = os.dup(0)
        os.dup2(read, 0)
        try:
            first = await run_agent(_AheadProvider(), "first task", agent="tester", cwd=str(tmp_path), warm=warm)
        finally:
            os.dup2(saved, 0)
            for descriptor in (saved, read, write):
                os.close(descriptor)
        await asyncio.wait_for(first.wait(), timeout=30)
        assert _said(first.run_id).startswith("cold /dev/null "), "a child started with its task never reads the launcher's stdin"

        held = await _held(warm)
        second = await run_agent(_AheadProvider(), "second task", agent="tester", cwd=str(tmp_path), warm=warm)
        await asyncio.wait_for(second.wait(), timeout=30)
        assert second.run_id == held and _said(second.run_id).startswith("ahead ") and "second task" in _said(second.run_id)

        held = await _held(warm)
        third = await run_agent(_AheadProvider(), "third task", agent="tester", cwd=str(other), warm=warm)
        await asyncio.wait_for(third.wait(), timeout=30)
        assert third.run_id != held and _said(third.run_id).startswith("cold "), "another folder is another launch"
        assert held in warm.holding, "the first folder's child still waits for its next start"
    finally:
        warm.close()
    assert not reg.raw_events_path(held).exists() and reg.get_run(held) is None, "an unused child leaves nothing behind"


def _launch(folder: Path, home: Path, *, arguments: dict | None = None, env: dict | None = None) -> ChildLaunch:
    return ChildLaunch(
        provider=ClaudeCodeProvider(), cwd=str(folder), environment={"HOME": str(home), **(env or {})}, meshed=True,
        arguments={"cwd": str(folder), "model": "claude-opus-5-5", "agent": "tester", "permission_mode": "manual",
                   "reasoning": "medium", **(arguments or {})},
    )


@pytest.mark.parametrize("change", [
    {"arguments": {"model": "claude-sonnet-5"}}, {"arguments": {"agent": "visual-critic"}},
    {"arguments": {"permission_mode": "bypassPermissions"}}, {"env": {"ANTHROPIC_BASE_URL": "http://other"}},
    {"edit": ".claude/CLAUDE.md"}, {"edit": ".claude/settings.json"}, {"edit": ".claude/agents/tester.md"},
    {"edit": "work/CLAUDE.md"}, {"edit": "work/.claude/settings.local.json"}, {"edit": "AGENTS.md"},
    {"edit": "work/.claude/agents/tester.md"}, {"edit": "work/.mcp.json"}, {"edit": "work/docs/imported.md"},
    {"edit": "work/.claude/rules/style.md"}, {"edit": "work/.claude/skills/x/SKILL.md"},
    {"edit": "work/.claude/commands/ship.md"},
])
def test_a_child_started_ahead_serves_only_its_own_launch(tmp_path, change):
    """Each input a Claude child takes at its start is in its launch key: a held child started before
    any of them changed is never handed a task."""
    home, folder = tmp_path, tmp_path / "work"
    (home / ".claude" / "agents").mkdir(parents=True)
    (folder / "docs").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("be brief")
    (folder / "CLAUDE.md").write_text("see @docs/imported.md")
    (folder / "docs" / "imported.md").write_text("an imported rule")
    held = _launch(folder, home).key()
    (folder / "CLAUDE.md").write_text("see @docs/imported.md")  # rewritten, same content: still fresh
    assert _launch(folder, home).key() == held
    if (edited := change.pop("edit", None)) is not None:
        (tmp_path / edited).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / edited).write_text("changed rule")
    assert _launch(folder, home, **change).key() != held
