"""`galaius --version` / `-v` report the installed build."""

from datetime import UTC, datetime

import pytest

from galaius import cli
from galaius.upgrade.release import BuildIdentity


@pytest.mark.parametrize("build", [None, BuildIdentity(version="0.43.0", released_at=datetime(2026, 9, 29, 1, 2, tzinfo=UTC), commit="40d6bc4090826a61")])
def test_version_command_prints_the_installed_version_and_the_build_it_is(capsys, monkeypatch, build):
    from galaius import installed_version

    monkeypatch.setattr(BuildIdentity, "installed", classmethod(lambda cls: build))
    cli.version()
    assert capsys.readouterr().out.strip() == installed_version() + ("" if build is None else " 40d6bc4 (2026-09-29 01:02 UTC)")


def test_dash_v_alias_is_registered_alongside_double_dash_version():
    # users reach for `-v`; cyclopts wires only `--version` by default, so we add the alias
    assert "-v" in cli.app.version_flags and "--version" in cli.app.version_flags
