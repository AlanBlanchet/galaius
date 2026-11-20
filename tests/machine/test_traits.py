"""The runner's traits share one declared contract: a `self.` attribute a trait reads is declared by the
runner or a trait (class attribute, annotation or method), never silently assumed from another file."""

import ast
import inspect
import sys

import pytest

from galaius.machine.connection import answers, commands, runs, shared, upkeep, watches
from galaius.machine.connection.runner import MachineRunner

TRAITS = (answers.Answers, commands.Commands, runs.Runs, upkeep.Upkeep, watches.Watches)


def _declared() -> set[str]:
    names = set(dir(MachineRunner))
    for cls in MachineRunner.__mro__:
        names.update(getattr(cls, "__annotations__", {}))
    return names


def _assigned(tree: ast.AST) -> set[str]:
    """Attributes the class itself sets on `self`."""
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self" and isinstance(node.ctx, ast.Store)}


def _read(tree: ast.AST) -> set[str]:
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id in {"self", "cls"}}


@pytest.mark.parametrize("trait", TRAITS, ids=lambda trait: trait.__name__)
def test_a_trait_reads_only_declared_attributes(trait: type) -> None:
    tree = ast.parse(inspect.getsource(sys.modules[trait.__module__]))
    unknown = _read(tree) - _declared() - _assigned(tree)
    assert not unknown, f"{trait.__name__} reads {sorted(unknown)} that no class of the runner declares (add them to its trait or to `Shared`)"


def test_every_trait_builds_on_the_shared_declarations() -> None:
    assert all(issubclass(trait, shared.Shared) for trait in TRAITS)
