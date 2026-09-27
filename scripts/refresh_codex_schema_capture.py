"""Refresh the compact, review-bound Codex server-request schema capture.

The command consumes an already generated upstream schema directory. Generating that
directory is deliberately separate so ordinary tests never execute Codex or inspect auth.
"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

SOURCE_VERSION = "codex-cli 0.150.0-alpha.8"
SOURCE_EXECUTABLE_SHA256 = (
    "ec77cf443344309a29859e16204cac6f0bae162ffd39427e94f24aeebb5e3d68"
)
PORTABLE_BUNDLE_SHA256 = (
    "9178ce22748c600fddb1bb0b969243f77c3a2f30eb4a6ce6ae62f8e891f14d8e"
)
SUPPORTED = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/tool/requestUserInput",
    }
)


def _canonical(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _example(schema: dict[str, Any], definitions: dict[str, Any]) -> Any:
    if schema is True:
        return {}
    if schema is False:
        raise ValueError("cannot synthesize a value for a false schema")
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _example(definitions[name], definitions)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    alternatives = schema.get("oneOf") or schema.get("anyOf")
    if alternatives:
        selected = _example(alternatives[0], definitions)
        if "properties" not in schema or not isinstance(selected, dict):
            return selected
        required = {
            key: _example(schema["properties"][key], definitions)
            for key in schema.get("required", [])
        }
        return {**selected, **required}
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "null")
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        return {
            key: _example(properties[key], definitions)
            for key in schema.get("required", [])
        }
    if kind == "array":
        return [_example(schema.get("items", {}), definitions)]
    if kind == "boolean":
        return False
    if kind in {"integer", "number"}:
        return max(1, int(schema.get("minimum", 1)))
    if kind == "null":
        return None
    if schema.get("format") == "uri":
        return "https://example.invalid/"
    return "/" if schema.get("description", "").startswith("A path") else "value"


def _response_name(params_name: str) -> str:
    if not params_name.endswith("Params"):
        raise ValueError(f"unexpected request params type: {params_name}")
    return f"{params_name.removesuffix('Params')}Response"


def refresh(source: Path, destination: Path) -> None:
    root = json.loads((source / "ServerRequest.json").read_text())
    definitions = root["definitions"]
    destination.mkdir(parents=True, exist_ok=True)
    request_refs: list[dict[str, str]] = []
    entries: list[dict[str, Any]] = []
    for variant in root["oneOf"]:
        method = variant["properties"]["method"]["enum"][0]
        params_ref = variant["properties"]["params"]["$ref"]
        params_name = params_ref.rsplit("/", 1)[-1]
        request_name = f"{params_name.removesuffix('Params')}Request.json"
        request_schema = {
            "$schema": root["$schema"],
            "definitions": definitions,
            **variant,
        }
        (destination / request_name).write_text(_canonical(request_schema))
        request_ref = f"./{request_name}"
        request_refs.append({"$ref": request_ref})
        response_name = _response_name(params_name) + ".json"
        response_source = source / response_name
        if not response_source.is_file():
            raise FileNotFoundError(response_source)
        (destination / response_name).write_text(
            _canonical(json.loads(response_source.read_text()))
        )
        params = _example(definitions[params_name], definitions)
        values: dict[str, Any]
        if method == "item/tool/requestUserInput":
            questions = params.get("questions", [])
            values = {question["id"]: "value" for question in questions}
        else:
            values = {"decision": "decline"}
        entries.append(
            {
                "method": method,
                "policy": "supported" if method in SUPPORTED else "reject",
                "request_ref": request_ref,
                "response_ref": response_name,
                "required_params": definitions[params_name].get("required", []),
                "representative_request": {
                    "id": "request",
                    "method": method,
                    "params": params,
                },
                "representative_interaction_values": values,
            }
        )
    server_request = {
        "$schema": root["$schema"],
        "title": root["title"],
        "oneOf": request_refs,
    }
    server_text = _canonical(server_request)
    (destination / "ServerRequest.json").write_text(server_text)
    manifest = {
        "source_version": SOURCE_VERSION,
        "source_executable_sha256": SOURCE_EXECUTABLE_SHA256,
        "portable_bundle_sha256": PORTABLE_BUNDLE_SHA256,
        "server_request_sha256": hashlib.sha256(server_text.encode()).hexdigest(),
        "experimental_api": False,
        "server_requests": entries,
    }
    (destination / "manifest.json").write_text(_canonical(manifest))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("src/interact/agents/codex_app_server_schema"),
    )
    arguments = parser.parse_args()
    refresh(arguments.source, arguments.destination)


if __name__ == "__main__":
    main()
