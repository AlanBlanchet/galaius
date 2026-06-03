from pathlib import Path

import pytest

from interact.config import Config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Strip every key in either namespace so tests start blank.
    import os

    for var in list(os.environ):
        if var.startswith("INTERACT_"):
            monkeypatch.delenv(var, raising=False)


def test_from_env(monkeypatch):
    monkeypatch.setenv("INTERACT_IMAGE_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("INTERACT_VIDEO_MODEL", "gpt-4o")
    monkeypatch.setenv("INTERACT_HEADLESS", "false")
    monkeypatch.setenv("INTERACT_BROWSER_TYPE", "firefox")
    monkeypatch.setenv("INTERACT_VIEWPORT_WIDTH", "1920")
    cfg = Config()
    assert cfg.image_model == "claude-sonnet-4-5"
    assert cfg.video_model == "gpt-4o"
    assert cfg.headless is False
    assert cfg.browser_type == "firefox"
    assert cfg.viewport_width == 1920


def test_screenshot_dump_dir_from_env(monkeypatch):
    monkeypatch.setenv("INTERACT_SCREENSHOT_DUMP_DIR", "/tmp/shots")
    cfg = Config()
    assert cfg.screenshot_dump_dir == Path("/tmp/shots")


def test_video_settings_from_env(monkeypatch):
    monkeypatch.setenv("INTERACT_VIDEO_FPS", "10")
    monkeypatch.setenv("INTERACT_VIDEO_DURATION", "5.0")
    cfg = Config()
    assert cfg.video_fps == 10
    assert cfg.video_duration == 5.0


@pytest.mark.parametrize(
    "component_model,image_model,expected",
    [
        ("gemini/gemini-2.0-flash", "gpt-4.1", "gemini/gemini-2.0-flash"),
        ("", "gpt-4.1", ""),
    ],
)
def test_model_for_component(monkeypatch, component_model, image_model, expected):
    monkeypatch.setenv("INTERACT_COMPONENT_MODEL", component_model)
    monkeypatch.setenv("INTERACT_IMAGE_MODEL", image_model)
    cfg = Config()
    assert cfg.model_for("component") == expected


def test_vlm_min_dim_exceeds_max_dim_raises(monkeypatch):
    monkeypatch.setenv("INTERACT_VLM_MIN_DIM", "2000")
    monkeypatch.setenv("INTERACT_VLM_MAX_DIM", "1280")
    with pytest.raises(ValueError, match="vlm_min_dim.*must be <= vlm_max_dim"):
        Config()
