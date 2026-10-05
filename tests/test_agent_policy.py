"""One file that says how agents run here: profiles, toolsets, and which providers are on.

"we should be able to, from interact, chose if we activate the agents or not for a provider
(claude, codex, other...)... profiles, such that we can write the rules and apply them to
multiple models instead of having to write conditions for each agent... Same for tools, we don't
have toolsets yet and are having to write interact__xxx to select some of our MCP tools..."

All three are the same shape — a NAME standing for a rule you would otherwise repeat — so they
live in one file with one grammar rather than three conventions to remember.
"""

import json

import pytest

from interact.agents.policy import Policy, PolicyError
from tests.support.agents import use_policy
from tests.support.models import catalog_of


@pytest.fixture
def policy_file(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({
        "profiles": {
            "eyes": "cap.vlm and price.in < 10",
            "thrifty": "price.in < 1",
        },
        "agents": {
            "visual-critic": "@eyes",
            "ux-critic": "@eyes",
            "librarian": "aa.intelligence >= 30",
            "scraper": "claude-sonnet-5",
        },
        "toolsets": {
            "vision": ["screenshot", "review_ui", "measure_ui"],
            "browse": ["navigate", "@vision"],
        },
        "agent_tools": {"visual-critic": ["@browse", "get_page_state"]},
        "providers": {"claude": True, "codex": False},
    }))
    return path


def test_a_profile_is_written_once_and_worn_by_many(policy_file):
    """The ask in one line: "write the rules and apply them to multiple models".

    A profile is referenced with `@`, exactly as a toolset references another toolset — one
    sigil for "a name I defined above", so an unknown one is an error rather than being
    mistaken for a model id nobody has heard of.
    """
    p = Policy.load(policy_file)
    assert p.criterion_for("visual-critic") == "cap.vlm and price.in < 10"
    assert p.criterion_for("ux-critic") == p.criterion_for("visual-critic")


def test_an_agent_may_still_speak_for_itself(policy_file):
    """A profile is a convenience, never a cage: an inline criterion and a plain model id both
    still work, so one odd agent needs no profile invented for it."""
    p = Policy.load(policy_file)
    assert p.criterion_for("librarian") == "aa.intelligence >= 30"
    assert p.criterion_for("scraper") == "claude-sonnet-5"
    assert p.criterion_for("nobody-mentioned") is None


def test_a_profile_that_does_not_exist_is_caught_when_the_file_is_read(policy_file):
    """Not at 3am when that agent happens to spawn."""
    policy_file.write_text(json.dumps({"agents": {"a": "@ghost-profile"}}))
    with pytest.raises(PolicyError) as e:
        Policy.load(policy_file)
    assert "ghost-profile" in str(e.value)


def test_toolsets_expand_so_nobody_types_the_prefix_twice(policy_file):
    """"we don't have toolsets yet and are having to write interact__xxx" — a toolset is named
    once and expands to the server-prefixed tool names the vendor CLI wants."""
    p = Policy.load(policy_file)
    assert p.tools_for("visual-critic") == [
        "mcp__interact__navigate", "mcp__interact__screenshot",
        "mcp__interact__review_ui", "mcp__interact__measure_ui",
        "mcp__interact__get_page_state",
    ]


def test_a_toolset_may_include_another(policy_file):
    """"browse" wears "vision" — sets compose, or every set repeats its neighbours."""
    p = Policy.load(policy_file)
    assert "mcp__interact__screenshot" in p.expand_toolset("browse")


def test_a_toolset_cycle_is_refused_rather_than_hanging(policy_file):
    policy_file.write_text(json.dumps({"toolsets": {"a": ["@b"], "b": ["@a"]}}))
    with pytest.raises(PolicyError) as e:
        Policy.load(policy_file)
    assert "cycle" in str(e.value).lower()


def test_providers_can_be_switched_off_from_interact(policy_file):
    """"chose if we activate the agents or not for a provider (claude, codex, other...)"."""
    p = Policy.load(policy_file)
    assert p.provider_active("claude") is True
    assert p.provider_active("codex") is False
    # Anything not mentioned is ON: a provider you installed is a provider you meant to use.
    assert p.provider_active("gemini") is True


def test_acting_default_is_a_machine_local_shared_touch_intent(policy_file):
    raw = json.loads(policy_file.read_text())
    raw["defaults"] = {"model": "aa.intelligence >= 30", "touch": "workspace_write"}
    policy_file.write_text(json.dumps(raw))
    policy = Policy.load(policy_file)
    assert policy.default_touch_intent() == "workspace_write"


