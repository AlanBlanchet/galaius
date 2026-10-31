"""CLI for registering and testing functions this machine can run from a workflow."""

import json
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter

from galaius.functions import FunctionRegistry, discover_python, discover_shell, invoke

function_app = App(name="functions", help="Register and test typed functions this machine can run from a workflow.")


def _parse_pairs(pairs: list[str]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator:
            raise SystemExit(f"expected key=value, got {pair!r}")
        try:
            parsed[key] = json.loads(value)
        except ValueError:
            parsed[key] = value
    return parsed


@function_app.command(name="add")
def functions_add(path_or_name: str, *command: str, description: str | None = None, permission: str = "read_only") -> None:
    """Register `PATH.py`'s `@galaius.function` callables, or `NAME -- cmd {arg}` as a shell
    function — a `--` before the command tokens ends option parsing, exactly like `env`."""
    registry = FunctionRegistry()
    if command:
        entry = discover_shell(path_or_name, description or path_or_name, tuple(command), permission="full_access")
        registry.add(entry)
        print(json.dumps({"ok": True, "added": [entry.name]}))
        return
    entries = discover_python(Path(path_or_name))
    for entry in entries:
        registry.add(entry)
    print(json.dumps({"ok": True, "added": [entry.name for entry in entries]}))


@function_app.command(name="list")
def functions_list() -> None:
    """Every registered function: name, kind, permission, and typed ports."""
    print(json.dumps([entry.model_dump(mode="json", exclude={"path", "attribute", "command"}) for entry in FunctionRegistry().load()], indent=2))


@function_app.command(name="remove")
def functions_remove(name: str) -> None:
    """Drop a registered function by name."""
    removed = FunctionRegistry().remove(name)
    print(json.dumps({"ok": removed}))
    if not removed:
        raise SystemExit(1)


@function_app.command(name="test")
def functions_test(name: str, *, arg: Annotated[list[str] | None, Parameter(name="--arg")] = None) -> None:
    """Run a registered function locally with `--arg k=v` arguments, bypassing the server."""
    entry = FunctionRegistry().get(name)
    try:
        result = invoke(entry, _parse_pairs(arg or []), working_directory=Path.cwd())
        print(json.dumps({"ok": True, "result": result}, default=str))
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        raise SystemExit(1) from None
