"""Build acceptance for the post-split public/separate repository pair: the public wheel must
carry only the public import plus its pinned `interact-core` git dependency (never a local
`interact_core` package folded in), and the client codegen script that runs before it must
resolve that same split deterministically (pinned git dep vs an editable sibling checkout).
"""

import os
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from interact.agents.events import AgentEvent
from interact.agents.protocol import (
    ConversationCommand,
    ConversationResponse,
    ConversationStreamEvent,
)
from interact.agents.registry import AgentRun


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def build_cache():
    """Build tools use the runner's cache; per-test HOME isolates user settings."""
    return subprocess.run(["uv", "cache", "dir"], check=True, capture_output=True, text=True).stdout.strip()


def test_root_build_configuration_targets_only_the_new_python_packages() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert configuration["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/interact",
    ]
    assert configuration["project"]["scripts"]["interact"] == "interact.cli:main"


def test_root_wheel_contains_only_the_local_public_import_and_cli(tmp_path: Path, build_cache: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", ".gitignore"):
        shutil.copyfile(ROOT / name, source / name)
    shutil.copytree(ROOT / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run(
        ["uv", "build", "--wheel", "--cache-dir", build_cache, "--out-dir", str(tmp_path)],
        cwd=source,
        capture_output=True,
        text=True,
        env=os.environ | {"UV_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    wheel, = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert any(name.startswith("interact/") for name in names)
        assert not any(name.startswith("interact_core/") for name in names)
        entry_points, = (name for name in names if name.endswith(".dist-info/entry_points.txt"))
        assert "interact = interact.cli:main" in archive.read(entry_points).decode()
        metadata_name, = (name for name in names if name.endswith(".dist-info/METADATA"))
        assert re.search(r"^Requires-Dist: interact-core\s*@ git\+https://github\.com/AlanBlanchet/interact-core\.git@[0-9a-f]{40}$", archive.read(metadata_name).decode(), re.MULTILINE)
    subprocess.run(
        [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import interact", str(wheel)],
        cwd=tmp_path,
        check=True,
    )


# ── Client codegen: same split, resolved before the build runs ─────────────────────────────
# `generate-types.sh` imports catalog modules from `interact-core`; it must resolve the same
# pinned-git-vs-editable-sibling split the wheel build above resolves, deterministically.


@pytest.mark.parametrize("sibling", [False, True], ids=["released-core", "editable-core"])
def test_type_generation_disables_external_catalog_discovery(tmp_path, sibling) -> None:
    """Codegen imports catalog modules; its launcher must make that import deterministic."""
    repo = tmp_path / "public"
    script = repo / "clients/vscode/scripts/generate-types.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile("clients/vscode/scripts/generate-types.sh", script)
    core = tmp_path / "interact-core"
    if sibling:
        core.mkdir()
        (core / "pyproject.toml").touch()
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    capture = tmp_path / "invocation"
    uv = binary_dir / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$LITELLM_LOCAL_MODEL_COST_MAP" "$OLLAMA_DISCOVERY" "$@" '
        '> "$CODEGEN_CAPTURE"\nexit 1\n'
    )
    uv.chmod(0o700)
    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env=os.environ | {"PATH": f"{binary_dir}:{os.environ['PATH']}",
                          "CODEGEN_CAPTURE": str(capture)},
    )
    assert result.returncode == 0, result.stderr
    assert "pydantic-to-typescript not installed; skipping" in result.stderr
    expected = ["True", "0", "run", "--directory", str(repo)]
    if sibling:
        expected += ["--with-editable", str(repo / ".." / "interact-core")]
    assert capture.read_text().splitlines() == expected + ["python", "-c", "import pydantic2ts"]


def test_generation_emits_exhaustive_runtime_decoders_from_python_wire_union() -> None:
    """Every Python wire variant must be accepted/rejected by generated executable validation."""
    generated = Path("clients/vscode/src/generated/types.ts").read_text()
    for model in (
        ConversationCommand, ConversationResponse, ConversationStreamEvent, AgentRun, AgentEvent,
    ):
        assert model.__name__ in generated
        assert f"decode{model.__name__}" in generated
    client = Path("clients/vscode/src/conversationClient.ts").read_text()
    assert "function isResponse" not in client
    assert "function isEvent" not in client