def test_a_missing_file_is_an_empty_policy_not_a_crash(tmp_path):
    p = Policy.load(tmp_path / "nothing.json")
    assert p.criterion_for("anyone") is None
    assert p.tools_for("anyone") == []
    assert p.provider_active("claude") is True


def test_switching_a_provider_off_and_on_round_trips(policy_file):
    """The setting is written back, so the extension's toggle and the CLI agree."""
    p = Policy.load(policy_file)
    p.set_provider_active("codex", True, path=policy_file)
    assert Policy.load(policy_file).provider_active("codex") is True
    # and the rest of the file survives the write
    assert Policy.load(policy_file).criterion_for("visual-critic") == "cap.vlm and price.in < 10"


def test_the_policy_reaches_the_spawn(policy_file, monkeypatch):
    """The seam that makes all three asks real: at spawn, an agent's profile decides its model,
    its toolset becomes the vendor's allow-list, and a switched-off provider refuses."""
    import interact.agents.run as run_mod
    from interact.agents.policy import Policy

    monkeypatch.setattr(run_mod, "load_policy", lambda: Policy.load(policy_file))

    # A profile decides the model without the agent naming one.
    assert run_mod.model_for_agent("visual-critic") == "cap.vlm and price.in < 10"
    assert run_mod.model_for_agent("nobody-mentioned") is None

    # A toolset becomes the vendor's allow-list, fully prefixed.
    tools = run_mod.tools_for_agent("visual-critic")
    assert "mcp__interact__screenshot" in tools and "mcp__interact__navigate" in tools

    # A provider switched off refuses to spawn at all, and says why.
    with pytest.raises(RuntimeError) as e:
        run_mod.check_provider_active("codex")
    assert "codex" in str(e.value) and "agents.json" in str(e.value)
    run_mod.check_provider_active("claude")  # active: no raise


def test_the_allow_list_reaches_the_vendor_command():
    """A toolset is only real if the vendor CLI is actually told about it."""
    from interact.agents.providers import provider_for

    claude = provider_for("claude")
    argv = claude.command("t", cwd=".", model=None, mcp_config=None, run_id="r",
                          agent="fixture", agent_prompt="Pinned fixture role",
                          allowed_tools=["mcp__interact__screenshot", "mcp__interact__navigate"])
    assert "--allowedTools" not in argv
    assert json.loads(argv[argv.index("--agents") + 1])["fixture"]["tools"] == [
        "mcp__interact__screenshot", "mcp__interact__navigate"]
    # No toolset configured: no flag at all, so an unrestricted agent stays unrestricted.
    assert "--allowedTools" not in claude.command(
        "t", cwd=".", model=None, mcp_config=None, run_id="r")


# ── the CLI surface: "from interact", not from a JSON file ─────────────────────────────────────

@pytest.fixture
def cli_policy(policy_file, monkeypatch):
    """Point every policy read/write at the fixture file."""
    import interact.agents.policy as pol
    monkeypatch.setattr(pol, "policy_path", lambda: policy_file)
    return policy_file


def test_variables_lists_every_comparison_with_its_source(capsys):
    """The discovery surface: you cannot write `aa.mmmu > 0.7` if nothing tells you it exists."""
    from interact.cli.app import agents_variables

    agents_variables()
    out = capsys.readouterr().out
    assert "aa.intelligence" in out and "gui.screenspot" in out and "price.in" in out
    assert "cap.vlm" in out
    assert "Artificial Analysis" in out, "each variable says who measured it"


def test_policy_shows_what_each_agent_resolves_to(cli_policy, capsys):
    from interact.cli.app import agents_policy

    agents_policy()
    out = capsys.readouterr().out
    assert "visual-critic" in out and "@eyes" in out and "cap.vlm and price.in < 10" in out
    assert "vision" in out and "mcp__interact__screenshot" in out
    assert "codex" in out and "off" in out
    # Preview gives one criteria order; local availability is evaluated at launch.
    line = next(l for l in out.splitlines() if l.strip().startswith("visual-critic"))
    assert "ranked:" in line and " → " in line and "gemini" not in line, line
    assert "availability checked at start" in line


def test_providers_toggle_from_the_cli(cli_policy, capsys):
    """"chose if we activate the agents or not for a provider" — one command, and it persists."""
    from interact.agents.policy import Policy
    from interact.cli.app import agents_providers

    agents_providers()                      # list
    out = capsys.readouterr().out
    assert "claude" in out and "on" in out and "codex" in out and "off" in out

    agents_providers("codex", "on")
    assert Policy.load(cli_policy).provider_active("codex") is True
    agents_providers("claude", "off")
    assert Policy.load(cli_policy).provider_active("claude") is False

    with pytest.raises(SystemExit):
        agents_providers("codex", "sideways")   # not on/off: refused, not guessed


