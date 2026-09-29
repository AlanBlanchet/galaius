"""Executable acceptance contract for the public production repository layout."""

import ast
import json
from importlib.resources import files
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RELOCATIONS = (
    ("packages/interact-local", "src/interact"),
    ("vscode-extension", "clients/vscode"),
    ("docs", "site"),
)
PROMPT_CONTRACTS = ("PromptKey", "PromptRevision", "PromptChannelEntry", "PromptCatalogPage")


@pytest.mark.parametrize(("old", "new"), RELOCATIONS)
def test_public_products_have_one_physical_home(old: str, new: str) -> None:
    assert not (ROOT / old).exists(), f"obsolete public path remains: {old}"
    assert (ROOT / new).is_dir(), f"production path is absent: {new}"


def test_public_prompt_contracts_are_exported_to_schema_and_vscode() -> None:
    typescript_path = ROOT / "clients/vscode/src/generated/promptContracts.ts"
    assert typescript_path.is_file()

    package = __import__("interact_core", fromlist=list(PROMPT_CONTRACTS))
    assert all(getattr(package, name, None) is not None for name in PROMPT_CONTRACTS)
    schema = files("interact_core").joinpath("schema/prompt-contracts.schema.json")
    definitions = json.loads(schema.read_text())["$defs"]
    assert set(PROMPT_CONTRACTS) <= definitions.keys()
    typescript = typescript_path.read_text()
    assert all(f"export interface {name}" in typescript for name in PROMPT_CONTRACTS)


def test_public_python_imports_no_interact_package_but_core() -> None:
    public_roots = (
        ROOT / "src",
    )
    assert all(root.is_dir() for root in public_roots)
    for root in public_roots:
        for source in root.rglob("*.py"):
            tree = ast.parse(source.read_text(), filename=str(source))
            imports = (
                node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            )
            names = (
                alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
            )
            roots = {(name or "").split(".")[0] for name in (*imports, *names)}
            assert {root for root in roots if root.startswith("interact_")} <= {"interact_core"}, source


def test_precommit_hook_targets_only_the_relocated_release_graph() -> None:
    hook = (ROOT / ".githooks/pre-commit").read_text()

    assert "vscode-extension/" not in hook
    assert "packages/interact-local" not in hook
    assert "clients/vscode" in hook
    assert "^src/interact/" in hook
