"""CLI surfaces tell ONE story about model selection: `status`/`providers`/`doctor` all print the
RESOLVED model per role (not an opaque 'auto'), plus the sovereign quality tier. A grep of
availability isn't the answer; resolution (frontier-first, first-available) is. The shared helper
is exercised here once; the three commands just call it."""

import importlib
import json

import pytest

from galaius import cli
from galaius.agents.providers import ClaudeCodeProvider, CodexProvider, MEDIA_PROVIDERS
from galaius.config import Config, UserConfig
from galaius.models import Model
import galaius.ollama as ollama
from galaius.runtime import config as runtime_config
from galaius.server.tools_meta import list_providers


@pytest.fixture
def _only_zai(monkeypatch):
    # ONLY the z.ai GLM reachable → resolution is deterministic without real keys.
    monkeypatch.setattr("galaius.models.Model.is_available", lambda self: self.id == "zai/glm-4.5v")


def test_resolved_models_helper_names_every_task_with_its_role_knob(_only_zai, capsys):
    cli._print_resolved_models()
    out = capsys.readouterr().out
    # the view is TASK-centric — each thing a user does is named...
    for task in ("screenshot", "get_interactive_elements", "transcribe", "review_ui", "measure_ui"):
        assert task in out
    # ...with the config knob (role) in brackets so it's clear what to pin
    for role in ("[image]", "[component]", "[video]", "[audio]"):
        assert role in out
    # review_ui/verify_ui resolve by STAKES to the sovereign GLM the z.ai key lights up at low/medium
    assert "zai/glm-4.5v" in out and "low/medium tier" in out


def test_resolved_models_helper_flags_a_missing_key(monkeypatch, capsys):
    monkeypatch.setattr("galaius.models.Model.is_available", lambda self: False)  # nothing reachable
    cli._print_resolved_models()
    out = capsys.readouterr().out
    assert "⚠" in out and "no model available" in out  # nothing resolving is called out, never silent
    assert "fall back to frontier" in out   # low/medium honestly report the absence of a sovereign key


def test_providers_includes_the_resolved_selection(_only_zai, capsys):
    cli.providers()
    out = capsys.readouterr().out
    assert "Resolved selection" in out and "zai/glm-4.5v" in out


def test_media_transport_status_is_registry_derived_and_actionable(monkeypatch, capsys):
    app_module = importlib.import_module("galaius.cli.app")
    monkeypatch.setattr(ClaudeCodeProvider, "available", lambda self: True)
    monkeypatch.setattr(CodexProvider, "available", lambda self: False)
    config = Config(
        media_backend="auto",
        media_billing="session_only",
        media_provider_order=tuple(MEDIA_PROVIDERS),
    )

    app_module._print_media_transport(config)

    output = capsys.readouterr().out
    assert "backend=auto" in output and "billing=session_only" in output
    assert " → ".join(MEDIA_PROVIDERS) in output
    assert "claude" in output and "installed" in output
    assert "codex" not in output.lower()
    assert "no metered API fallback" in output


@pytest.mark.asyncio
async def test_list_providers_reports_subscription_auth_without_credentials(monkeypatch):
    async def authenticated(self, env, *, timeout=10):
        assert not any("KEY" in name or "TOKEN" in name for name in env)
        return self.name == "claude"

    monkeypatch.setattr(ClaudeCodeProvider, "available", lambda self: True)
    monkeypatch.setattr(ClaudeCodeProvider, "subscription_authenticated", authenticated)
    monkeypatch.setattr(Model, "load_registry", lambda: None)
    monkeypatch.setattr(Model, "live_providers", lambda: set())
    monkeypatch.setattr(ollama, "serving", lambda: [])
    monkeypatch.setattr(runtime_config, "media_backend", "auto")
    monkeypatch.setattr(runtime_config, "media_billing", "session_only")

    payload = json.loads(await list_providers())

    media = payload["media"]
    assert media["backend"] == "auto" and media["billing"] == "session_only"
    assert media["provider_order"] == list(MEDIA_PROVIDERS)
    by_name = {item["provider"]: item for item in media["subscription_providers"]}
    assert by_name["claude"]["authenticated"] is True
    assert set(by_name) == {"claude"}
    assert "credential" not in json.dumps(payload).lower()