# ── one file, for the panel and the spawn alike ────────────────────────────────────────────────

def test_the_policy_lives_beside_config_env_not_under_the_debug_dir(monkeypatch):
    """On a box that relocates its dumps (INTERACT_DEBUG_DIR), the policy must not move with
    them: the CLI found `<repo>/out/agents.json` while the panel wrote `~/.interact/agents.json`
    — one fact, two files, and a choice that never bit."""
    from interact.agents.policy import policy_path
    from interact.config import UserConfig

    monkeypatch.setenv("INTERACT_DEBUG_DIR", "/tmp/somewhere/else/out")
    assert policy_path() == UserConfig.PATH.parent / "agents.json"


def test_a_profile_may_be_named_wherever_a_model_is(policy_file, monkeypatch):
    """`--model @eyes` on the CLI, `@eyes` chosen in the panel: a profile is a NAME for a model
    rule, so it works everywhere a model id does — resolved to a real model before the vendor
    CLI sees it, and an unknown one refused by name."""
    import interact.agents.run as run_mod
    from interact.agents.policy import Policy

    monkeypatch.setattr(run_mod, "load_policy", lambda: Policy.load(policy_file))
    _, via_profile = run_mod.resolve_model("@eyes", {}, available_only=False)
    _, via_rule = run_mod.resolve_model("cap.vlm and price.in < 10", {}, available_only=False)
    assert via_profile is not None and not via_profile.startswith("@")
    assert via_profile == via_rule, "a profile resolves to exactly what its rule resolves to"
    with pytest.raises(Exception) as e:
        run_mod.resolve_model("@ghost", {}, available_only=False)
    assert "ghost" in str(e.value) and "eyes" in str(e.value), "name the typo AND what exists"


def test_a_broken_policy_is_never_overwritten_by_a_toggle(tmp_path):
    """The file holds profiles and toolsets somebody typed. One missing comma must not let a
    provider switch flatten it to `{"providers": {...}}` — refuse, and name the file to fix."""
    path = tmp_path / "agents.json"
    path.write_text("{ not json at all")
    with pytest.raises(PolicyError) as e:
        Policy().set_provider_active("codex", False, path)
    assert str(path) in str(e.value)
    assert path.read_text() == "{ not json at all", "their broken file is theirs to fix"


# ── Coarse tool-policy acceptance: a CLI that cannot enforce a role's tool list ─────────────
# Codex has sandbox scope (read-only/workspace-write) and a whole-MCP-server on/off toggle, but no
# per-tool allow/deny list the way Claude's `--agents`/`--allowedTools` has. Unmentioned must
# refuse — the same "never silently loosen" posture `provider_active` already applies to whether a
# vendor runs at all — and an operator names the tradeoff explicitly, per role, per provider.


def test_a_role_with_no_coarse_acceptance_is_not_accepted(policy_file):
    p = Policy.load(policy_file)
    assert p.accepts_coarse_tool_policy("visual-critic", "codex") is False


def test_a_named_role_provider_pair_is_accepted(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"coarse_tool_policy": {"tester": ["codex"]}}))
    p = Policy.load(path)
    assert p.accepts_coarse_tool_policy("tester", "codex") is True
    assert p.accepts_coarse_tool_policy("tester", "claude") is False, \
        "Claude enforces the real list; accepting a coarse substitute for it is never asked"
    assert p.accepts_coarse_tool_policy("visual-critic", "codex") is False, \
        "acceptance is per ROLE — one role's tradeoff must never widen a sibling's"


def test_the_cli_says_one_line_when_nothing_clears_a_criterion(monkeypatch, capsys):
    """`interact agents spawn --model "aa.intelligence > 999"` printed a Python traceback where
    every other interact failure prints one actionable ERROR line — and the model was resolved
    AFTER the vendor argv had been built, so the binary would have been handed the raw text."""
    import interact.agents.providers as providers
    from interact.cli.app import agents_spawn

    monkeypatch.setattr(providers.ClaudeCodeProvider, "available", lambda self: True)
    # This test isolates criterion failure, with the caller's conversation already known.
    monkeypatch.setenv("INTERACT_SESSION_ID", "fixture-criterion-session")

    class _Vendor:
        name = "claude"
        native_providers = frozenset({"anthropic"})

        def available(self):
            return True

        def can_run(self, model, env):
            return model.provider in self.native_providers

        def model_id_for(self, model):
            return model.id

        def command(self, *a, **k):
            raise AssertionError("nothing clears the bar — the vendor CLI must never be reached")

    monkeypatch.setattr(providers, "provider_for", lambda name: _Vendor())
    use_policy(monkeypatch, agents={"tester": "aa.intelligence > 999"})
    with pytest.raises(SystemExit) as stop:
        agents_spawn("say hi", agent="tester")
    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("ERROR:") and "999" in err and "Traceback" not in err


