"""`interact --version` / `-v` report the installed build."""

from interact import cli


def test_version_command_prints_the_installed_version(capsys):
    from interact import installed_version

    cli.version()
    assert capsys.readouterr().out.strip() == installed_version()


def test_dash_v_alias_is_registered_alongside_double_dash_version():
    # users reach for `-v`; cyclopts wires only `--version` by default, so we add the alias
    assert "-v" in cli.app.version_flags and "--version" in cli.app.version_flags
