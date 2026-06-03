"""debug_dir: the single, overridable base for interact's logs + debug artifacts."""

from pathlib import Path

from interact import debug_utils, usage
from interact.config import Config


def test_debug_dir_default_is_home_interact():
    assert Config().debug_dir == Path.home() / ".interact"
    assert Config().usage_log == Path.home() / ".interact" / "logs" / "usage.jsonl"


def test_debug_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERACT_DEBUG_DIR", str(tmp_path))
    config = Config()
    assert config.debug_dir == tmp_path
    assert config.usage_log == tmp_path / "logs" / "usage.jsonl"


def test_dump_dir_precedence(monkeypatch, tmp_path):
    # per-call arg > explicit screenshot_dump_dir > debug_dir base
    monkeypatch.setattr(debug_utils.config, "screenshot_dump_dir", None)
    monkeypatch.setattr(debug_utils.config, "debug_dir", tmp_path)
    assert debug_utils.Debug.dump_dir(None) == tmp_path
    assert debug_utils.Debug.dump_dir("out/claude") == Path("out/claude")

    monkeypatch.setattr(debug_utils.config, "screenshot_dump_dir", tmp_path / "shots")
    assert debug_utils.Debug.dump_dir(None) == tmp_path / "shots"


def test_usage_default_log_follows_debug_dir(monkeypatch, tmp_path):
    from interact.runtime import config as runtime_config

    monkeypatch.setattr(runtime_config, "debug_dir", tmp_path)
    assert usage.default_log_path() == tmp_path / "logs" / "usage.jsonl"