def test_agents_models_ranks_what_can_be_compared(capsys, monkeypatch):
    """A picker can only be a comparison if something ranks the candidates. This lists the models
    the REGISTRY scores — id, score, rank — newest measure first, as JSON for the panel."""
    import json as _json

    from interact.cli.app import agents_models
    from interact.models import Model

    with catalog_of(
        Model(id="big", provider="anthropic", capabilities=set(), intelligence_score=60.2),
        Model(id="mid", provider="anthropic", capabilities=set(), intelligence_score=37.1),
        Model(id="unmeasured", provider="anthropic", capabilities=set()),
    ):
        agents_models(json_out=True)
    rows = _json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["big", "mid"], "scored only, best first"
    assert rows[0]["score"] == 60.2 and rows[0]["competence"] == "aa.intelligence 60.2 · 1st of 2"


def test_agents_models_lists_each_model_once_not_once_per_provider_alias(capsys, monkeypatch):
    """40 rows carried 4 facts: `azure/gpt-5.5`, `azure/gpt-5.5-2026-04-23`, `vertex_ai/…` are the
    same model wearing provider prefixes, and eight rows of one score compare nothing. One row per
    distinct model, the plainest id, its aliases counted; the rank is over DISTINCT models."""
    import json as _json

    from interact.cli.app import agents_models
    from interact.models import Model

    with catalog_of(
        Model(id="azure/gpt-5.5", provider="azure", capabilities=set(), intelligence_score=60.2),
        Model(id="gpt-5.5", provider="openai", capabilities=set(), intelligence_score=60.2),
        Model(id="azure_ai/gpt-5.5-2026-04-23", provider="azure", capabilities=set(), intelligence_score=60.2),
        Model(id="us.anthropic.claude-opus-4-7", provider="bedrock", capabilities=set(), intelligence_score=57.3),
        Model(id="au.anthropic.claude-opus-4-7", provider="bedrock", capabilities=set(), intelligence_score=57.3),
        Model(id="vertex_ai/claude-opus-4-7@default", provider="vertex", capabilities=set(), intelligence_score=57.3),
        Model(id="openrouter/anthropic/claude-opus-4.7", provider="openrouter", capabilities=set(), intelligence_score=57.3),
        Model(id="claude-opus-4-7", provider="anthropic", capabilities=set(), intelligence_score=57.3),
    ):
        agents_models(json_out=True)
    rows = _json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["gpt-5.5", "claude-opus-4-7"], rows
    assert [r["aliases"] for r in rows] == [2, 4], "a region, an @revision and a dotted version are the same model"
    assert rows[1]["competence"] == "aa.intelligence 57.3 · 2nd of 2"


def test_bare_model_name_collapses_the_serving_tags_a_reseller_appends():
    """Round 16 swept the picker and still found eight duplicate PAIRS: one score, two rows, no
    comparison. Each pair differed only by something a reseller appends — a hosting tag
    (`:cloud`), a moving pointer (`-latest`), a Bedrock revision (`-v1`), a maturity label
    (`-preview`) or a mid-id MMDD revision (`-0309-`) — never by the model."""
    from interact.cli.app import _bare_model_name

    pairs = [
        ("moonshot/kimi-k2.6", "ollama/kimi-k2.6:cloud"),
        ("xai/grok-4.3", "xai/grok-4.3-latest"),
        ("azure/gpt-5.2-chat", "gpt-5.2-chat-latest"),
        ("xai/grok-4.20-0309-reasoning", "vertex_ai/xai/grok-4.20-reasoning"),
        ("gemini-3-pro-preview", "replicate/google/gemini-3-pro"),
        ("azure/gpt-5.1-chat", "gpt-5.1-chat-latest"),
        ("azure_ai/kimi-k2.5", "ollama/kimi-k2.5:cloud"),
        ("claude-opus-4-6", "anthropic.claude-opus-4-6-v1"),
        # A maturity label sits MID-id too, and left four grok rows sharing one score.
        ("xai/grok-4.20-0309-reasoning", "xai/grok-4.20-beta-0309-reasoning"),
    ]
    for left, right in pairs:
        assert _bare_model_name(left) == _bare_model_name(right), f"{left} and {right} are one model"
    # A four-digit run that is NOT a month keeps its meaning: only 01-12 reads as a revision.
    assert _bare_model_name("qwen-3-4096-instruct") != _bare_model_name("qwen-3-instruct")
    # What genuinely NAMES a different model is never stripped: reasoning is not non-reasoning,
    # a multi-agent build is its own thing, and an image model is not its text sibling.
    assert _bare_model_name("xai/grok-4.20-beta-0309-reasoning") != _bare_model_name("xai/grok-4.20-beta-0309-non-reasoning")
    assert _bare_model_name("xai/grok-4.20-multi-agent-beta-0309") != _bare_model_name("xai/grok-4.20-0309-reasoning")
    assert _bare_model_name("gemini-3-pro-image-preview") != _bare_model_name("gemini-3-pro-preview")


