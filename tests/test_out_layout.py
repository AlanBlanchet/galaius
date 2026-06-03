"""Regression test: every `out/` writer must nest under a typed subdir.

Bare `out/` is rejected by `_new_invocation_dir`; well-known writers must
resolve under `out/vscode` (MCP server debug) or `out/tests` (test artefacts).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from interact.debug_utils import Debug


_INVOCATION_RE = re.compile(r"out/vscode/(\d{8}_\d{6})/(\d{6}_screenshot)$")


class TestOutLayout:
    def test_rejects_bare_out(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match="must be a subdirectory of out/"):
            Debug.new_invocation_dir("out", "screenshot")

    def test_new_invocation_dir_under_vscode(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = Debug.new_invocation_dir("out/vscode", "screenshot")
        assert result is not None
        rel = Path(result).as_posix()
        assert _INVOCATION_RE.search(rel), rel
        assert (tmp_path / result).is_dir()

    @pytest.mark.parametrize(
        "writer, expected_prefix",
        [
            ("e2e_harness", Path("out") / "tests"),
            ("statusbar_e2e", Path("out") / "tests"),
        ],
    )
    def test_writers_under_tests_e2e(self, writer, expected_prefix):
        # Late import: tests.e2e.harness pulls heavy desktop deps that may be
        # mid-refactor; defer until the test actually runs.
        if writer == "e2e_harness":
            from tests.e2e.harness import OUT_DIR  # noqa: PLC0415

            rel = OUT_DIR.as_posix()
            assert rel.startswith(expected_prefix.as_posix())
            # New layout: one dated run folder per run, with the test type nested
            # under it — out/tests/{session_ts}/e2e (not out/tests/e2e/{session_ts}).
            assert re.search(r"out/tests/\d{8}_\d{6}/e2e$", rel), rel
        else:
            # statusbar test imports OUT_DIR from harness — check the import exists.
            src = (
                Path(__file__).parent / "e2e" / "test_extension_statusbar.py"
            ).read_text()
            assert "from .harness import OUT_DIR" in src
