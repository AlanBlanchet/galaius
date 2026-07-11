"""~/.interact/config.env is the live source of truth: a long-running server reflects file
edits on the next tool call (config.refresh()), and clearing a setting in the file reverts it —
no stale environment snapshot. This is the bug where a TUI/file model change didn't reach the
already-running MCP server."""

import pytest

from interact.config import UserConfig


@pytest.fixture
def live_config(tmp_path, monkeypatch):
    monkeypatch.setattr(UserConfig, "PATH", tmp_path / "config.env")
    # Start from a clean environment for the settings under test.
    for name in ("INTERACT_COMPONENT_MODEL", "INTERACT_IMAGE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    from interact.runtime import config

    config.clear_overrides()  # isolate from any override leaked by an earlier test
    return config


def test_file_edit_is_picked_up_and_clearing_reverts(live_config):
    # No pin → the configured model is empty (resolves to the auto default downstream).
    assert live_config.refresh().component_model == ""

    # A file edit (what the TUI / hand-edit does) reaches a running server on the next refresh.
    UserConfig.set("component.model", "zai/glm-4.5v")
    assert live_config.refresh().component_model == "zai/glm-4.5v"

    # Clearing it in the file actually takes effect — the stale env var is dropped, not kept.
    UserConfig.unset("component.model")
    assert live_config.refresh().component_model == ""


def test_in_process_override_survives_refresh(live_config):
    # An explicit in-process set (tests, screenshot_dump_dir) wins over the file and persists
    # across refreshes — and is visible to methods that read self.field (model_for).
    live_config.component_model = "test/override"
    assert live_config.model_for("component") == "test/override"
    UserConfig.set("component.model", "zai/glm-4.5v")
    live_config.refresh()
    assert live_config.model_for("component") == "test/override"  # override still wins
