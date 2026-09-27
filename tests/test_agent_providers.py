"""Provider adapters for the agent CLIs interact supervises.

Parsing is pinned against a synthetic stream (`tests/fixtures/agents/claude_stream.jsonl`) shaped
exactly like `claude -p --output-format stream-json` output — same event types, same field names,
same nesting — with all content replaced by neutral placeholders so no real session data ships in
the repo.

The legal line these tests also encode: interact builds an argv and reads stdout. It never reads,
stores or forwards a credential — the CLI authenticates itself with the user's own login.
"""

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from interact.agents.events import TOKEN_FIELDS, AgentEvent, UsageLedger
from interact.agents.providers import (
    PROVIDERS, ClaudeCodeProvider, CodexProvider, UnsupportedToolPolicy, provider_for,
)
from interact.models import Model


@pytest.mark.parametrize("resume", [False, True])
def test_claude_role_tools_and_denials_are_available_tool_restrictions_on_every_turn(resume):
    provider = ClaudeCodeProvider()
    options = dict(model="fixture-model", agent="visual-critic", agent_prompt="Pinned role instructions",
                   allowed_tools=["Read", "Bash", "mcp__interact__screenshot", "mcp__interact__report_issue"],
                   denied_tools=("mcp__interact__report_issue",))
    command = provider.resume_command("fixture-session", "Review", **options) if resume else provider.command(
        "Review", cwd="/tmp", mcp_config=None, run_id="fixture-run", **options)
    definition = json.loads(command[command.index("--agents") + 1])["visual-critic"]
    assert definition["prompt"] == "Pinned role instructions"
    assert definition["tools"] == options["allowed_tools"]
    assert command[command.index("--agent") + 1] == "visual-critic"
    assert command[command.index("--disallowedTools") + 1] == "mcp__interact__report_issue"
    assert "--allowedTools" not in command
    assert "--permission-mode" not in command


def test_denied_tool_names_cannot_inject_flags_or_unverified_patterns():
    provider = ClaudeCodeProvider()
    for tool in ("--permission-mode", "Bash(git *)", "mcp__*", "Read,Write", "Read\nWrite"):
        with pytest.raises(ValueError, match="tool"):
            provider.command("t", cwd="/tmp", model=None, mcp_config=None, run_id="x", denied_tools=(tool,))


@pytest.mark.parametrize("provider_type", [ClaudeCodeProvider, CodexProvider])
def test_role_definition_rejects_malformed_frontmatter_and_unsafe_names(provider_type, monkeypatch, tmp_path):
    provider = provider_type()
    path = tmp_path / "role.md"
    path.write_text("---\nname: fixture\n")
    monkeypatch.setattr(provider, "definition_path", lambda agent: path)
    with pytest.raises(ValueError, match="frontmatter"):
        provider.definition_prompt("fixture")
    with pytest.raises(ValueError, match="name"):
        provider.definition_prompt("../fixture")
    path.write_text("---\nname: fixture\n---\nRole instructions")
    assert provider.definition_prompt("fixture") == "Role instructions"
    path.write_text("Role instructions")
    assert provider.definition_prompt("fixture") == "Role instructions"


def test_codex_auth_status_requires_an_affirmative_login_message():
    provider = CodexProvider()
    assert provider.accepts_any_auth("", "Logged in using ChatGPT")
    assert provider.accepts_any_auth("Logged in using an API key", "")
    assert provider.accepts_any_auth("", "WARNING: proceeding\nLogged in using ChatGPT")
    for status in ("logged in: false", "Not logged in", "Login failed", ""):
        assert not provider.accepts_any_auth(status, "")


def test_codex_command_uses_the_effort_resolved_by_the_launcher():
    command = CodexProvider().command(
        "Reply once", cwd="/tmp", model="fixture-model", mcp_config=None,
        run_id="fixture-run", reasoning="medium",
    )
    assert 'model_reasoning_effort="medium"' in command


def test_codex_command_honors_the_requested_write_scope():
    provider = CodexProvider()
    command = provider.command(
        "Edit the assigned file", cwd="/tmp", model="fixture-model", mcp_config=None,
        run_id="fixture-run", permission_mode="acceptEdits",
    )
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    with pytest.raises(ValueError, match="not a permission intent"):
        provider.command(
            "Edit the assigned file", cwd="/tmp", model="fixture-model", mcp_config=None,
            run_id="fixture-run", permission_mode="invented-mode",
        )


def test_shared_workspace_write_intent_maps_to_each_harness():
    """The local default names the capability, not one vendor's flag spelling."""
    assert ClaudeCodeProvider().provider_permission_mode("workspace_write") == "acceptEdits"
    assert CodexProvider().provider_permission_mode("workspace_write") == "workspace-write"


def test_native_claude_model_id_uses_cli_version_spelling_without_a_model_pin():
    provider = ClaudeCodeProvider()
    model = Model(
        provider="anthropic", id="claude-opus-9.9", capabilities=set(),
    )
    assert provider.model_id_for(model) == "claude-opus-9-9"


def test_codex_uses_only_documented_approval_flags():
    provider = CodexProvider()
    plan = provider.command("t", cwd="/tmp", model=None, mcp_config=None, run_id="r", permission_mode="plan")
    auto = provider.command("t", cwd="/tmp", model=None, mcp_config=None, run_id="r", permission_mode="auto")
    assert "--ask-for-approval" not in plan and "--ask-for-approval" not in auto
    assert "--approve-for-me" in auto


@pytest.mark.parametrize(
    ("intent", "claude_mode", "codex_sandbox"),
    [
        ("plan", "plan", "read-only"),
        ("manual", "manual", "read-only"),
        ("auto", "auto", "workspace-write"),
        ("acceptEdits", "acceptEdits", "workspace-write"),
        ("dontAsk", "dontAsk", "workspace-write"),
        ("bypassPermissions", "bypassPermissions", "workspace-write"),
    ],
)
def test_shared_permission_intents_translate_for_each_provider(
    intent, claude_mode, codex_sandbox,
):
    claude = ClaudeCodeProvider().command(
        "t", cwd="/tmp", model=None, mcp_config=None, run_id="r",
        permission_mode=intent,
    )
    codex = CodexProvider().command(
        "t", cwd="/tmp", model=None, mcp_config=None, run_id="r",
        permission_mode=intent,
    )
    assert claude[claude.index("--permission-mode") + 1] == claude_mode
    assert codex[codex.index("--sandbox") + 1] == codex_sandbox