@pytest.mark.asyncio
async def test_list_providers_refreshes_a_live_config_file_edit(
    monkeypatch, tmp_path
) -> None:
    config_file = tmp_path / "config.env"
    monkeypatch.setattr(UserConfig, "PATH", config_file)
    monkeypatch.setattr(Model, "load_registry", lambda: None)
    monkeypatch.setattr(Model, "live_providers", lambda: set())
    monkeypatch.setattr(ollama, "serving", lambda: [])
    monkeypatch.setattr(ClaudeCodeProvider, "available", lambda self: False)
    monkeypatch.setattr(CodexProvider, "available", lambda self: False)
    runtime_config.clear_overrides()
    baseline = json.loads(await list_providers())["media"]
    UserConfig.set("GALAIUS_MEDIA_BACKEND", "session")
    UserConfig.set("GALAIUS_MEDIA_BILLING", "session_only")
    UserConfig.set("GALAIUS_MEDIA_PROVIDER_ORDER", "claude")
    UserConfig.set("GALAIUS_MEDIA_SESSION_NO_EXTRA_USAGE_CONFIRMED_FOR", "claude")

    edited = json.loads(await list_providers())["media"]
    config_file.unlink()
    restored = json.loads(await list_providers())["media"]

    assert edited["backend"] == "session" and edited["billing"] == "session_only"
    assert edited["provider_order"] == ["claude"]
    assert restored["backend"] == baseline["backend"]
    assert restored["billing"] == baseline["billing"]


# --- `agents modes`: the autonomy choices, machine-readably ---------------------------------


def test_modes_prints_one_tab_separated_row_per_mode(capsys):
    from galaius.cli.app import agents_modes

    from galaius.agents.providers import ClaudeCodeProvider

    agents_modes(provider="claude")
    rows = [r for r in capsys.readouterr().out.splitlines() if r]
    # Against the provider's own list, not a hardcoded 6 — a count copied from a collection in
    # hand is a second source of truth that only ever goes stale.
    assert len(rows) == len(ClaudeCodeProvider().permission_modes())
    for row in rows:
        ident, label, detail, unrestricted = row.split("\t")
        assert ident and label and detail
        assert unrestricted in ("yes", "no")


def test_the_unrestricted_mode_is_flagged_in_the_machine_output(capsys):
    """The panel has to mark it. A flag it must infer from wording would break the day the
    wording changes — the same failure that made `agents definitions` exist."""
    from galaius.cli.app import agents_modes

    agents_modes(provider="claude")
    flagged = [r.split("\t")[0] for r in capsys.readouterr().out.splitlines()
               if r.endswith("\tyes")]
    assert flagged == ["bypassPermissions"]


def test_a_provider_with_no_verified_modes_prints_nothing(capsys, monkeypatch):
    """Not a line of prose: a caller splitting on newlines would read it as a mode."""
    from galaius.cli.app import agents_modes

    monkeypatch.setattr(CodexProvider, "permission_modes", lambda self: ())
    agents_modes(provider="codex")
    assert capsys.readouterr().out == ""


# --- `agents discovered`: the sessions galaius did NOT start, machine-readably ---------------
#
# The VS Code panel reads the registry DIRECTLY off disk, and a foreign session has no record on
# disk — it is discovered at list time. So the panel could never show one, despite carrying an
# icon and a tooltip for them, and "i have agents in the sheets folder elsewhere, and i can't
# change and see how they work" was literally true: the panel had no way to learn they existed.


