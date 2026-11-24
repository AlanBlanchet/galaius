"""Every action class `actions/models.py` defines reaches the agent: in the schema union the
runner parses with, in the announcement snapshot a client's handshake is answered from, and by
name in the `run_actions` description. `press` shipped as a class and a docstring while the
union refused it (a call failed schema validation); a class missing from any of the three is red."""

import inspect
import json
import re
import typing

from galaius.actions import models
from galaius.data import PackageData


def _action_types() -> dict[str, str]:
    """Wire `type` -> class name, for each concrete action class (those with a `type` literal)."""
    found = {}
    for name, cls in inspect.getmembers(models, inspect.isclass):
        field = cls.model_fields.get("type") if issubclass(cls, models.Action) else None
        if field is not None:
            found[field.default] = name
    return found


def _union_types() -> set[str]:
    members = typing.get_args(typing.get_args(models.AnyAction)[0])
    return {cls.model_fields["type"].default for cls in members}


def _schema_types(schema: dict) -> set[str]:
    """Every wire tag the schema's `type` properties accept (a single `const`, or an `enum` of aliases)."""
    tags = (d.get("properties", {}).get("type", {}) for d in schema["$defs"].values())
    return {tag for t in tags for tag in ([t["const"]] if "const" in t else t.get("enum", []))}


def test_every_action_class_is_in_the_union_the_announcement_and_the_description():
    declared = _action_types()
    assert len(declared) > 20, "the walk found no action classes: the guard itself is broken"
    announced = json.loads(PackageData.read(PackageData.MCP_ANNOUNCEMENT))["lists"]["tools/list"]["tools"]
    tool = next(t for t in announced if t["name"] == "run_actions")
    schema_types = _schema_types(tool["inputSchema"])
    description = tool["description"]

    missing = {
        "the AnyAction union": sorted(set(declared) - _union_types()),
        "the announced run_actions schema": sorted(set(declared) - schema_types),
        "the run_actions description": sorted(t for t in declared if not re.search(rf"\b{t}\b", description)),
    }
    assert not any(missing.values()), f"action classes missing from: {missing}"