@pytest.mark.parametrize("resume", [False, True])
def test_codex_projects_mesh_without_changing_permissions(resume):
    provider = CodexProvider()
    config = json.dumps({"mcpServers": {"interact": {
        "command": '/fixture path/\U00010400/interact', "args": ["mcp", 'quoted"arg'],
        "env": {"INTERACT_PARENT_RUN_ID": "fixture-run"},
    }}})
    # A pure-read role: no write, no network tool named, so no extra `-c` table beyond the mesh's
    # own two — isolates "does the mesh smuggle permission flags" from the sandbox-mapping tests.
    kwargs = dict(allowed_tools=["Read"])
    if resume:
        argv = provider.resume_command("thread", "continue", mcp_config=config, **kwargs)
    else:
        argv = provider.command("read", cwd=".", model=None, mcp_config=config, run_id="fixture-run", **kwargs)
    overrides = [argv[i + 1] for i, value in enumerate(argv) if value == "-c"]
    data = tomllib.loads("\n".join(overrides))
    assert data["mcp_servers"]["interact"] == json.loads(config)["mcpServers"]["interact"]
    assert set(data) == {"features", "mcp_servers"}


# --- Mapping a role's NATIVE tool policy onto codex's OWN mechanisms -------------------------
#
# Codex has no per-name flag for a native tool, but it is not toothless: `--sandbox` genuinely
# blocks a write (apply_patch AND shell `>`/`rm`/... alike — verified live 2026-09-22, both fail
# at the OS level under `-s read-only`, only the OS-vs-approval error text differs), and
# `-c features.shell_tool=false` genuinely removes the shell tool from the model's toolset
# (codex-rs 0.155.1 `tools/spec_plan.rs` L969-973). A role's allow-list is read as INTENT and
# translated: Read/Grep/Glob need nothing (reads are never gated); Write/Edit need
# workspace-write; Bash needs the shell tool kept; WebFetch/WebSearch need local network, which
# codex only documents a toggle for under workspace-write — the one combination (network without
# write) that has no clean equivalent and still falls back to the coarse opt-in, named concretely.

@pytest.mark.parametrize("allowed,expected_sandbox,shell_disabled,network_on", [
    ([], "workspace-write", False, True),                                          # no restriction at all
    (["Read", "Grep", "Glob"], "read-only", True, False),                          # pure read: no reason for a shell
    (["Read", "Grep", "Glob", "Bash"], "read-only", False, False),                 # read-only critic
    (["Read", "Write", "Edit"], "workspace-write", True, False),                   # writes, no shell (e.g. teacher)
    # A builder runs dev servers, test suites and the interact launcher through its shell, and
    # codex's sandbox cannot tell loopback from the internet: without the toggle a builder
    # cannot even bind 127.0.0.1 (live: PermissionError: [Errno 1] Operation not permitted).
    (["Read", "Bash", "Write", "Edit"], "workspace-write", False, True),           # builder
    (["Read", "Bash", "Write", "Edit", "WebFetch", "WebSearch"], "workspace-write", False, True),  # builder + network
    (["Agent", "SendMessage", "ListAgents"], "read-only", True, False),            # Claude-only orchestration names
])
def test_codex_maps_native_tool_policy_onto_sandbox_and_feature_flags(allowed, expected_sandbox, shell_disabled, network_on):
    provider = CodexProvider()
    argv = provider.command("read", cwd=".", model=None, mcp_config=None,
                            run_id="fixture-run", allowed_tools=allowed)
    assert argv[argv.index("--sandbox") + 1] == expected_sandbox
    assert (argv.count("--disable") > 0 and "shell_tool" in argv) is shell_disabled
    assert ("sandbox_workspace_write.network_access=true" in " ".join(argv)) is network_on


def test_codex_denied_write_narrows_an_unrestricted_roles_sandbox():
    """`denied_tools` subtracts even when nothing was explicitly allowed — it only narrows,
    matching `agent_spawn`'s own contract ('never grants permissions')."""
    provider = CodexProvider()
    argv = provider.command("read", cwd=".", model=None, mcp_config=None,
                            run_id="fixture-run", denied_tools=("Write", "Edit"))
    assert argv[argv.index("--sandbox") + 1] == "read-only"


@pytest.mark.parametrize("resume", [False, True])
def test_codex_network_without_write_has_no_documented_sandbox_equivalent(resume):
    """The one genuine gap: codex documents a network toggle only under workspace-write, so a
    role wanting WebFetch/WebSearch but no filesystem write has no exact codex mapping — refused,
    naming the combination, until the operator accepts the coarser (workspace-write) substitute."""
    provider = CodexProvider()
    kwargs = dict(allowed_tools=["Read", "WebFetch"])
    with pytest.raises(UnsupportedToolPolicy, match="network"):
        if resume:
            provider.resume_command("thread", "continue", **kwargs)
        else:
            provider.command("read", cwd=".", model=None, mcp_config=None, run_id="fixture-run", **kwargs)
    if resume:
        argv = provider.resume_command("thread", "continue", coarse_accepted=True, **kwargs)
    else:
        argv = provider.command("read", cwd=".", model=None, mcp_config=None,
                                run_id="fixture-run", coarse_accepted=True, **kwargs)
    assert "sandbox_workspace_write.network_access=true" in " ".join(argv)


@pytest.mark.parametrize("resume", [False, True])
def test_codex_needs_no_coarse_acceptance_for_its_own_mcp_tools(resume):
    """Unlike native tools, `mcp__interact__*` names ARE fully expressible — no tradeoff, no
    opt-in, because nothing is left unenforced."""
    provider = CodexProvider()
    kwargs = dict(allowed_tools=["mcp__interact__screenshot", "mcp__interact__run_actions"],
                  denied_tools=("mcp__interact__report_issue",))
    if resume:
        argv = provider.resume_command("thread", "continue", **kwargs)
    else:
        argv = provider.command("read", cwd=".", model=None, mcp_config=None, run_id="fixture-run", **kwargs)
    joined = " ".join(argv)
    assert 'mcp_servers.interact.enabled_tools=["run_actions", "screenshot"]' in joined
    assert 'mcp_servers.interact.disabled_tools=["report_issue"]' in joined


