"""A computer that ran interact keeps its state, settings and MCP registration once moved to galaius."""

import json
import shutil

import pytest

from galaius.install_migration import InstallMigration


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    monkeypatch.setattr(shutil, "which", lambda name: None)  # never touches this PC's uv, systemctl, claude, code
    (tmp_path / ".interact").mkdir()
    (tmp_path / ".interact" / "config.env").write_text("INTERACT_IMAGE_MODEL=x\nexport INTERACT_HEADLESS=1\nOTHER=interact\n")
    (tmp_path / ".config" / "interact").mkdir(parents=True)
    (tmp_path / ".config" / "interact" / "machine.json").write_text("{}")
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"interact": {"command": "interact", "args": ["mcp"]}, "other": {}}}))
    (tmp_path / ".bashrc").write_text("export PATH=x\nexport INTERACT_DEBUG_DIR=/x\n")
    return tmp_path


def test_state_settings_and_registration_move_once(home):
    steps = {step.name: step for step in InstallMigration(home=home).run()}
    assert (home / ".galaius" / "config.env").read_text() == "GALAIUS_IMAGE_MODEL=x\nexport GALAIUS_HEADLESS=1\nOTHER=interact\n"
    assert (home / ".config" / "galaius" / "machine.json").exists() and not (home / ".config" / "interact").exists()
    assert set(json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]) == {"galaius", "other"}
    assert steps[str(home / ".bashrc")].outcome == "manual" and "lines 2" in steps[str(home / ".bashrc")].detail
    again = InstallMigration(home=home).run()
    assert {step.outcome for step in again} <= {"already", "absent", "manual"}


def test_a_folder_the_installer_started_takes_the_former_entries(home):
    (home / ".config" / "galaius").mkdir(parents=True)
    (home / ".config" / "galaius" / "login-server").write_text("https://galaius.ai\n")
    (home / ".galaius").mkdir()
    (home / ".galaius" / "config.env").write_text("GALAIUS_IMAGE_MODEL=y\n")
    steps = {step.name: step for step in InstallMigration(home=home).move_folders()}
    assert steps[str(home / ".config" / "galaius")].outcome == "done" and not (home / ".config" / "interact").exists()
    assert sorted(path.name for path in (home / ".config" / "galaius").iterdir()) == ["login-server", "machine.json"]
    assert steps[str(home / ".galaius")].outcome == "conflict" and "config.env" in steps[str(home / ".galaius")].detail
    assert (home / ".interact" / "config.env").exists() and (home / ".galaius" / "config.env").read_text() == "GALAIUS_IMAGE_MODEL=y\n"


def test_the_installers_own_uv_and_login_server_win_over_the_former_copies(home):
    data = home / ".local" / "share"
    for product in ("interact", "galaius"):
        (data / product / "uv" / "bin").mkdir(parents=True)
        (data / product / "uv" / "bin" / "uv").write_text(product)
        (home / ".config" / product).mkdir(exist_ok=True)
        (home / ".config" / product / "login-server").write_text(f"https://{product}.ai\n")
    (data / "interact" / "workspaces").mkdir()
    steps = {step.name: step for step in InstallMigration(home=home).move_folders()}
    assert steps[str(data / "galaius")].outcome == "done" and steps[str(home / ".config" / "galaius")].outcome == "done"
    assert (data / "galaius" / "uv" / "bin" / "uv").read_text() == "galaius" and (data / "galaius" / "workspaces").is_dir()
    assert (home / ".config" / "galaius" / "login-server").read_text() == "https://galaius.ai\n"
    assert sorted(path.name for path in (home / ".config" / "galaius").iterdir()) == ["login-server", "machine.json"]
    assert not (data / "interact").exists() and not (home / ".config" / "interact").exists()
