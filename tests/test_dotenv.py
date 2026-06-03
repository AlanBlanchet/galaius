"""Tests for the dotenv autouse fixture in conftest.py."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path


def test_dotenv_fixture_loads_keys(tmp_path: Path, monkeypatch) -> None:
    """The session fixture walks up from cwd and loads `.env` if present."""
    from dotenv import load_dotenv

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
    from dotenv import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("INTERACT_TEST_DOTENV_KEY2=from-dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INTERACT_TEST_DOTENV_KEY2", "preset")

    load_dotenv(env_file, override=False)
    assert os.environ.get("INTERACT_TEST_DOTENV_KEY2") == "preset"