def test_agents_models_scores_the_alias_a_picker_offers(capsys):
    """The blocking round-16 finding: the rows a picker puts NEAREST THE TOP — Claude Code's
    `sonnet` / `opus` tier aliases and whatever the agent runs on today — carried no score, so the
    one comparison anyone makes (keep the incumbent, or switch) could not be made. A named alias is
    resolved to the best-scored model it stands for, and comes back carrying that model's score."""
    import json as _json

    from interact.cli.app import agents_models
    from interact.models import Model

    with catalog_of(
        Model(id="gpt-5.5", provider="openai", capabilities=set(), intelligence_score=60.2),
        Model(id="claude-opus-4-7", provider="anthropic", capabilities=set(), intelligence_score=57.3),
        Model(id="claude-sonnet-5", provider="anthropic", capabilities=set(), intelligence_score=44.4),
        Model(id="claude-haiku-4-5", provider="anthropic", capabilities=set(), intelligence_score=31.0),
    ):
        agents_models(json_out=True, limit=1, names=["sonnet", "opus", "claude-sonnet-5", "nonesuch"])
    rows = _json.loads(capsys.readouterr().out)
    asked = {r["asked"]: r for r in rows if r.get("asked")}
    assert asked["sonnet"]["id"] == "claude-sonnet-5", "the tier stands for the model it aliases"
    assert asked["sonnet"]["competence"] == "aa.intelligence 44.4 · 3rd of 4"
    assert asked["opus"]["id"] == "claude-opus-4-7"
    assert asked["claude-sonnet-5"]["id"] == "claude-sonnet-5", "the incumbent resolves to itself"
    assert "nonesuch" not in asked, "an alias nothing scores is simply absent, never invented"
    assert rows[0]["id"] == "gpt-5.5" and not rows[0].get("asked"), "the ranked list still leads"


def test_agents_models_ranks_by_competition_not_by_list_position(capsys):
    """Round 17, blocking: 34 of 43 scored rows sat in a tie group printing DIFFERENT ranks —
    `gpt-5.5` 1st and `gpt-5.5-pro` 2nd, both 60.2, adjacent on screen. Enumerating the list
    manufactures exactly the difference the measure denies. Equal scores share one rank, and a
    shared rank says so rather than pretending to break the tie."""
    import json as _json

    from interact.cli.app import agents_models
    from interact.models import Model

    with catalog_of(
        Model(id="alpha", provider="a", capabilities=set(), intelligence_score=60.2),
        Model(id="bravo", provider="b", capabilities=set(), intelligence_score=60.2),
        Model(id="charlie", provider="c", capabilities=set(), intelligence_score=44.6),
    ):
        agents_models(json_out=True)
    rows = {r["id"]: r for r in _json.loads(capsys.readouterr().out)}
    assert rows["alpha"]["competence"] == "aa.intelligence 60.2 · joint 1st of 3"
    assert rows["bravo"]["competence"] == rows["alpha"]["competence"], "one score is one rank"
    assert rows["charlie"]["competence"] == "aa.intelligence 44.6 · 3rd of 3", "the tie consumed two places"


def test_agents_models_names_the_aliases_it_absorbed(capsys):
    """Round 17, blocking: the dedupe ran in the scored section only, so the browse list beneath it
    still offered `openai/gpt-5.5` as "not scored" directly under `gpt-5.5` at "60.2 · 1st" — the
    picker contradicting itself about its own top model. Each row names the ids it absorbed, so a
    second list can drop what is already ranked above it."""
    import json as _json

    from interact.cli.app import agents_models
    from interact.models import Model

    with catalog_of(
        Model(id="gpt-5.5", provider="openai", capabilities=set(), intelligence_score=60.2),
        Model(id="azure/gpt-5.5", provider="azure", capabilities=set(), intelligence_score=60.2),
        Model(id="openai/gpt-5.5-latest", provider="openai", capabilities=set(), intelligence_score=60.2),
    ):
        agents_models(json_out=True)
    row = _json.loads(capsys.readouterr().out)[0]
    assert row["id"] == "gpt-5.5"
    assert sorted(row["covers"]) == ["azure/gpt-5.5", "openai/gpt-5.5-latest"], "every absorbed id, named"
    assert row["aliases"] == 2


