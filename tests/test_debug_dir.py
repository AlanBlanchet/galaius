"""debug_dir: the single, overridable base for galaius's logs + debug artifacts, and what
`Debug` writes into it (tool-input dumps, output dumps, per-invocation/per-step paths)."""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from galaius import debug_utils
from galaius.cli import usage
from galaius.config import Config


@pytest.fixture
def srv():
    import galaius.server as _srv
    from galaius.server import breaker

    breaker.clear()
    _srv.config.component_criteria = "cap.gui_grounding"
    with patch.object(_srv.Debug, "save"):
        yield _srv
    _srv.config.clear_overrides()  # drop the transient override so it can't leak into later tests
    breaker.clear()


def test_debug_dir_default_is_home_galaius_out(monkeypatch):
    # Output lives under ~/.galaius/out so the ~/.galaius root stays clean (config.env + out/).
    # delenv first: importing galaius.runtime loads the developer's OWN ~/.galaius/config.env
    # into os.environ, so on a machine that sets GALAIUS_DEBUG_DIR this asserted the developer's
    # override instead of the default — green in CI, permanently red locally.
    monkeypatch.delenv("GALAIUS_DEBUG_DIR", raising=False)
    assert Config().debug_dir == Path.home() / ".galaius" / "out"
    assert Config().usage_log == Path.home() / ".galaius" / "out" / "usage.jsonl"


def test_debug_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("GALAIUS_DEBUG_DIR", str(tmp_path))
    config = Config()
    assert config.debug_dir == tmp_path
    assert config.usage_log == tmp_path / "usage.jsonl"


@pytest.mark.parametrize(
    "field, attr",
    [
        ("GALAIUS_DEBUG_DIR", "debug_dir"),
        ("GALAIUS_SCREENSHOT_DUMP_DIR", "screenshot_dump_dir"),
        ("GALAIUS_BROWSER_PROFILE_DIR", "browser_profile_dir"),
    ],
)
def test_path_settings_expand_tilde(monkeypatch, field, attr):
    """`~` is expanded once, where the value enters. The TUI and the VS Code settings UI both
    hand these fields free text and advertise `~/...` defaults, so a literal `PosixPath('~/x')`
    silently creates a `./~/x` dir next to wherever the server happened to start."""
    monkeypatch.setenv(field, "~/proj/out")
    assert getattr(Config(), attr) == Path.home() / "proj" / "out"


def test_usage_log_follows_expanded_tilde(monkeypatch):
    monkeypatch.setenv("GALAIUS_DEBUG_DIR", "~/.galaius/out")
    assert Config().usage_log == Path.home() / ".galaius" / "out" / "usage.jsonl"


def test_bare_tilde_is_home(monkeypatch):
    monkeypatch.setenv("GALAIUS_DEBUG_DIR", "~")
    assert Config().debug_dir == Path.home()


def test_unexpandable_tilde_gives_a_readable_error(monkeypatch):
    """`~nosuchuser` makes expanduser raise RuntimeError, which pydantic does NOT wrap — without
    the ValueError conversion the server dies with a bare traceback instead of a field error."""
    monkeypatch.setenv("GALAIUS_DEBUG_DIR", "~nosuchuser12345/out")
    with pytest.raises(ValidationError, match="cannot expand"):
        Config()


def test_dump_dir_precedence(monkeypatch, tmp_path):
    # per-call arg > explicit screenshot_dump_dir > debug_dir base
    monkeypatch.setattr(debug_utils.config, "screenshot_dump_dir", None)
    monkeypatch.setattr(debug_utils.config, "debug_dir", tmp_path)
    assert debug_utils.Debug.dump_dir(None) == tmp_path
    assert debug_utils.Debug.dump_dir("out/claude") == tmp_path / "out" / "claude"

    monkeypatch.setattr(debug_utils.config, "screenshot_dump_dir", tmp_path / "shots")
    assert debug_utils.Debug.dump_dir(None) == tmp_path / "shots"


def test_dump_dir_expands_tilde(monkeypatch, tmp_path):
    """The per-call `debug_dir` tool arg is the same boundary as the env var — an agent passing
    `~/shots` must not get a literal `./~/shots` next to the server's cwd."""
    monkeypatch.setattr(debug_utils.config, "debug_dir", tmp_path / "artifacts")
    assert debug_utils.Debug.dump_dir("~/shots") == Path.home() / "shots"
    assert debug_utils.Debug.dump_dir("out/claude") == tmp_path / "artifacts/out/claude"