@pytest.mark.parametrize("resume", [False, True])
def test_codex_mixed_native_and_mcp_tools_needs_acceptance_but_still_enforces_the_mcp_half(resume):
    """A role naming BOTH an mcp tool and a GENUINELY unmapped native one (no sandbox / feature
    flag speaks to it at all) is refused for the native remainder alone — never blamed on the mcp
    name it already covers, and never on a native name (Bash, Write, ...) codex CAN map; once
    accepted, the mcp restriction still applies exactly."""
    provider = CodexProvider()
    kwargs = dict(allowed_tools=["NotebookEdit", "mcp__interact__screenshot"])
    with pytest.raises(UnsupportedToolPolicy) as excinfo:
        if resume:
            provider.resume_command("thread", "continue", **kwargs)
        else:
            provider.command("read", cwd=".", model=None, mcp_config=None, run_id="fixture-run", **kwargs)
    assert "NotebookEdit" in str(excinfo.value) and "screenshot" not in str(excinfo.value)
    if resume:
        argv = provider.resume_command("thread", "continue", coarse_accepted=True, **kwargs)
    else:
        argv = provider.command("read", cwd=".", model=None, mcp_config=None,
                                run_id="fixture-run", coarse_accepted=True, **kwargs)
    assert 'mcp_servers.interact.enabled_tools=["screenshot"]' in " ".join(argv)