def test_agents_models_falls_back_to_the_closest_ranked_relative(capsys):
    """Round 19, blocking: on the real fleet the incumbent scored for 0 of 45 agents — every agent
    runs `inherit`, `claude-sonnet-5` or a local model, and the registry (162 scored) holds none of
    them. The registry is simply OLDER than the fleet. A name it cannot rank exactly resolves to
    its closest ranked relative, flagged as approximate so the surface never passes it off as the
    model itself; a name sharing too little resolves to nothing at all."""
    import json as _json

    from interact.cli.app import agents_models
    from interact.models import Model

    with catalog_of(
        Model(id="gpt-5.5", provider="openai", capabilities=set(), intelligence_score=60.2),
        Model(id="claude-opus-4-7", provider="anthropic", capabilities=set(), intelligence_score=57.3),
        Model(id="claude-sonnet-4-6", provider="anthropic", capabilities=set(), intelligence_score=44.4),
    ):
        agents_models(json_out=True, names=["claude-sonnet-5", "claude-opus-4-7", "ollama/deepseek-v4-pro:cloud"])
    asked = {r["asked"]: r for r in _json.loads(capsys.readouterr().out) if r.get("asked")}
    assert asked["claude-sonnet-5"]["id"] == "claude-sonnet-4-6", "its closest ranked relative"
    assert asked["claude-sonnet-5"]["approximate"] is True, "never passed off as the model itself"
    assert not asked["claude-opus-4-7"].get("approximate"), "an exact hit is exact"
    assert "ollama/deepseek-v4-pro:cloud" not in asked, "sharing nothing resolves to nothing"


def test_rank_is_over_the_board_not_over_what_this_machine_can_reach(capsys, monkeypatch, tmp_path):
    """Round 25: the same model ranked 10th from one directory and 13th from another, because the
    denominator was "models you can route to" — a credential-scope measure wearing an intelligence
    label. A rank only means something against a FIXED population, so it is taken over the board
    itself; which models you happen to be able to reach changes what is LISTED, never the place."""
    import json as _json

    from interact import model_catalog as mcat
    from interact.cli.app import agents_models
    from interact.models import Model

    board = tmp_path / "board.json"
    board.write_text(_json.dumps({"scores": [
        {"name": "Alpha One", "intelligence": 90.0},
        {"name": "Beta Two", "intelligence": 80.0},
        {"name": "Gamma Three", "intelligence": 70.0},
        {"name": "Delta Four", "intelligence": 60.0},
    ]}))
    monkeypatch.setattr(mcat, "leaderboard_path", lambda: board)
    # This machine reaches only two of the four the board measures.
    with catalog_of(
        Model(id="beta-two", provider="x", capabilities=set(), intelligence_score=80.0),
        Model(id="delta-four", provider="y", capabilities=set(), intelligence_score=60.0),
    ):
        agents_models(json_out=True)
    rows = {r["id"]: r for r in _json.loads(capsys.readouterr().out)}
    assert rows["beta-two"]["competence"] == "aa.intelligence 80.0 · 2nd of 4", "its place on the board"
    assert rows["delta-four"]["competence"] == "aa.intelligence 60.0 · 4th of 4", (
        "not '2nd of 2' — reaching fewer models never promotes one"
    )


def test_the_ranking_scores_every_model_the_board_measures(capsys, monkeypatch, tmp_path):
    """Round 26, blocking: the RANK came from the board while the SCORE came from the registry,
    and the registry held none of the board's top models. The result inverted the ask — the most
    competent models were exactly the ones printed "nothing scored carries this name", and every
    number on screen was an approximation of a superseded model. A model this machine can reach is
    scored by the board directly, wherever its id is known from."""
    import json as _json

    from interact import model_catalog as mcat
    from interact.cli.app import agents_models
    from interact.models import Model

    board = tmp_path / "board.json"
    board.write_text(_json.dumps({"scores": [
        {"name": "Claude Fable 5.1 (Max Effort)", "intelligence": 53.4},
        {"name": "Claude Opus 5", "intelligence": 50.7},
        {"name": "Claude Opus 4.7", "intelligence": 40.7},
    ]}))
    monkeypatch.setattr(mcat, "leaderboard_path", lambda: board)
    # The browse catalogue reaches two models the REGISTRY never heard of — including the board's
    # leader, which the panel was calling unscored.
    monkeypatch.setattr(mcat, "load_catalog", lambda **_: mcat.Catalog(
        models=[
            mcat.ModelInfo(id="anthropic/claude-fable-5.1", name="Claude Fable 5.1"),
            mcat.ModelInfo(id="anthropic/claude-opus-5", name="Claude Opus 5"),
        ],
        source="openrouter", fetched_at=1.0,
    ))
    with catalog_of(Model(id="claude-opus-4-7", provider="anthropic", capabilities=set())):
        agents_models(json_out=True, names=["fable"])
    rows = _json.loads(capsys.readouterr().out)
    by_id = {r["id"]: r for r in rows if not r.get("asked")}
    assert "anthropic/claude-fable-5.1" in by_id, "the board's leader is reachable and must be ranked"
    assert by_id["anthropic/claude-fable-5.1"]["competence"] == "aa.intelligence 53.4 · 1st of 3"
    assert by_id["anthropic/claude-opus-5"]["score"] == 50.7
    assert by_id["claude-opus-4-7"]["score"] == 40.7, "a registry model is still ranked"
    asked = {r["asked"]: r for r in rows if r.get("asked")}
    assert asked["fable"]["id"] == "anthropic/claude-fable-5.1", "and a tier alias finds it"
    assert not asked["fable"].get("approximate"), "an exact family match is not an approximation"