def test_usage_default_log_follows_debug_dir(monkeypatch, tmp_path):
    from galaius.runtime import config as runtime_config

    monkeypatch.setattr(runtime_config, "debug_dir", tmp_path)
    assert usage.default_log_path() == tmp_path / "usage.jsonl"


def test_dump_input_writes_both_files(tmp_path):
    from galaius.debug_utils import Debug

    inv = str(tmp_path / "inv")
    Debug.dump_input(inv, {"tool": "screenshot", "query": "q"}, {"image_model": "m", "headless": True})
    written = json.loads((tmp_path / "inv" / "tool_input.json").read_text())
    resolved = json.loads((tmp_path / "inv" / "tool_input_resolved.json").read_text())
    assert written["query"] == "q" and written["tool"] == "screenshot"
    assert resolved["image_model"] == "m"


def test_dump_output_records_exact_return_including_errors(tmp_path):
    from galaius.debug_utils import Debug

    inv = str(tmp_path / "ok")
    Debug.dump_output(inv, "clicked [3] button: 'Play'")
    assert (tmp_path / "ok" / "output.txt").read_text() == "clicked [3] button: 'Play'"

    inv_err = str(tmp_path / "err")
    Debug.dump_output(inv_err, "No window matching 'Foo'. Available:\n  Bar")
    assert "No window matching" in (tmp_path / "err" / "output.txt").read_text()

    inv_img = str(tmp_path / "img")
    Debug.dump_output(inv_img, ["window summary text", object()])
    assert (tmp_path / "img" / "output.txt").read_text() == "window summary text"

    Debug.dump_output(None, "ignored")  # no invocation dir → no-op


# =============================================================================================
# Session-timestamped debug folder structure + Debug.step_save
# =============================================================================================


def test_debug_path_returns_none_without_invocation_id(srv):
    """Debug.path returns None when invocation_id is not set."""
    result = srv.Debug.path("my_label", "png")
    assert result is None


def test_debug_path_with_invocation_id(srv, tmp_path):
    """Debug.path with invocation_id uses flat filename inside invocation dir."""
    inv_dir = tmp_path / "20260423_140000" / "143045_tool"
    inv_dir.mkdir(parents=True)
    result = srv.Debug.path("vlm_raw", "txt", invocation_id=str(inv_dir))

    assert result is not None
    assert result.parent == inv_dir
    assert result.name == "vlm_raw.txt"


def test_new_invocation_dir(srv, tmp_path):
    """Debug.new_invocation_dir creates HHMMSS_tool subfolder under session timestamp, deduplicates on collision."""
    now = datetime(2026, 4, 23, 14, 52, 14)
    with (
        patch("galaius.debug_utils._dt", wraps=datetime) as mock_dt,
        patch.object(srv.Debug, "SESSION_TS", "20260423_140000"),
    ):
        mock_dt.now.return_value = now
        inv = srv.Debug.new_invocation_dir(str(tmp_path), "get_interactive_elements")

    assert inv is not None

    p = Path(inv)
    assert p.name == "145214_get_interactive_elements"
    assert p.parent.name == "20260423_140000"
    assert p.exists()

    # Second call in same second creates _2 suffix
    with (
        patch("galaius.debug_utils._dt", wraps=datetime) as mock_dt,
        patch.object(srv.Debug, "SESSION_TS", "20260423_140000"),
    ):
        mock_dt.now.return_value = now
        inv2 = srv.Debug.new_invocation_dir(str(tmp_path), "get_interactive_elements")
    assert Path(inv2).name == "145214_get_interactive_elements_2"
    assert Path(inv2).exists()


def test_step_debug_save_computes_step_dir(srv):
    with patch.object(srv.Debug, "save") as mock_save:
        srv.Debug.step_save(
            "/dbg/20260424/123000_run_actions",
            2,
            "click",
            "screenshot",
            b"png",
            ext="png",
        )

    mock_save.assert_called_once_with(
        "screenshot",
        b"png",
        ext="png",
        # os-native separator (Debug.step_save joins via pathlib → backslashes on Windows)
        invocation_id=str(Path("/dbg/20260424/123000_run_actions") / "002_click"),
    )


def test_step_debug_save_noop_without_invocation_id(srv):
    with patch.object(srv.Debug, "save") as mock_save:
        srv.Debug.step_save(None, 0, "click", "screenshot", b"data")

    mock_save.assert_not_called()
