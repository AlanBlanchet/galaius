"""CLI surfaces tell ONE story about model selection: `status`/`providers`/`doctor` all print the
RESOLVED model per role (not an opaque 'auto'), plus the sovereign quality tier — and `version`/`-v`
report the build. A grep of availability isn't the answer; resolution (frontier-first, first-available)
is. The shared helper is exercised here once; the three commands just call it."""

import pytest

from interact import cli


@pytest.fixture
def _only_zai(monkeypatch):
    # ONLY the z.ai GLM reachable → resolution is deterministic without real keys.
    monkeypatch.setattr("interact.models.Model.is_available", lambda self: self.id == "zai/glm-4.5v")


def test_resolved_models_helper_names_every_role_and_the_sovereign_tier(_only_zai, capsys):
    cli._print_resolved_models()
    out = capsys.readouterr().out
    for label in ("image", "component", "video", "audio", "quality"):
        assert label in out
    # the low/medium sovereign tier names the GLM the z.ai key lights up (independent of any role pin)
    assert "zai/glm-4.5v" in out and "low/medium tier" in out


def test_resolved_models_helper_flags_a_missing_key(monkeypatch, capsys):
    monkeypatch.setattr("interact.models.Model.is_available", lambda self: False)  # nothing reachable
    cli._print_resolved_models()
    out = capsys.readouterr().out
    assert "⚠ key missing" in out          # a pick that will error is called out, not shown silently
    assert "fall back to frontier" in out   # low/medium honestly report the absence of a sovereign key


def test_providers_includes_the_resolved_selection(_only_zai, capsys):
    cli.providers()
    out = capsys.readouterr().out
    assert "Resolved selection" in out and "zai/glm-4.5v" in out


def test_version_command_prints_the_installed_version(capsys):
    from interact import installed_version

    cli.version()
    assert capsys.readouterr().out.strip() == installed_version()


def test_dash_v_alias_is_registered_alongside_double_dash_version():
    # users reach for `-v`; cyclopts wires only `--version` by default, so we add the alias
    assert "-v" in cli.app.version_flags and "--version" in cli.app.version_flags