@pytest.mark.parametrize("rows,expected", [
    ([{"name": "interact", "enabled": True, "transport": {"command": "system-wrapper"}}], True),
    ([{"name": "interact", "enabled": False, "transport": {"url": "https://example.invalid/mcp"}}], True),
    ([{"name": "another-server"}], False),
    ([], False),
])
def test_codex_registration_uses_provider_effective_configuration(tmp_path, monkeypatch, rows, expected):
    provider = CodexProvider()
    monkeypatch.setattr(provider, "executable", lambda: "fixture-codex")
    calls = []

    def configured(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")

    monkeypatch.setattr(subprocess, "run", configured)
    assert provider.has_mcp_server("interact", cwd=str(tmp_path)) is expected
    assert calls == [(["fixture-codex", "mcp", "list", "--json"], {
        "cwd": str(tmp_path), "capture_output": True, "text": True, "timeout": 10,
    })]


@pytest.mark.parametrize("failure", ["timeout", "os-error", "exit", "json", "shape"])
def test_codex_registration_discovery_fails_closed_without_echoing_output(tmp_path, monkeypatch, failure):
    provider = CodexProvider()
    monkeypatch.setattr(provider, "executable", lambda: "fixture-codex")

    def failed(argv, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 10, output="fixture-sensitive-value")
        if failure == "os-error":
            raise OSError("fixture-sensitive-value")
        output = '[{"missing_name":"fixture-sensitive-value"}]' if failure == "shape" else "fixture-sensitive-value"
        return subprocess.CompletedProcess(argv, 1 if failure == "exit" else 0, output, "fixture-sensitive-value")

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(ValueError, match="^Cannot inspect Codex MCP configuration safely$"):
        provider.has_mcp_server("interact", cwd=str(tmp_path))


@pytest.mark.parametrize("field,value", [
    ("env", {"INTERACT_PARENT_RUN_ID": "fixture-run", "TOKEN": "fixture-sensitive-value"}),
    ("enabled", True), ("command", ""), ("args", [42]),
])
def test_codex_mesh_rejects_extra_fields_and_invalid_values_without_echoing_them(field, value):
    server = {"command": "fixture-interact", "args": ["mcp"],
              "env": {"INTERACT_PARENT_RUN_ID": "fixture-run"}, field: value}
    with pytest.raises(ValueError, match="^Invalid credential-free Codex mesh configuration$"):
        CodexProvider.mesh_arguments(json.dumps({"mcpServers": {"interact": server}}))


def test_codex_image_support_is_verified_from_installed_help(monkeypatch):
    provider = CodexProvider()
    monkeypatch.setattr(provider, "executable", lambda: "/usr/bin/codex")

    class _Help:
        returncode = 0
        stdout = "  -i, --image <FILE>...  Optional image(s)"

    monkeypatch.setattr("interact.agents.providers.subprocess.run", lambda *a, **k: _Help())
    assert provider.image_attachment_support() is True

    _Help.stdout = "  -m, --model <MODEL>"
    assert provider.image_attachment_support() is False


def test_codex_command_emits_one_image_flag_per_attachment(monkeypatch, tmp_path):
    provider = CodexProvider()
    monkeypatch.setattr(provider, "image_attachment_support", lambda: True)
    first = tmp_path / "first.png"
    second = tmp_path / "second.webp"
    command = provider.command(
        "Inspect", cwd="/tmp", model="fixture-model", mcp_config=None,
        run_id="fixture-run", image_paths=(first, second),
    )
    assert command[-5:] == ["--image", str(first), str(second), "--", "Inspect"]


def test_unsupported_provider_refuses_image_attachments(tmp_path):
    image = tmp_path / "screen.png"
    image.write_bytes(b"png")
    with pytest.raises(ValueError, match="does not support image attachments"):
        ClaudeCodeProvider().command(
            "Inspect", cwd="/tmp", model="fixture-model", mcp_config=None,
            run_id="fixture-run", image_paths=(image,),
        )


def test_codex_failed_command_is_a_tool_result_not_a_terminal_agent_failure():
    event = CodexProvider().parse(json.dumps({
        "type": "item.completed", "item": {
            "id": "item_6", "type": "command_execution", "command": "pytest",
            "aggregated_output": "1 failed", "exit_code": 1, "status": "completed",
        },
    }))
    assert event is not None
    assert event.kind == "tool_result" and event.status == "failed"
    assert event.tool_id == "item_6" and event.text == "1 failed"


def test_codex_command_start_exposes_the_actual_tool_and_command():
    event = CodexProvider().parse(json.dumps({
        "type": "item.started", "item": {
            "id": "item_6", "type": "command_execution", "command": "pytest",
            "status": "in_progress",
        },
    }))
    assert event is not None
    assert event.kind == "tool" and event.tool == "Shell"
    assert event.tool_id == "item_6" and event.tool_input == "pytest"


def test_codex_delivery_commands_use_the_vendor_thread_id():
    provider = CodexProvider()
    assert provider.queue_command("vendor-thread", "ping") == [
        "codex", "queue", "--thread", "vendor-thread", "--message", "ping",
    ]
    resumed = provider.resume_command(
        "vendor-thread", "ping", model="fresh-model", reasoning="high",
        permission_mode="acceptEdits",
    )
    assert resumed[:3] == ["codex", "exec", "resume"] and resumed[-3:] == ["--", "vendor-thread", "ping"]
    assert "--json" in resumed and "--model" in resumed
    assert 'model_reasoning_effort="high"' in resumed
    assert "--sandbox" not in resumed
    assert 'sandbox_mode="workspace-write"' in resumed


def test_codex_resume_does_not_re_inject_the_role_definition(monkeypatch, tmp_path):
    """Unlike Claude (a separate ``--agents`` channel), Codex can only seed a role by baking its
    definition into the FIRST turn's message — :func:`test_codex_command_injects_the_named_role_definition`
    below covers that. That first turn is already part of the saved session; re-baking the whole
    definition into every RESUMED message doubles input tokens per turn and was observed live to
    make the model re-answer the original task instead of the new message. A resumed turn sends
    exactly the new message."""
    root = tmp_path / "interact" / "prompts" / "agents"
    root.mkdir(parents=True)
    (root / "tester.md").write_text(
        "---\nname: tester\n---\nAGENT_ROLE: tester\nDo the assigned check.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    resumed = CodexProvider().resume_command("vendor-thread", "continue", agent="tester")

    assert resumed[-1] == "continue"


def test_codex_command_injects_the_named_role_definition(monkeypatch, tmp_path):
    """The FIRST turn has no other channel to seed the role, so it must bake the definition in."""
    root = tmp_path / "interact" / "prompts" / "agents"
    root.mkdir(parents=True)
    (root / "tester.md").write_text(
        "---\nname: tester\n---\nAGENT_ROLE: tester\nDo the assigned check.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    started = CodexProvider().command(
        "continue", cwd="/tmp", model=None, mcp_config=None, run_id="fixture-run", agent="tester",
    )
    prompt = started[-1]

    assert started[-2] == "--" and prompt.startswith("AGENT_ROLE: tester\n")
    assert "Do the assigned check." in prompt
    assert "Delegated task:\ncontinue" in prompt


def test_codex_thread_start_keeps_vendor_session_identity():
    event = CodexProvider().parse(json.dumps({
        "type": "thread.started", "thread_id": "vendor-thread",
    }))
    assert event is not None and event.session_id == "vendor-thread"

# ── an OpenAI-compatible endpoint (HF's router, a self-hosted vLLM box) as a Codex target ───────


def test_codex_routes_a_base_url_through_a_named_isolated_provider():
    """The routed endpoint must land in a NEW ``model_providers`` entry, never on the built-in
    ``openai``/``chatgpt`` ids — those are tied to the user's own ChatGPT/API-key login, and
    reusing them would risk sending that session's credential to a third-party box."""
    command = CodexProvider().command(
        "Reply once", cwd="/tmp", model="glm-5.2", mcp_config=None,
        run_id="fixture-run", base_url="http://gpubox.lan:8000/v1",
    )
    joined = " ".join(command)
    assert 'model_providers.interact_openai_compat.base_url="http://gpubox.lan:8000/v1"' in joined
    assert 'model_provider="interact_openai_compat"' in joined
    assert "openai_base_url" not in joined, "must never touch the native openai provider's base url"
    assert '-c model_providers.openai.' not in joined
    assert '-c model_providers.chatgpt.' not in joined


def test_codex_omits_openai_compat_routing_when_no_base_url_is_given():
    command = CodexProvider().command(
        "Reply once", cwd="/tmp", model="fixture-model", mcp_config=None, run_id="fixture-run",
    )
    assert "model_providers.interact_openai_compat" not in " ".join(command)


def test_codex_resume_also_carries_the_routed_base_url():
    command = CodexProvider().resume_command(
        "vendor-thread", "continue", base_url="http://gpubox.lan:8000/v1", model="glm-5.2",
    )
    assert 'model_providers.interact_openai_compat.base_url="http://gpubox.lan:8000/v1"' in " ".join(command)


def test_claude_cannot_claim_an_openai_protocol_model_even_when_its_base_url_is_set():
    """The overlay-truthiness bug: overlay_for now returns a non-empty dict for BOTH wire
    protocols, so can_run must discriminate by KEY, never by mere presence."""
    model = Model(id="glm-5.2", provider="vllm", capabilities=set())
    env = {"VLLM_BASE_URL": "http://gpubox.lan:8000/v1"}
    assert ClaudeCodeProvider().can_run(model, env) is False


def test_codex_can_run_a_routed_openai_compat_model():
    model = Model(id="glm-5.2", provider="vllm", capabilities=set())
    env = {"VLLM_BASE_URL": "http://gpubox.lan:8000/v1"}
    assert CodexProvider().can_run(model, env) is True


def test_claude_still_claims_ollama_after_the_can_run_tightening(monkeypatch):
    """Availability asks the live daemon; this test is about the route, so it pins availability."""
    monkeypatch.setattr(Model, "is_available", lambda self: True)
    model = Model(id="deepseek-v4-flash", provider="ollama", capabilities=set())
    assert ClaudeCodeProvider().can_run(model, {"OLLAMA_API_BASE": "http://localhost:11434"}) is True


FIXTURE = Path(__file__).parent / "fixtures" / "agents" / "claude_stream.jsonl"


def _events() -> list[AgentEvent]:
    p, ledger = ClaudeCodeProvider(), UsageLedger()
    out = []
    for line in FIXTURE.read_text().splitlines():
        ev = p.parse(line, ledger)
        if ev is not None:
            out.append(ev)
    return out


# ── parsing a real stream ────────────────────────────────────────────────────────────────────


def test_the_real_stream_parses_end_to_end():
    evs = _events()
    kinds = [e.kind for e in evs]
    assert "started" in kinds, f"no init event recognised: {kinds}"
    assert "text" in kinds
    assert kinds[-1] == "done"


def test_the_assistant_text_is_extracted():
    said = [e.text for e in _events() if e.kind == "text"]
    assert any("OK" in t for t in said), said


def test_cost_and_tokens_come_off_the_result_event():
    events = _events()
    done = [e for e in events if e.kind == "done"][-1]
    # Claude Code reports API-equivalent cost even on a subscription run — this is what makes
    # per-agent cost real rather than a guess.
    assert done.cost_usd is not None and done.cost_usd > 0
    assert done.output_tokens is not None
    assert sum(e.input_tokens or 0 for e in events) == 106591, \
        "run accounting includes cached prompt tokens"


def test_every_event_carries_the_session_id():
    evs = _events()
    ids = {e.session_id for e in evs if e.session_id}
    assert len(ids) == 1, f"expected one session, got {ids}"


def test_a_rate_limit_event_is_surfaced_as_its_own_kind():
    # Running several agents on one account hits a POOLED limit; a run that dies on it must say
    # so rather than just going quiet.
    raw = [json.loads(l) for l in FIXTURE.read_text().splitlines()]
    if not any(r.get("type") == "rate_limit_event" for r in raw):
        pytest.skip("this capture has no rate-limit event")
    assert any(e.kind == "rate_limit" for e in _events())


def test_junk_lines_never_raise():
    p = ClaudeCodeProvider()
    assert p.parse("") is None
    assert p.parse("not json") is None
    assert p.parse('{"type":"totally-unknown"}') is not None  # kept as `other`, never dropped


# ── building the command ─────────────────────────────────────────────────────────────────────


def test_claude_command_asks_for_a_parseable_stream():
    argv = ClaudeCodeProvider().command("do a thing", cwd="/tmp", model="sonnet",
                                        mcp_config='{"mcpServers":{}}', run_id="11111111-2222-3333-4444-555555555555")
    assert argv[0] == "claude"
    assert "-p" in argv and "do a thing" in argv
    # stream-json REQUIRES --verbose; without it the CLI refuses and we'd get no events at all.
    assert "--output-format" in argv and "stream-json" in argv and "--verbose" in argv


def test_the_run_id_is_the_session_id():
    # Assigning the session id means our registry key and the vendor's own id are the SAME string,
    # so `claude --resume <run_id>` and our records line up with no mapping table.
    rid = "11111111-2222-3333-4444-555555555555"
    argv = ClaudeCodeProvider().command("t", cwd="/tmp", model=None, mcp_config=None, run_id=rid)
    assert argv[argv.index("--session-id") + 1] == rid


def test_the_mesh_config_is_passed_through():
    cfg = '{"mcpServers":{"interact":{"command":"interact"}}}'
    argv = ClaudeCodeProvider().command("t", cwd="/tmp", model=None, mcp_config=cfg, run_id="r")
    assert argv[argv.index("--mcp-config") + 1] == cfg


def test_no_credential_is_ever_placed_on_the_command_line():
    argv = ClaudeCodeProvider().command("t", cwd="/tmp", model="opus", mcp_config=None, run_id="r")
    joined = " ".join(argv).lower()
    for banned in ("api_key", "apikey", "token", "oauth", "bearer", "credential"):
        assert banned not in joined, f"{banned!r} must never appear — the CLI authenticates itself"


# ── the provider set ─────────────────────────────────────────────────────────────────────────


def test_providers_are_addressable_by_name():
    assert provider_for("claude") is not None
    assert isinstance(provider_for("claude"), ClaudeCodeProvider)
    assert "claude" in PROVIDERS


def test_an_unknown_provider_names_the_ones_that_exist():
    with pytest.raises(ValueError) as exc:
        provider_for("gpt5-cli")
    assert "claude" in str(exc.value)


def test_codex_is_verified_and_states_the_limit_that_remains():
    # The adapter was exercised against a real codex binary (spawn, a resumed thread, the quota
    # refusal), so it no longer claims to be unverified. Most of a role's native tool policy now
    # maps exactly onto --sandbox + feature flags; ONE limit survives and must be stated: network
    # without a write tool has no documented codex toggle.
    c = CodexProvider()
    assert c.verified is True
    assert "webfetch" in (c.caveat or "").lower()  # gap itself: test_codex_network_without_write_has_no_documented_sandbox_equivalent


def test_a_verified_providers_real_caveat_still_shows():
    """`verified` says the flags were checked against a real binary; it is NOT permission to hide
    a caveat that stays true regardless — every one of the three surfaces that print this
    (`agent_providers`, the CLI `providers` command, the per-spawn NOTE) shares this one line."""
    from interact.agents.providers import provider_caveat_note

    assert "no codex-documented equivalent" in provider_caveat_note(CodexProvider())


def test_an_unverified_provider_with_no_written_caveat_falls_back_to_saying_so():
    from interact.agents.providers import provider_caveat_note

    class _Untested(ClaudeCodeProvider):
        verified = False
        caveat = None

    assert "unverified" in provider_caveat_note(_Untested()).lower()


def test_a_verified_provider_with_no_caveat_says_nothing():
    from interact.agents.providers import provider_caveat_note

    class _Solid(ClaudeCodeProvider):
        verified = True
        caveat = None

    assert provider_caveat_note(_Solid()) == ""


# ── what a CONVERSATION view needs (not just a status line) ──────────────────────────────────
# The tree's one-line summary was enough to answer "what is it doing"; showing the actual
# conversation needs the tool's INPUT, the tool's RESULT, and the model's thinking — all of which
# the stream carries and the first normalisation discarded.


def _parse(raw: dict):
    import json as _json
    return ClaudeCodeProvider().parse(_json.dumps(raw))


def test_a_tool_call_keeps_its_input():
    ev = _parse({"type": "assistant", "session_id": "s", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la", "description": "list"}}]}})
    assert ev.kind == "tool" and ev.tool == "Bash"
    assert "ls -la" in ev.tool_input, "the command is the whole point of showing a Bash call"


def test_a_tool_result_is_its_own_event():
    ev = _parse({"type": "user", "session_id": "s", "message": {"content": [
        {"type": "tool_result", "content": "file-a\nfile-b"}]}})
    assert ev.kind == "tool_result" and "file-a" in ev.text


def test_thinking_is_kept_but_marked_as_thinking():
    ev = _parse({"type": "assistant", "session_id": "s", "message": {"content": [
        {"type": "thinking", "thinking": "weighing the options"}]}})
    assert ev.kind == "thinking" and "weighing" in ev.text


def test_a_long_tool_result_is_truncated_not_dropped():
    ev = _parse({"type": "user", "session_id": "s", "message": {"content": [
        {"type": "tool_result", "content": "x" * 50_000}]}})
    assert 0 < len(ev.text) < 5000, "a huge result must not blow up the transcript file"


def test_a_named_agent_definition_is_passed_through():
    """Claude Code resolves `--agent <name>` against the agent files (~/.claude/agents/*.md), so a
    run can BE visual-critic rather than an anonymous 'claude'. That name is also what the panel
    should show, which is why the caller gets it back rather than inventing a label."""
    argv = ClaudeCodeProvider().command("t", cwd="/tmp", model=None, mcp_config=None,
                                        run_id="r", agent="visual-critic")
    assert argv[argv.index("--agent") + 1] == "visual-critic"


def test_no_agent_flag_when_none_is_named():
    argv = ClaudeCodeProvider().command("t", cwd="/tmp", model=None, mcp_config=None, run_id="r")
    assert "--agent" not in argv


# ── agents talking to each other ─────────────────────────────────────────────────────────────
# Delivery is not a bespoke protocol either: our run_id IS the vendor's session id, so resuming
# that session with a message continues the SAME conversation — the recipient keeps its context
# and its transcript grows in place, which is what makes the exchange visible afterwards.


def test_resume_continues_the_same_session_with_the_message():
    argv = ClaudeCodeProvider().resume_command("11111111-2222-3333-4444-555555555555", "ping")
    assert argv[0] == "claude"
    assert argv[argv.index("--resume") + 1] == "11111111-2222-3333-4444-555555555555"
    assert "ping" in argv
    # Still a parseable stream, or the reply would never reach the transcript.
    assert "--output-format" in argv and "stream-json" in argv and "--verbose" in argv


def test_a_provider_that_cannot_resume_says_so():
    assert CodexProvider().can_resume is True
    assert CodexProvider().can_queue is True
    assert ClaudeCodeProvider().can_resume is True


# ── Housekeeping is not activity ────────────────────────────────────────────────────────────
# The activity view rendered more `other` rows than real ones: token accounting and hook
# lifecycle lines outnumbered what the agent actually DID, so the transparency the panel exists
# for was buried in noise. Bookkeeping the vendor emits about its own machinery is dropped;
# anything describing the agent's WORK is kept and named.


@pytest.mark.parametrize(
    "subtype",
    ["thinking_tokens", "hook_started", "hook_response"],
)
def test_vendor_housekeeping_is_dropped_not_shown_as_other(subtype):
    from interact.agents.providers import ClaudeCodeProvider

    event = ClaudeCodeProvider().parse(
        json.dumps({"type": "system", "subtype": subtype, "session_id": "s"})
    )
    assert event is None, f"{subtype} is bookkeeping about the harness, not agent activity"


def test_a_spawned_subagent_is_real_activity_and_is_kept():
    """An agent starting a Task IS the team behaviour the panel exists to show."""
    from interact.agents.providers import ClaudeCodeProvider

    event = ClaudeCodeProvider().parse(
        json.dumps({"type": "system", "subtype": "task_started", "session_id": "s"})
    )
    assert event is not None and event.kind != "other"


def test_an_unknown_system_subtype_is_still_kept_as_other():
    """A vendor adding a new event must not vanish — dropping is for the known-noisy only."""
    from interact.agents.providers import ClaudeCodeProvider

    event = ClaudeCodeProvider().parse(
        json.dumps({"type": "system", "subtype": "something_new", "session_id": "s"})
    )
    assert event is not None and event.kind == "other"


def test_the_incoming_prompt_is_named_not_lumped_as_other():
    """A `user` line carrying text is what was ASKED of the agent — the other half of the
    conversation. Left as `other` the activity view showed only the agent's replies."""
    from interact.agents.providers import ClaudeCodeProvider

    event = ClaudeCodeProvider().parse(json.dumps({
        "type": "user", "session_id": "s",
        "message": {"role": "user", "content": [{"type": "text", "text": "check the error paths"}]},
    }))
    assert event is not None and event.kind == "prompt"
    assert "check the error paths" in (event.text or "")


# ── Which agent definitions can be spawned here ─────────────────────────────────────────────
# `agent_spawn(agent=...)` resolves a definition by name, but nothing told a caller WHICH names
# exist — and an agent cannot ask a follow-up question, so an unlisted capability is an unusable
# one. The provider knows where its definitions live, so it is the provider that answers.


def test_a_provider_lists_the_agent_definitions_it_can_resolve(tmp_path, monkeypatch):
    from interact.agents.providers import ClaudeCodeProvider

    home = tmp_path / ".claude" / "agents"
    home.mkdir(parents=True)
    (home / "code-reviewer.md").write_text("---\nname: code-reviewer\n---\nreview things")
    (home / "visual-critic.md").write_text("---\nname: visual-critic\n---\nlook at things")
    (home / "notes.txt").write_text("not an agent")
    assert ClaudeCodeProvider().agent_definitions() == ["code-reviewer", "visual-critic"]


def test_no_definitions_directory_is_an_empty_list_not_an_error(tmp_path, monkeypatch):
    from interact.agents.providers import ClaudeCodeProvider

    assert ClaudeCodeProvider().agent_definitions() == []


def test_codex_lists_installed_role_definitions(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    provider = CodexProvider()
    assert provider.agent_definitions() == []
    root = tmp_path / "interact/prompts/agents"
    root.mkdir(parents=True)
    (root / "tester.md").write_text("Verify the assigned change.")
    assert provider.agent_definitions() == ["tester"]
    assert provider.definition_path("tester") == root / "tester.md"


# ── Session usage sums to the vendor's truth ────────────────────────────────────────────────
# Lines trimmed from real recorded streams (Claude run 665a5b67, Codex run 5339802d). Contract:
# input = every prompt token incl. cache reads + writes, cached / cache_write are subsets of it,
# output includes reasoning, and a run's totals are the plain SUM of its events.

_OPUS, _HAIKU = "claude-opus-5-5", "claude-haiku-4-5-20251001"


def _claude_assistant(block: str, *, message_id: str, write: int, read: int = 0) -> dict:
    return {"type": "assistant", "session_id": "s", "message": {
        "id": message_id, "model": _OPUS, "role": "assistant",
        "content": [{"type": block, block: "x"}],
        "usage": {"input_tokens": 2, "cache_creation_input_tokens": write,
                  "cache_read_input_tokens": read, "output_tokens": 6}}}


def _claude_result(cost: float, main: tuple[int, int, int, int],
                   per_model: dict[str, tuple[int, int, int, int]]) -> dict:
    """``main`` / ``per_model`` = (uncached input, cache read, cache write, output)."""
    return {"type": "result", "subtype": "success", "session_id": "s", "is_error": False,
            "result": "done", "total_cost_usd": cost,
            "usage": dict(zip(("input_tokens", "cache_read_input_tokens",
                               "cache_creation_input_tokens", "output_tokens"), main)),
            "modelUsage": {model: dict(zip(("inputTokens", "cacheReadInputTokens",
                                            "cacheCreationInputTokens", "outputTokens"), usage))
                           for model, usage in per_model.items()}}


_INIT = {"type": "system", "subtype": "init", "session_id": "s", "model": _OPUS, "tools": []}
_FIRST = _claude_result(2.68535105, (84, 4532709, 152345, 23514), {
    _HAIKU: (1712, 0, 68937, 310), _OPUS: (84, 4532709, 152345, 23514)})
_RESUMED = _claude_result(3.74336, (18, 1532956, 55022, 4689), {
    _HAIKU: (1718, 0, 241548, 634), _OPUS: (102, 6065665, 207367, 28203)})
_BLOCKS = [_claude_assistant("thinking", message_id="msg_011CfPu79Kk466DNzRoTXjkd", write=66743),
           _claude_assistant("text", message_id="msg_011CfPu79Kk466DNzRoTXjkd", write=66743)]


def _codex_turn(input_tokens: int, cached: int, write: int, output: int) -> dict:
    return {"type": "turn.completed", "usage": {
        "input_tokens": input_tokens, "cached_input_tokens": cached,
        "cache_write_input_tokens": write, "output_tokens": output,
        "reasoning_output_tokens": 348}}


@pytest.mark.parametrize(("provider", "stream", "truth"), [
    pytest.param("claude", [_INIT, *_BLOCKS], (66745, 0, 66743, 0, None),
                 id="claude-live-message-counted-once-across-its-block-lines"),
    pytest.param("claude", [_INIT, *_BLOCKS, _FIRST],
                 (1712 + 68937 + 84 + 4532709 + 152345, 4532709, 68937 + 152345, 310 + 23514,
                  2.68535105),
                 id="claude-result-reconciles-to-modelUsage-incl-internal-haiku"),
    pytest.param("claude", [_INIT, *_BLOCKS, _FIRST, _INIT, _RESUMED],
                 (1718 + 241548 + 102 + 6065665 + 207367, 6065665, 241548 + 207367,
                  634 + 28203, 3.74336),
                 id="claude-resume-restored-cumulative-counters-counted-once"),
    pytest.param("claude", [_INIT, _RESUMED, _INIT, *_BLOCKS, _FIRST],
                 (1718 + 241548 + 102 + 6065665 + 207367
                  + 1712 + 68937 + 84 + 4532709 + 152345,
                  6065665 + 4532709, 241548 + 207367 + 68937 + 152345,
                  634 + 28203 + 310 + 23514, 3.74336 + 2.68535105),
                 id="claude-resume-whose-counters-restarted-from-zero"),
    pytest.param("claude", [_INIT, *_BLOCKS, _FIRST, _FIRST | {"usage": {"input_tokens": 3}}],
                 (1712 + 68937 + 84 + 4532709 + 152345, 4532709, 68937 + 152345, 310 + 23514,
                  2.68535105),
                 id="claude-queued-turn-repeats-process-counters"),
    pytest.param("codex", [_codex_turn(529466, 463616, 0, 2883),
                           _codex_turn(1352384, 1249792, 4096, 5655)],
                 (1352384, 1249792, 4096, 5655, None),
                 id="codex-exec-resume-reports-thread-cumulative-with-cache-write"),
])
def test_session_usage_sums_to_vendor_truth(provider, stream, truth):
    ledger = UsageLedger()
    events = [e for line in stream if (e := PROVIDERS[provider].parse(json.dumps(line), ledger))]
    costs = [e.cost_usd for e in events if e.cost_usd is not None]
    summed = tuple(sum(getattr(e, field) or 0 for e in events)
                   for field in ("input_tokens", "cached_input_tokens",
                                 "cache_write_input_tokens", "output_tokens"))
    assert set(TOKEN_FIELDS) == {"input_tokens", "cached_input_tokens",
                                 "cache_write_input_tokens", "output_tokens"}
    assert summed == truth[:4]
    assert (round(sum(costs), 8) if costs else None) == truth[4]


def test_no_usage_at_all_reports_nothing_rather_than_zero():
    """Zero would render as a real measurement of an empty context."""
    from interact.agents.providers import ClaudeCodeProvider

    event = ClaudeCodeProvider().parse(json.dumps({
        "type": "assistant", "session_id": "s",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
    }))
    assert event.input_tokens is None


# ── A machine-readable list of definitions ──────────────────────────────────────────────────
# The panel's "start an agent" picker was scraping the human-readable `agents providers` output by
# splitting on "agents:" and commas — a display change would silently empty the picker.


def test_definitions_command_prints_one_name_per_line(capsys, tmp_path, monkeypatch):
    import importlib

    home = tmp_path / ".claude" / "agents"
    home.mkdir(parents=True)
    for name in ("code-reviewer", "researcher"):
        (home / f"{name}.md").write_text("---\n---\n")
    cli = importlib.import_module("interact.cli.app")
    cli.agents_definitions()
    assert capsys.readouterr().out.split() == ["code-reviewer", "researcher"]


def test_definitions_command_is_silent_when_there_are_none(capsys, tmp_path, monkeypatch):
    """Empty output, not a message: the caller is a parser, and prose would become a fake agent."""
    import importlib

    cli = importlib.import_module("interact.cli.app")
    cli.agents_definitions()
    assert capsys.readouterr().out.strip() == ""


# --- How much autonomy an agent is given ----------------------------------------------------
#
# Supervising a team means deciding what each member may do on its own. The panel could spawn
# agents but had no way to say "this one may not write" or "let this one plan first, don't touch
# anything" — every run got whatever the vendor defaults to. The modes below are read off the
# INSTALLED binary's own `--help`, never recalled: `claude --permission-mode` accepts
# acceptEdits/auto/bypassPermissions/manual/dontAsk/plan, which is not the set an older memory of
# the docs would produce.


def test_codex_offers_the_verified_bounded_sandbox_scopes():
    modes = {mode.id: mode for mode in CodexProvider().permission_modes()}
    assert set(modes) == {
        "plan", "manual", "auto", "acceptEdits", "dontAsk", "bypassPermissions",
    }
    assert modes["plan"].provider_mode == "read-only"
    assert modes["acceptEdits"].provider_mode == "workspace-write"
    assert modes["bypassPermissions"].provider_mode == "workspace-write"
    assert modes["bypassPermissions"].unrestricted is True
    assert "no exact" in modes["bypassPermissions"].detail.lower()


def test_claude_offers_the_modes_its_binary_actually_accepts():
    modes = {m.id for m in ClaudeCodeProvider().permission_modes()}
    assert modes == {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}


def test_every_offered_mode_says_what_it_lets_the_agent_do():
    for mode in ClaudeCodeProvider().permission_modes():
        assert mode.label and mode.detail, f"{mode.id} is offered with no explanation"


def test_the_dangerous_mode_is_marked_as_such():
    """bypassPermissions is the one that can act without asking. It is offered — refusing to
    expose it would just push people to the terminal — but never as an unremarkable choice."""
    modes = {m.id: m for m in ClaudeCodeProvider().permission_modes()}
    assert modes["bypassPermissions"].unrestricted is True
    assert not any(m.unrestricted for i, m in modes.items() if i != "bypassPermissions")


def test_a_chosen_mode_reaches_the_argv():
    argv = ClaudeCodeProvider().command(
        "t", cwd="/tmp", model=None, mcp_config=None, run_id="r", permission_mode="plan")
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"


def test_no_mode_means_no_flag():
    """The vendor's own default must stay reachable; passing a mode nobody asked for would
    override a setting the person configured in their own CLI."""
    argv = ClaudeCodeProvider().command(
        "t", cwd="/tmp", model=None, mcp_config=None, run_id="r")
    assert "--permission-mode" not in argv


def test_an_unknown_mode_is_refused_at_the_edge():
    """The value arrives from a tool caller and lands on a command line. An unknown one is a
    typo or an injection attempt, not a mode the CLI will helpfully ignore."""
    with pytest.raises(ValueError, match="permission intent"):
        ClaudeCodeProvider().command(
            "t", cwd="/tmp", model=None, mcp_config=None, run_id="r",
            permission_mode="--dangerously-skip-everything")


def test_a_tool_call_and_its_result_share_the_vendor_tool_id():
    """The transcript view opens a call's FULL input/output from the raw stream on click.

    The summarised event is clipped (300/2000 chars) by design; the raw line holds everything,
    and the vendor's tool_use id is the ONE stable key that pairs a call with its result across
    both files. Without it the viewer falls back to prefix-matching, which breaks the moment two
    identical commands run."""
    call = _parse({"type": "assistant", "session_id": "s", "message": {"content": [
        {"type": "tool_use", "id": "toolu_42", "name": "Bash", "input": {"command": "ls"}}]}})
    assert call.tool_id == "toolu_42"
    answer = _parse({"type": "user", "session_id": "s", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_42", "content": "a\nb"}]}})
    assert answer.tool_id == "toolu_42", "the result must carry the id that names its question"


def test_codex_initial_and_resumed_delegates_cannot_bypass_shared_role_policy():
    provider = CodexProvider()
    commands = (
        provider.command("Read", cwd="/tmp", model="fixture-model", mcp_config=None, run_id="fixture"),
        provider.resume_command("fixture-session", "Continue", model="fixture-model"),
    )
    for command in commands:
        assert "features.multi_agent=false" in command
        assert "features.multi_agent_v2=false" in command


# ───────────── Agent-name validation (formerly test_agent_name_validation.py) ─────────────────
#
# `agent` arrives from the MCP boundary and is handed to the provider, which turns it into a
# filesystem path (`~/.claude/agents/<agent>.md`) that is now RECORDED on the run and offered by
# the VS Code panel as a clickable "system prompt" link. Nothing validated it, so
# `agent="../../x"` walked out of the definitions directory into a path a person would then click.
# Exploitability is low but the fix belongs at the edge, not in the sink.


def test_a_traversing_agent_name_is_refused_before_it_becomes_a_path():
    prov = provider_for("claude")
    assert prov.valid_definition("../../../etc/passwd") is False
    assert prov.valid_definition("../secrets") is False
    assert prov.valid_definition("nested/name") is False
    assert prov.valid_definition("") is False


def test_a_real_definition_is_accepted(monkeypatch):
    prov = provider_for("claude")
    monkeypatch.setattr(type(prov), "agent_definitions", lambda self: ["code-reviewer", "tester"])
    assert prov.valid_definition("code-reviewer") is True
    assert prov.valid_definition("ghost") is False


@pytest.mark.asyncio
async def test_spawn_refuses_an_unknown_definition_and_says_which_exist(monkeypatch):
    import interact.server as srv
    from interact.agents.providers import ClaudeCodeProvider

    monkeypatch.setattr(ClaudeCodeProvider, "available", lambda self: True)
    monkeypatch.setattr(ClaudeCodeProvider, "agent_definitions", lambda self: ["tester", "researcher"])

    async def never(*a, **k):
        raise AssertionError("a process was started for an unvalidated definition name")

    monkeypatch.setattr(srv.tools_agents, "run_agent", never)

    out = await srv.tools_agents.agent_spawn("do a thing", agent="../../../etc/passwd")
    assert out.startswith("ERROR:")
    assert "tester" in out and "researcher" in out, "an agent cannot ask, so list what IS valid"


@pytest.mark.parametrize("provider", [ClaudeCodeProvider(), CodexProvider()], ids=["claude", "codex"])
@pytest.mark.parametrize("text", ["- a markdown bullet", "--help", "-"])
def test_a_prompt_starting_with_a_dash_is_never_an_option(provider, text):
    """A message the owner types ("- fix the header") reaches the model as text: after "--", last."""
    started = provider.command(text, cwd="/tmp", model=None, mcp_config=None, run_id="fixture-run")
    resumed = provider.resume_command("vendor-thread", text)
    assert started[-2:] == ["--", text] and resumed[-1] == text and resumed[-3 if provider.name == "codex" else -2] == "--"
