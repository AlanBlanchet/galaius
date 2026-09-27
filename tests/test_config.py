"""Everything the `Config` surface reads and writes.

Formerly split across `test_config`, `test_config_check`, `test_config_logging` and
`test_dotenv`. One subject (how a user's configuration reaches the running product), three
facets:

* environment → `Config` fields (this file's first block);
* `interact config set` running a real vision call to say whether the key works;
* the `.env` autouse fixture in `conftest.py` really loading keys.

A wrong key saved silently sits in `config.env` until the first real screenshot fails deep
inside a vendor error; the `set` site is the one place the user is watching the answer, so a
"works / does not work" verdict happens there.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
from dotenv import load_dotenv

from interact.cli import config_check
from interact.config import Config
from interact.config.user import UserConfig


# ── Environment → `Config` fields ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip every key in the INTERACT_ namespace so tests start blank."""
    for var in list(os.environ):
        if var.startswith("INTERACT_"):
            monkeypatch.delenv(var, raising=False)


def test_from_env(monkeypatch):
    monkeypatch.setenv("INTERACT_IMAGE_CRITERIA", "cap.vlm and aa.intelligence > 80%")
    monkeypatch.setenv("INTERACT_VIDEO_CRITERIA", "cap.video")
    monkeypatch.setenv("INTERACT_HEADLESS", "false")
    monkeypatch.setenv("INTERACT_BROWSER_TYPE", "firefox")
    monkeypatch.setenv("INTERACT_VIEWPORT_WIDTH", "1920")
    cfg = Config()
    assert cfg.image_criteria == "cap.vlm and aa.intelligence > 80%"
    assert cfg.video_criteria == "cap.video"
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
    "component_criteria,expected",
    [
        ("cap.gui_grounding and price.in < 5", "cap.gui_grounding and price.in < 5"),
        ("", "cap.gui_grounding"),  # unset → the role's own bare-capability default, never blank
    ],
)
def test_criteria_for_component(monkeypatch, component_criteria, expected):
    monkeypatch.setenv("INTERACT_COMPONENT_CRITERIA", component_criteria)
    cfg = Config()
    assert cfg.criteria_for("component") == expected


def test_vlm_min_dim_exceeds_max_dim_raises(monkeypatch):
    monkeypatch.setenv("INTERACT_VLM_MIN_DIM", "2000")
    monkeypatch.setenv("INTERACT_VLM_MAX_DIM", "1280")
    with pytest.raises(ValueError, match="vlm_min_dim.*must be <= vlm_max_dim"):
        Config()


def test_require_media_session_confirmation_names_the_in_product_path_forward():
    cfg = Config(media_session_no_extra_usage_confirmed_for=())
    with pytest.raises(RuntimeError, match="confirm_media_session_for"):
        cfg.require_media_session_confirmation(("claude",))


def test_confirm_media_session_for_unblocks_the_current_session_only():
    cfg = Config(media_session_no_extra_usage_confirmed_for=())
    with pytest.raises(RuntimeError):
        cfg.require_media_session_confirmation(("claude",))

    cfg.confirm_media_session_for("claude")
    cfg.require_media_session_confirmation(("claude",))  # no longer raises

    # session-only: a fresh Config (the on-disk default) is unaffected.
    assert Config(media_session_no_extra_usage_confirmed_for=()).media_session_no_extra_usage_confirmed_for == ()


def test_confirm_media_session_for_rejects_unknown_provider():
    cfg = Config()
    with pytest.raises(ValueError, match="unknown media provider: bogus"):
        cfg.confirm_media_session_for("bogus")


# ── `interact config set` → "it works" verdict ─────────────────────────────────────────────
# When a user persists a model key or a model pin, the CLI must not stop at "saved" — it must
# run that exact credential / model against a real vision call and print whether it works.


@pytest.fixture
def _check_env(tmp_path, monkeypatch):
    monkeypatch.setattr(UserConfig, "PATH", tmp_path / "config.env")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config_check.ConfigCheck.reset()
    yield
    config_check.ConfigCheck.reset()


class _Result:
    def __init__(self, text="a tiny image", model="openai/gpt-4o"):
        self.text = text
        self.model = model
        self.elapsed = 0.1


async def _ok(*args, **kwargs):
    return _Result()


async def _bad(*args, **kwargs):
    raise RuntimeError("invalid api key")


@pytest.fixture
def _probe_openai(monkeypatch):
    """Route the probe resolver at an openai model whatever the criterion says."""
    monkeypatch.setattr(config_check.ConfigCheck, "_criterion_for", classmethod(
        lambda cls, env: "openai/gpt-4o" if env == "OPENAI_API_KEY" else None))


@pytest.mark.anyio
async def test_set_key_reports_works(monkeypatch, _check_env, _probe_openai):
    monkeypatch.setattr(config_check.ConfigCheck, "_analyze", classmethod(
        lambda cls, model, what: _ok()))
    verdict = await config_check.ConfigCheck.key("OPENAI_API_KEY")
    assert verdict.ok
    assert "gpt-4o" in verdict.model


@pytest.mark.anyio
async def test_set_key_reports_failure_with_reason(monkeypatch, _check_env, _probe_openai):
    monkeypatch.setattr(config_check.ConfigCheck, "_analyze", classmethod(
        lambda cls, model, what: _bad()))
    verdict = await config_check.ConfigCheck.key("OPENAI_API_KEY")
    assert not verdict.ok
    assert "invalid api key" in verdict.detail


@pytest.mark.anyio
async def test_unknown_provider_key_no_probe(_check_env):
    verdict = await config_check.ConfigCheck.key("COHERE_API_KEY")
    assert not verdict.ok
    assert "no probe model" in verdict.detail