def test_discovered_prints_one_json_object_per_line(monkeypatch, capsys):
    import json

    from galaius.agents import registry as reg
    from galaius.cli.app import agents_discovered

    monkeypatch.setattr(reg, "_discover_foreign", lambda: [
        {"sessionId": "s-1", "name": "sheets-ab", "cwd": "/workspace/xp/sheets",
         "kind": "interactive", "pid": 4242},
    ])
    agents_discovered()
    rows = [json.loads(r) for r in capsys.readouterr().out.splitlines() if r.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == "s-1" and row["name"] == "sheets-ab"
    assert row["foreign"] is True and row["status"] == "foreign"
    assert row["cwd"] == "/workspace/xp/sheets"


def test_discovered_files_each_session_under_its_project(capsys, monkeypatch, tmp_path):
    """The whole point: the panel groups by project, so a session with no project is unreachable
    by folder however visible it is in a flat list."""
    import json

    from galaius.agents import registry as reg
    from galaius.cli.app import agents_discovered

    sheets = tmp_path / "xp" / "sheets"
    (sheets / ".git").mkdir(parents=True)
    monkeypatch.setattr(reg, "_discover_foreign", lambda: [
        {"sessionId": "s-1", "name": "sheets-ab", "cwd": str(sheets), "kind": "interactive"},
    ])
    agents_discovered()
    assert json.loads(capsys.readouterr().out.strip())["project"] == "sheets"


def test_discovered_prints_nothing_when_there_are_none(capsys, monkeypatch):
    """Not a line of prose — the caller parses each line as JSON."""
    from galaius.agents import registry as reg
    from galaius.cli.app import agents_discovered

    monkeypatch.setattr(reg, "_discover_foreign", lambda: [])
    agents_discovered()
    assert capsys.readouterr().out == ""


def test_a_broken_discovery_is_LOUD_rather_than_an_empty_answer(capsys, monkeypatch):
    """The first version of this test certified the swallow: it asserted stdout was empty and
    called that "surviving". But an empty stdout is what "you have no other sessions" looks like,
    so a real bug here would cache as a plausible answer and re-cache every refresh forever, with
    nothing anywhere saying why. Each provider already swallows its own failure — reaching this
    handler at all means something is actually wrong, which is exactly what must be said."""
    import pytest as _pytest

    from galaius.agents import registry as reg
    from galaius.cli.app import agents_discovered

    def boom():
        raise OSError("no such directory")

    monkeypatch.setattr(reg, "_discover_foreign", boom)
    with _pytest.raises(SystemExit) as exit_info:
        agents_discovered()
    assert exit_info.value.code != 0, "a failure must not exit 0"
    captured = capsys.readouterr()
    assert captured.out == "", "stdout is the data channel — it stays parseable or empty"
    assert "ERROR" in captured.err and "no such directory" in captured.err


def test_the_permission_flag_spelling_is_pinned_end_to_end(monkeypatch):
    """The panel builds `--permission-mode <id>` as a literal string (org.ts `spawnArgs`) and the
    CLI receives it as a cyclopts parameter named `permission_mode`. Nothing pinned the two
    together: rename either and the spawn silently falls back to the CLI's own default while the
    panel tells the user it chose "plan". For a SAFETY control, silently doing something other
    than what the UI reported is the worst available failure.
    """
    import importlib

    # `galaius.cli` re-exports the cyclopts `app` object, and that attribute shadows the
    # submodule — the same trap test_agent_run.py already documents.
    cli = importlib.import_module("galaius.cli.app")

    got = {}

    class _Handle:
        run_id = "r-1"

        async def wait(self):
            return 0

    async def _spawn(provider, task, **kwargs):
        got.update(kwargs)
        return _Handle()

    monkeypatch.setattr(cli, "_run_agent_for_cli", _spawn, raising=False)
    cli.agents_spawn("do a thing", permission_mode="plan")
    assert got.get("permission_mode") == "plan", (
        "the CLI did not receive the mode the panel sends — the flag spelling has drifted")


def test_an_unknown_mode_at_the_CLI_reads_like_every_other_error(monkeypatch, capsys):
    """`command()` validates, but it is an argv BUILDER four frames below the boundary — the MCP
    tool catches its ValueError and the CLI did not, so `--permission-mode typo` printed a
    traceback where every other galaius failure prints one actionable line.
    """
    import importlib

    cli = importlib.import_module("galaius.cli.app")
    monkeypatch.setattr(ClaudeCodeProvider, "available", lambda self: True)
    monkeypatch.setattr("galaius.agents.run.load_policy", lambda: pytest.fail("invalid mode must be rejected before policy loading"))

    with pytest.raises(SystemExit) as exit_info:
        cli.agents_spawn("t", agent="tester", permission_mode="definitely-not-a-mode")
    assert exit_info.value.code != 0
    said = capsys.readouterr()
    combined = said.out + said.err
    assert "ERROR" in combined, combined
    assert "definitely-not-a-mode" in combined
    # Names what IS accepted — an error that only says "no" costs another round trip.
    assert "plan" in combined


def test_agents_run_reports_a_bad_mode_too(monkeypatch, capsys):
    """`agents run` is the streaming sibling of `agents spawn` and validates the same value. Its
    handler referenced `sys` without importing it — so the one path meant to produce a clean line
    would have raised NameError instead. One report is one sample of a class."""
    import importlib

    cli = importlib.import_module("galaius.cli.app")
    monkeypatch.setattr(ClaudeCodeProvider, "available", lambda self: True)
    monkeypatch.setattr("galaius.agents.run.load_policy", lambda: pytest.fail("invalid mode must be rejected before policy loading"))

    with pytest.raises(SystemExit) as exit_info:
        cli.agents_run("t", agent="tester", permission_mode="definitely-not-a-mode")
    assert exit_info.value.code != 0
    combined = "".join(capsys.readouterr())
    assert "ERROR" in combined and "definitely-not-a-mode" in combined, combined


def test_the_doctor_reports_the_criterion_when_one_governs_the_role(monkeypatch, capsys):
    """A CRITERION set on the role's own field is what the real VLM call resolves with —
    `Config.resolve_model` parses it and chooses. The doctor must read the SAME field, or the
    two surfaces disagree: setting a criterion changes which model actually runs and shows
    nothing here.

    That is worse than a cosmetic gap. Watching this line is how you check your routing, so a
    criterion looked like it did nothing and the honest-seeming move was to pin a model id —
    which is exactly what a criterion exists to avoid.
    """
    monkeypatch.setattr("galaius.models.Model.is_available", lambda self: self.id == "zai/glm-4.5v")
    monkeypatch.setattr(runtime_config, "image_criteria", "cap.vlm and aa.intelligence >= 1")
    cli._print_resolved_models()
    out = capsys.readouterr().out
    assert "criterion" in out.lower(), "the line says a criterion is what decides"
    assert "cap.vlm and aa.intelligence >= 1" in out, "and names the rule, not just its answer"


def test_agent_provider_json_preserves_disabled_and_unavailable_choices(monkeypatch, capsys):
    from galaius.cli import app_commands
    from galaius.agents.policy import Policy

    policy = Policy(providers={"claude": False})
    monkeypatch.setattr(Policy, "load", classmethod(lambda cls: policy))
    monkeypatch.setattr(ClaudeCodeProvider, "available", lambda self: True)
    monkeypatch.setattr(CodexProvider, "available", lambda self: False)
    app_commands.agents_providers(json_out=True)
    rows = {row["id"]: row for row in json.loads(capsys.readouterr().out)["providers"]}
    assert rows["claude"] == {"id": "claude", "label": "claude", "active": False, "available": True}
    assert rows["codex"] == {"id": "codex", "label": "codex", "active": True, "available": False}


def test_agent_provider_json_listing_never_mutates_policy(monkeypatch, capsys):
    from galaius.cli import app_commands
    from galaius.agents.policy import Policy

    monkeypatch.setattr(Policy, "load", classmethod(lambda cls: Policy()))
    monkeypatch.setattr(Policy, "set_provider_active", lambda *args: pytest.fail("must not mutate"))
    with pytest.raises(SystemExit) as error:
        app_commands.agents_providers("codex", "off", json_out=True)
    assert error.value.code == 2
    assert "omit name and state" in capsys.readouterr().err