def test_a_different_version_is_never_passed_off_as_the_one_asked_for(capsys, monkeypatch, tmp_path):
    """Round 27, blocking: an agent that ran on `claude-fable-5` was shown `claude-fable-5.1`'s
    number — a different model, four places higher — with nothing marking the substitution, because
    the asked-for name is a SUBSTRING of the other. A bare family word like `sonnet` legitimately
    stands for whatever wears it; a name carrying its own VERSION does not, and a swap there is an
    approximation that must say so."""
    import json as _json

    from interact import model_catalog as mcat
    from interact.cli.app import agents_models
    from interact.models import Model

    board = tmp_path / "board.json"
    board.write_text(_json.dumps({"scores": [
        {"name": "Claude Fable 5.1", "intelligence": 53.4},
        {"name": "Claude Fable 5", "intelligence": 49.7},
        {"name": "Claude Sonnet 5", "intelligence": 38.4},
    ]}))
    monkeypatch.setattr(mcat, "leaderboard_path", lambda: board)
    monkeypatch.setattr(mcat, "load_catalog", lambda **_: mcat.Catalog(models=[], source="x", fetched_at=1.0))
    with catalog_of(
        Model(id="anthropic/claude-fable-5.1", provider="anthropic", capabilities=set()),
        Model(id="anthropic/claude-fable-5", provider="anthropic", capabilities=set()),
        Model(id="anthropic/claude-sonnet-5", provider="anthropic", capabilities=set()),
    ):
        agents_models(json_out=True, names=["claude-fable-5", "sonnet", "claude-fable-5.1"])
    asked = {r["asked"]: r for r in _json.loads(capsys.readouterr().out) if r.get("asked")}
    assert asked["claude-fable-5"]["id"] == "anthropic/claude-fable-5", "its own version, exactly"
    assert not asked["claude-fable-5"].get("approximate")
    assert asked["claude-fable-5.1"]["score"] == 53.4
    assert asked["sonnet"]["id"] == "anthropic/claude-sonnet-5", "a family word stands for its model"
    assert not asked["sonnet"].get("approximate"), "no version asked for, so nothing was substituted"


def test_a_criterion_can_be_asked_what_it_picks_today(capsys, monkeypatch, tmp_path):
    """His words: "we shouldn't write a model, but resolve a model from the constraints". Nothing
    could ask a criterion what it currently picks, so every surface that helped choose could only
    offer a PINNED id — the frozen claim the criteria language exists to replace. This answers it,
    and refuses loudly rather than falling back, because a criterion that quietly resolves to some
    other model looks like it worked."""
    import json as _json

    from interact import model_catalog as mcat
    from interact.cli.app import agents_criterion
    from interact.models import Model

    board = tmp_path / "board.json"
    board.write_text(_json.dumps({"scores": [
        {"name": "Big One", "intelligence": 60.0},
        {"name": "Mid One", "intelligence": 40.0},
    ]}))
    monkeypatch.setattr(mcat, "leaderboard_path", lambda: board)
    monkeypatch.setattr("interact.agents.policy.policy_path", lambda: tmp_path / "agents.json")
    monkeypatch.setenv("FIXTURE_KEY", "set")
    with catalog_of(
        Model(id="big-one", provider="x", capabilities=set(), intelligence_score=60.0,
              input_cost_per_million=30.0),
        Model(id="mid-one", provider="x", capabilities=set(), intelligence_score=40.0,
              input_cost_per_million=1.0),
    ):
        # A model nobody can reach is not a candidate, so the fixture's provider gets a key —
        # set INSIDE the catalog, which resets the registry as it enters.
        Model._provider_keys["x"] = ["FIXTURE_KEY"]
        agents_criterion("aa.intelligence > 35", json_out=True)
        picked = _json.loads(capsys.readouterr().out)
        assert picked["model"] == "mid-one", "cheapest that clears the bar, not the highest score"
        assert picked["criterion"] == "aa.intelligence > 35"

        agents_criterion("aa.intelligence > 99", json_out=True)
        none = _json.loads(capsys.readouterr().out)
        assert none["model"] is None, "nothing qualifying REFUSES, never falls back"
        assert none["why"], "and says which term excluded everyone"


