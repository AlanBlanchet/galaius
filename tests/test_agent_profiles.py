"""Per-agent model routing, without opening an escalation hole.

Alan asked: "why couldn't we mix? Can't we control the env that an agent (or sub-agent) spawns in
by activating / deactivating what the agent can do before called? Even for other providers?"

Yes — and the safe shape is the one he described: decide what an agent may do BEFORE the call,
rather than letting the caller hand over an environment. A raw `env` dict on an MCP tool is a
model-reachable path to `LD_PRELOAD`, `PATH`, and key exfiltration; this project already refuses
that shape once, where `agent_spawn` may not select an unrestricted permission mode.

So a profile is a NAME the operator defines, and it resolves to a fixed, allow-listed set of
variables. There is no arrangement of inputs that turns a profile into an arbitrary environment.
"""

import json

import pytest

from interact.agents.profiles import (
    ALLOWED_ENV, PROFILE_PREFIX, overlay_for, profiles_from,
)


def test_a_profile_is_defined_by_the_operator_not_the_caller():
    env = {f"{PROFILE_PREFIX}CHEAP": "ollama/deepseek-v4-flash", "UNRELATED": "x"}
    assert profiles_from(env) == {"cheap": "ollama/deepseek-v4-flash"}


def test_a_profile_resolves_to_a_base_url_and_a_model():
    got = overlay_for("ollama/deepseek-v4-flash", env={"OLLAMA_API_BASE": "http://localhost:11434"})
    assert got["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
    assert got["ANTHROPIC_MODEL"] == "deepseek-v4-flash", "the provider prefix is ours, not the CLI's"


def test_nothing_outside_the_allow_list_can_ever_be_set():
    """The whole point. Even a profile value crafted to look like an assignment cannot add a key."""
    got = overlay_for("ollama/x\nLD_PRELOAD=/tmp/evil.so", env={})
    assert set(got) <= ALLOWED_ENV
    assert "LD_PRELOAD" not in got
    for key in ("PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "NODE_OPTIONS"):
        assert key not in ALLOWED_ENV, f"{key} must never be settable through a profile"


@pytest.mark.parametrize("hostile", [
    "ollama/x@http://evil.example",
    "ollama/x http://evil.example",
    "ollama/../../etc/passwd",
    "ollama/x\nANTHROPIC_BASE_URL=http://evil.example",
])
def test_a_hostile_model_id_cannot_smuggle_an_endpoint(hostile):
    """A base URL comes from the OPERATOR's own environment, never from the model string.

    A malformed id is REFUSED rather than sanitised into something plausible: the failure mode of
    guessing is sending the operator's credentials to an endpoint nobody chose, and there is no
    version of that worth the convenience.
    """
    got = overlay_for(hostile, env={"OLLAMA_API_BASE": "http://localhost:11434"})
    assert "evil.example" not in str(got)
    assert got == {} or got["ANTHROPIC_BASE_URL"] == "http://localhost:11434"


def test_a_model_with_no_provider_prefix_routes_nowhere():
    """An unprefixed model is the vendor's own default — no base URL override, nothing to redirect."""
    assert overlay_for("opus", env={}) == {}


def test_an_unknown_profile_is_refused_rather_than_guessed():
    with pytest.raises(KeyError):
        profiles_from({})["nope"]


@pytest.mark.parametrize("value", ["", "   ", "/", "ollama/"])
def test_a_malformed_profile_value_yields_nothing(value):
    assert overlay_for(value, env={}) == {}


@pytest.fixture
def named_profile_role(tmp_path):
    """Satisfy real role-definition and model-policy prerequisites before profile validation."""
    from interact.config import UserConfig

    role = "profile-test-role"
    definition = tmp_path / ".claude" / "agents" / f"{role}.md"
    definition.parent.mkdir(parents=True)
    definition.write_text("---\nname: profile-test-role\n---\nFixture role.\n", encoding="utf-8")
    policy = UserConfig.PATH.parent / "agents.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps({"agents": {role: "cap.vlm"}}), encoding="utf-8")
    return role


@pytest.mark.asyncio
async def test_the_spawn_tool_refuses_a_profile_nobody_defined(monkeypatch, named_profile_role):
    """Silently ignoring an unknown profile is the dangerous version: the agent runs, looks fine,
    and quietly used the wrong model. Refuse, and say which profiles exist."""
    import interact.server as srv

    monkeypatch.setattr("interact.agents.providers.ClaudeCodeProvider.available", lambda self: True)

    monkeypatch.delenv("INTERACT_PROFILE_CHEAP", raising=False)
    out = await srv.agent_spawn("do a thing", agent=named_profile_role, profile="does-not-exist")
    assert out.startswith("ERROR:"), out
    assert "does-not-exist" in out


@pytest.mark.asyncio
async def test_a_known_profile_cannot_override_a_named_roles_policy(monkeypatch, named_profile_role):
    import interact.server as srv

    monkeypatch.setattr("interact.agents.providers.ClaudeCodeProvider.available", lambda self: True)
    monkeypatch.setenv("INTERACT_PROFILE_CHEAP", "ollama/fixture-model")
    out = await srv.agent_spawn("do a thing", agent=named_profile_role, profile="cheap")
    assert out.startswith("ERROR:"), out
    assert "cannot be bypassed with a provider profile" in out


@pytest.mark.asyncio
async def test_a_defined_profile_reaches_the_spawn(monkeypatch):
    """The producer/consumer seam: a profile that resolves must actually reach `run_agent`, not
    merely exist in a module nothing calls."""
    import interact.server as srv
    from interact.agents import run as run_mod

    monkeypatch.setattr("interact.agents.providers.ClaudeCodeProvider.available", lambda self: True)

    monkeypatch.setenv("INTERACT_PROFILE_CHEAP", "ollama/deepseek-v4-flash")
    seen: dict = {}

    async def fake_run(provider, task, **kw):
        seen.update(kw)
        raise RuntimeError("stop here — the argument is what we are testing")

    monkeypatch.setattr(run_mod, "run_agent", fake_run, raising=False)
    monkeypatch.setattr(srv.tools_agents, "run_agent", fake_run, raising=False)
    await srv.agent_spawn("do a thing", profile="cheap")
    assert seen.get("profile") == "cheap", f"the profile never reached the spawn: {seen}"


def test_a_provider_prefixed_model_routes_even_without_a_named_profile(monkeypatch):
    """The gap that made "researcher runs on DeepSeek V4" untrue in practice.

    A named profile (INTERACT_PROFILE_X=ollama/model) got the env overlay AND a bare model name for
    the CLI. But a `provider/name` id declared in the company file — or chosen in the panel — was
    handed to the vendor CLI verbatim, which cannot resolve `ollama/deepseek-v4-pro:cloud`, so every
    dispatch died at startup. The pin was then reverted with the cause recorded as "routing config
    is Alan's side"; it was interact's side.
    """
    from interact.agents.run import resolve_model

    env = {"OLLAMA_API_BASE": "http://localhost:11434"}
    overlay, cli_model = resolve_model("ollama/deepseek-v4-pro:cloud", env)
    assert overlay["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
    assert overlay["ANTHROPIC_MODEL"] == "deepseek-v4-pro:cloud"
    assert cli_model == "deepseek-v4-pro:cloud", (
        "the CLI must be handed the BARE name; the prefix says where to send it, not what to ask for"
    )


def test_a_vendor_model_is_left_completely_alone():
    """`claude-sonnet-5` means the vendor's own default endpoint. Redirecting it would send the
    operator's credentials somewhere nobody chose."""
    from interact.agents.run import resolve_model

    overlay, cli_model = resolve_model("claude-sonnet-5", {})
    assert overlay == {}
    assert cli_model == "claude-sonnet-5"


def test_an_unroutable_prefix_is_left_alone_rather_than_guessed():
    """A provider with no known endpoint must not be invented — refusing to act is safe here."""
    from interact.agents.run import resolve_model

    overlay, cli_model = resolve_model("whoknows/some-model", {})
    assert overlay == {}
    assert cli_model == "whoknows/some-model"


def test_no_model_asked_for_means_no_opinion():
    from interact.agents.run import resolve_model

    assert resolve_model(None, {}) == ({}, None)


# ── an OpenAI-compatible endpoint (HF's router, a self-hosted vLLM/TGI box) ─────────────────────


def test_hf_router_resolves_to_its_own_known_base_url_with_no_operator_override():
    """Hugging Face's router has exactly one address — the one thing here allowed a default."""
    got = overlay_for("hf/glm-5.2", env={})
    assert got["OPENAI_BASE_URL"] == "https://router.huggingface.co/v1"
    assert got["OPENAI_MODEL"] == "glm-5.2"


def test_a_self_hosted_box_has_no_default_and_a_bare_provider_routes_nowhere():
    """vLLM/TGI/self-hosted has no ONE obvious address (multi-GPU box case) — base URL ABSENT
    means the operator never configured it, and refusing to act stays the safe default."""
    assert overlay_for("vllm/glm-5.2", env={}) == {}


def test_a_self_hosted_box_resolves_once_the_operator_names_it():
    got = overlay_for("vllm/glm-5.2", env={"VLLM_BASE_URL": "http://gpubox.lan:8000/v1"})
    assert got["OPENAI_BASE_URL"] == "http://gpubox.lan:8000/v1"
    assert got["OPENAI_MODEL"] == "glm-5.2"


@pytest.mark.parametrize("provider", ["hf", "vllm"])
@pytest.mark.parametrize("hostile_base", [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "unix:///var/run/docker.sock",
    "/etc/passwd",
    "ftp://box.lan/",
])
def test_a_non_http_scheme_base_url_is_refused_not_smuggled(provider, hostile_base):
    """WRONG SCHEME case: a mistyped or hostile base URL must never reach a downstream HTTP
    client dressed as a URL — only http/https are ever honoured."""
    env_key = "HF_BASE_URL" if provider == "hf" else "VLLM_BASE_URL"
    got = overlay_for(f"{provider}/glm-5.2", env={env_key: hostile_base})
    # hf still has its own safe default to fall back to; vllm has none.
    assert got == {} or got.get("OPENAI_BASE_URL") == "https://router.huggingface.co/v1"
    assert "passwd" not in str(got) and "docker.sock" not in str(got) and "alert" not in str(got)


def test_a_bearer_token_is_copied_into_one_fixed_key_never_the_real_openai_name():
    """The operator's own HF_TOKEN must reach the routed request, but never under the literal
    OPENAI_API_KEY name — that would masquerade as (or collide with) a real OpenAI credential
    Codex's native, ChatGPT-authenticated provider might also read from the same process env."""
    got = overlay_for("hf/glm-5.2", env={"HF_TOKEN": "tok42"})
    assert got["INTERACT_OPENAI_COMPAT_KEY"] == "tok42"
    assert "OPENAI_API_KEY" not in got


def test_no_token_configured_means_no_key_in_the_overlay():
    """A bare self-hosted server started with no --api-key needs no Authorization header at
    all — absence must stay absence, never an empty-string credential."""
    got = overlay_for("vllm/glm-5.2", env={"VLLM_BASE_URL": "http://gpubox.lan:8000/v1"})
    assert "INTERACT_OPENAI_COMPAT_KEY" not in got


def test_an_openai_compat_model_routes_even_when_the_catalog_has_never_heard_of_it():
    """MODEL THE CATALOG HAS NEVER HEARD OF case, at the same seam the ollama regression test
    above covers: a literal provider/name id resolves on shape alone, never on catalog lookup."""
    from interact.agents.run import resolve_model

    env = {"VLLM_BASE_URL": "http://gpubox.lan:8000/v1"}
    overlay, cli_model = resolve_model("vllm/some-model-nobody-catalogued", env)
    assert overlay["OPENAI_BASE_URL"] == "http://gpubox.lan:8000/v1"
    assert cli_model == "some-model-nobody-catalogued"


# ── activate once, never twice ──────────────────────────────────────────────────────────────────


def test_a_child_is_not_handed_interact_twice(tmp_path, monkeypatch):
    """"we should be able to activate the 'agents' for the provider, but once (and not twice)...
    no conflicts."

    A spawned Claude agent received --mcp-config registering interact — while the user's own
    ~/.claude.json ALREADY registers interact at user scope, because that is what `interact
    install` sets up. The child then carries two registrations of the same server. Attribution
    does not need the duplicate: INTERACT_PARENT_RUN_ID travels in the child's process
    environment, which the globally-configured server inherits.
    """
    from interact.agents.run import already_meshed

    cfg = tmp_path / ".claude.json"
    cfg.write_text('{"mcpServers": {"interact": {"command": "/usr/local/bin/interact"}}}')
    assert already_meshed("claude") is True


def test_a_machine_without_interact_registered_still_gets_the_mesh(tmp_path, monkeypatch):
    """The mesh exists for exactly this case: a provider with no interact of its own."""
    from interact.agents.run import already_meshed

    assert already_meshed("claude") is False
    (tmp_path / ".claude.json").write_text('{"mcpServers": {}}')
    assert already_meshed("claude") is False


def test_a_broken_provider_config_never_blocks_the_spawn(tmp_path, monkeypatch):
    """A corrupt ~/.claude.json must degrade to 'not registered' — doubling a server is annoying,
    a spawn that refuses to start is worse."""
    from interact.agents.run import already_meshed

    (tmp_path / ".claude.json").write_text("{ not json")
    assert already_meshed("claude") is False


def test_an_unknown_provider_is_assumed_unmeshed():
    from interact.agents.run import already_meshed

    assert already_meshed("fixture-unknown-provider") is False
