"""Logs consolidate under ~/.interact (no /tmp scatter) and separate by the CALLING Claude session:
a session's user-set custom-title (read from ~/.claude/projects — the store scan_client_errors uses)
names the folder, with a dated dir inside; it falls back to the project/cwd basename, then 'default'.
The dir basename and the session title can differ (e.g. dir 'aino' vs title 'Aino') — the title wins."""

import json
import re
from pathlib import Path

import pytest

from interact import config as cfgmod
from interact.config import Config, _safe_dir_name, caller_session_name


@pytest.fixture(autouse=True)
def _clear_session_cache():
    cfgmod._resolve_session_name.cache_clear()
    yield
    cfgmod._resolve_session_name.cache_clear()


def _fake_session(home: Path, slug: str, sid: str, custom_title: str | None):
    """Write a minimal Claude session transcript with (optionally) a custom-title entry."""
    d = home / ".claude" / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "user", "message": "hi"}, {"type": "ai-title", "aiTitle": "a long generated summary"}]
    if custom_title is not None:
        lines.append({"type": "custom-title", "customTitle": custom_title, "sessionId": sid})
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))


def test_session_name_uses_the_custom_title_over_the_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-1")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/work/aino")  # dir basename "aino"
    _fake_session(tmp_path, "proj-aino", "sid-1", "Aino")  # title "Aino"
    assert caller_session_name() == "Aino"  # the session NAME, not the dir — they differ by case


def test_session_name_falls_back_to_project_basename_when_unnamed(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-2")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/work/aino")
    _fake_session(tmp_path, "proj-aino", "sid-2", None)  # no custom-title → dir, NOT ai-title
    assert caller_session_name() == "aino"


def test_session_name_defaults_to_cwd_without_claude_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert caller_session_name() == _safe_dir_name(tmp_path.name)  # a clean name from cwd


def test_session_name_is_sanitised(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-3")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/x/y")
    _fake_session(tmp_path, "-x-y", "sid-3", "My Session / v2!")
    name = caller_session_name()
    assert "/" not in name and " " not in name and name  # safe for a directory


def test_session_log_dir_is_sessions_name_date(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-4")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/work/interact")
    _fake_session(tmp_path, "proj-interact", "sid-4", "Interact")
    d = Config(debug_dir=tmp_path / ".interact").session_log_dir()
    rel = d.relative_to(tmp_path / ".interact")
    assert rel.parts[0] == "sessions" and rel.parts[1] == "Interact"  # organised BY SESSION, not a flat 'logs'
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", rel.parts[2])  # dated dir inside


def test_new_invocation_dir_default_nests_under_sessions_name_date(monkeypatch, tmp_path):
    from interact.debug_utils import Debug
    from interact.runtime import config as rc

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/work/aino")
    monkeypatch.setattr(rc, "debug_dir", tmp_path / ".interact")
    monkeypatch.setattr(rc, "screenshot_dump_dir", None)
    out = Debug.new_invocation_dir(None, "review_ui")
    rel = Path(out).relative_to(tmp_path / ".interact")
    assert rel.parts[:2] == ("sessions", "aino")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", rel.parts[2]) and rel.parts[3].endswith("_review_ui")


def test_open_log_writes_under_interact_not_tmp(monkeypatch, tmp_path):
    from interact.desktop.backend import NestedBackend
    from interact.runtime import config as rc

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/work/interact")
    monkeypatch.setattr(rc, "debug_dir", tmp_path / ".interact")
    p = NestedBackend._open_log("xephyr:99")
    try:
        # Under <debug_dir>/sessions/<session>/<date>/ — not a bare system-temp mkstemp (the old /tmp path).
        rel = Path(p).relative_to(tmp_path / ".interact" / "sessions" / "interact")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", rel.parts[0])  # dated dir
        assert rel.parts[1].startswith("xephyr:99-")  # the log file inside it
    finally:
        Path(p).unlink(missing_ok=True)
