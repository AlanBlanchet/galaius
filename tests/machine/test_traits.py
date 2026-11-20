"""The runner's traits share one declared contract: a `self.` attribute a trait reads is declared by the trait
itself or by a class it inherits (`Shared`, `MachineFile`, `Events`, another trait it builds on), never assumed
to exist on `MachineRunner`."""

import ast
import inspect
import sys

import pytest

from galaius.machine.connection import answers, commands, runs, shared, upkeep, watches

TRAITS = (answers.Answers, commands.Commands, runs.Runs, upkeep.Upkeep, watches.Watches)


def _declared(trait: type) -> set[str]:
    names = set(dir(trait))
    for cls in trait.__mro__:
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
    unknown = _read(tree) - _declared(trait) - _assigned(tree)
    assert not unknown, f"{trait.__name__} reads {sorted(unknown)} that neither it nor a class it inherits declares (declare them in the trait, or inherit the one that does)"


def test_every_trait_builds_on_the_shared_declarations() -> None:
    assert all(issubclass(trait, shared.Shared) for trait in TRAITS)