def test_the_policy_says_which_rule_governs_an_agent_and_what_it_means_today(
    capsys, monkeypatch, tmp_path,
):
    """A panel showing a bare `sonnet` cannot say whether that was RESOLVED or merely typed.

    "we shouldn't write a model, but resolve a model from the constraints". The difference has to
    be VISIBLE where the model is shown, or a pin and a resolution look identical and the whole
    point is invisible — so the policy answers, per agent, the rule as written, whether that rule
    is a criterion at all, and the model it comes out as right now.
    """
    from interact import model_catalog as mcat
    from interact.cli.app import agents_policy
    from interact.models import Model

    board = tmp_path / "board.json"
    board.write_text(json.dumps({"scores": [{"name": "Mid One", "intelligence": 40.0}]}))
    monkeypatch.setattr(mcat, "leaderboard_path", lambda: board)
    monkeypatch.setenv("FIXTURE_KEY", "set")
    policy = tmp_path / "agents.json"
    policy.write_text(json.dumps({"agents": {
        "code-reviewer": "aa.intelligence > 35",
        "web-researcher": "mid-one",
    }}))
    monkeypatch.setattr("interact.agents.policy.policy_path", lambda: policy)

    with catalog_of(
        Model(id="mid-one", provider="anthropic", capabilities=set(), intelligence_score=40.0,
              input_cost_per_million=1.0),
    ):
        Model._provider_keys["anthropic"] = ["FIXTURE_KEY"]
        agents_policy(json_out=True)
        seen = {a["name"]: a for a in json.loads(capsys.readouterr().out)["agents"]}

    assert seen["code-reviewer"]["criterion"] is True, "a rule with a bar is a criterion"
    assert seen["code-reviewer"]["resolves"] == "mid-one", "and it says what that means today"
    assert seen["web-researcher"]["criterion"] is False, "a bare id is a pin, and says so"
    assert seen["web-researcher"]["resolves"] == "mid-one"


def test_a_criterion_answers_PER_VENDOR_CLI_because_that_is_who_will_run_it(
    capsys, monkeypatch, tmp_path,
):
    """One criterion, two answers — and a preview naming a model the CLI cannot run is a lie.

    A spawn resolves inside what THAT binary can be pointed at: its own vendor's models through
    its login. So "what does this criterion pick" has no single answer, and a picker previewing
    the catalog-wide winner would offer a rule that refuses the moment it is used.
    """
    from interact import model_catalog as mcat
    from interact.cli.app import agents_criterion
    from interact.models import Model

    board = tmp_path / "board.json"
    board.write_text(json.dumps({"scores": [{"name": "Only One", "intelligence": 50.0}]}))
    monkeypatch.setattr(mcat, "leaderboard_path", lambda: board)
    monkeypatch.setattr("interact.agents.policy.policy_path", lambda: tmp_path / "agents.json")

    class OneVendor:
        name = "onlyvendor"

        def can_run(self, model, env):
            return model.provider == "onlyvendor"

        def model_id_for(self, model):
            return model.id

    monkeypatch.setattr("interact.agents.providers.PROVIDERS", {"onlyvendor": OneVendor()})
    monkeypatch.setattr("interact.agents.providers.provider_for", lambda _n: OneVendor())
    monkeypatch.setenv("FIXTURE_KEY", "set")
    with catalog_of(
        Model(id="cheap-outsider", provider="other", capabilities=set(), intelligence_score=50.0,
              input_cost_per_million=0.1),
        Model(id="vendors-own", provider="onlyvendor", capabilities=set(), intelligence_score=50.0,
              input_cost_per_million=9.0),
    ):
        Model._provider_keys["other"] = ["FIXTURE_KEY"]
        Model._provider_keys["onlyvendor"] = ["FIXTURE_KEY"]
        agents_criterion("aa.intelligence >= 40", json_out=True)
        answer = json.loads(capsys.readouterr().out)

    assert answer["model"] == "cheap-outsider", "catalog-wide, thrift still wins"
    assert answer["providers"] == {"onlyvendor": "vendors-own"}, (
        "but the CLI that will actually run it can only reach its own"
    )
