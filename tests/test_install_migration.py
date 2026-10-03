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


def test_both_folders_present_moves_nothing(home):
    (home / ".galaius").mkdir()
    steps = {step.name: step for step in InstallMigration(home=home).move_folders()}
    assert steps[str(home / ".galaius")].outcome == "conflict"
    assert (home / ".interact" / "config.env").exists()
