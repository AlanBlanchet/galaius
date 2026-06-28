"""`interact providers` surfaces the RESOLVED selection — what each tool actually uses given the
current keys, plus the sovereign quality tier — so a user never has to ask "why is my default X?".
A grep of availability isn't enough: resolution (frontier-first, first-available) is the real answer."""

import pytest


@pytest.fixture
def _only_zai(monkeypatch):
    # Make ONLY the z.ai GLM reachable, so resolution is deterministic without real keys.
    monkeypatch.setattr("interact.models.Model.is_available", lambda self: self.id == "zai/glm-4.5v")


def test_providers_surfaces_the_resolved_selection_and_sovereign_tier(_only_zai, capsys):
    from interact import cli

    cli.providers()
    out = capsys.readouterr().out
    assert "Resolved selection" in out
    # the low/medium sovereign tier names the GLM lit up by the z.ai key — the meaningful interpretation
    assert "zai/glm-4.5v" in out and "low/medium use this GLM" in out


def test_providers_flags_a_role_whose_key_is_missing(monkeypatch, capsys):
    from interact import cli

    monkeypatch.setattr("interact.models.Model.is_available", lambda self: False)  # nothing reachable
    cli.providers()
    out = capsys.readouterr().out
    assert "⚠ key missing" in out  # a pick that will error at call time is called out, not silently shown
    assert "no sovereign key" in out  # and low/medium honestly report the fall-back to frontier