def test_non_media_setting_skips_probe(_check_env, capsys):
    """`config set desktop.target nested` must not fire a model call."""
    from interact.cli.app_commands import config_set

    config_set("desktop.target", "nested")
    out = capsys.readouterr().out
    assert "works" not in out.lower()


def test_set_value_breaking_shell_sourcing_warns(_check_env, capsys):
    """A value that breaks when `config.env` is SOURCED must be caught at set time.

    Real failure: `INTERACT_MEDIA_CRITERIA=cap.vlm and aa.intelligence >= 40` was written
    unquoted; every `source ~/.interact/config.env` then ran `and` as a command, and nothing
    ever said so. The set site is where the user is watching — the warning belongs there.
    """
    from interact.cli.app_commands import config_set

    config_set("media.criteria", "cap.vlm and aa.intelligence >= 40")
    out = capsys.readouterr().out
    assert "sources" in out.lower() or "shell" in out.lower() or "quote" in out.lower()


def test_written_file_sources_cleanly(_check_env, capsys):
    """Whatever `config set` writes, `bash -c 'source file'` must exit 0."""
    import subprocess
    from interact.cli.app_commands import config_set

    config_set("media.criteria", "cap.vlm and aa.intelligence >= 40")
    proc = subprocess.run(
        ["bash", "-c", f"source {UserConfig.PATH} && echo ok"],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_quoted_value_reads_back_unquoted(_check_env, tmp_path, monkeypatch):
    """Write side quotes shell-hostile values; read side must strip them again.

    Real failure: after the quoting fix, the criteria value round-tripped as
    `'cap.vlm and …'` WITH quotes, and the next criterion parse failed on the leading quote.
    The file is one store with two readers (python, bash) — the pair must compose.
    """
    from interact.cli.app_commands import config_set

    config_set("media.criteria", "cap.vlm and aa.intelligence >= 40")
    assert UserConfig.get("media.criteria") == "cap.vlm and aa.intelligence >= 40"
    raw = UserConfig.PATH.read_text()
    assert "INTERACT_MEDIA_CRITERIA='cap.vlm and aa.intelligence >= 40'" in raw


def test_probe_resolves_through_criterion(_check_env, monkeypatch):
    """The probe target comes from the user's own `media.criteria` resolver, restricted to the
    provider owning the key — never a literal table."""
    from interact.models import Model

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    Model.load_registry()
    resolved = config_check.ConfigCheck._criterion_for("OPENAI_API_KEY")
    assert resolved is not None
    bare = resolved.split("/", 1)[-1]
    assert Model.from_litellm_id(resolved).provider == "openai" or bare.startswith("gpt")


# ── Debug dumps + no coord leak ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_dumps_early_error_returns(tmp_path, monkeypatch):
    """A tool's malformed-input early return must still be logged. `measure_ui` used to dump
    only its happy-path result, so an error return (bad region/point) was silently dropped from
    the audit trail; the `@instrumented` decorator now dumps EVERY return path once. Dumps
    route to `tmp_path` via the autouse log-isolation fixture; `refresh` is stubbed so the
    decorator never reads (or leaks into `os.environ`) the developer's real
    `~/.interact/config.env`."""
    from interact.runtime import _LiveConfig
    from interact.server.tools_vision import measure_ui

    monkeypatch.setattr(_LiveConfig, "refresh", lambda self: self)
    out = await measure_ui(region="not,valid,ints")
    assert out.startswith("ERROR")
    dumped = list(tmp_path.glob("**/output.txt"))
    assert dumped, "the tool's early error return was not written to output.txt"
    assert "ERROR" in dumped[0].read_text()


# ── The session `.env` fixture in `conftest.py` really loads keys ──────────────────────────


def test_dotenv_fixture_loads_keys(tmp_path: Path, monkeypatch) -> None:
    """The session fixture walks up from cwd and loads `.env` if present."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        textwrap.dedent(
            """\
            INTERACT_TEST_DOTENV_KEY=from-dotenv
            """
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("INTERACT_TEST_DOTENV_KEY", raising=False)

    # Replicate the fixture body — we can't trigger the session fixture mid-run.
    cwd = Path.cwd().resolve()
    for parent in [cwd, *list(cwd.parents)[:3]]:
        candidate = parent / ".env"
        if candidate.exists():
            load_dotenv(candidate, override=False)
            break

    assert os.environ.get("INTERACT_TEST_DOTENV_KEY") == "from-dotenv"


def test_dotenv_override_false_preserves_existing(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("INTERACT_TEST_DOTENV_KEY2=from-dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INTERACT_TEST_DOTENV_KEY2", "preset")

    load_dotenv(env_file, override=False)
    assert os.environ.get("INTERACT_TEST_DOTENV_KEY2") == "preset"


# ── Config.resolve_model: the single resolution site, never empty downstream ────────────────


def test_resolve_model_override_wins_over_everything():
    cfg = Config(image_criteria="aa.intelligence > 999")  # would otherwise clear nothing
    assert cfg.resolve_model("image", override="explicit/override") == "explicit/override"


@pytest.mark.parametrize("role", ["image", "component", "video"])
def test_resolve_model_auto_is_never_empty(role):
    """No configured criterion, no override → resolution still falls through to a concrete id
    via the role's bare-capability default. This is the exact scenario that returned '[Vision
    not configured]': the old model_for() was '' and that empty string flowed all the way to
    analyze_media."""
    cfg = Config()
    resolved = cfg.resolve_model(role)
    assert resolved  # non-empty
    assert resolved in {m.id for m in cfg.ranked_models(role)}
