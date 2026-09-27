"""Generated-bound Codex server-request validation.

Generated from ServerRequest.json SHA-256
1b8268c3d7f19f5fea362b7b9f5e964c6ba9c53acb79f86e2b69394ebd719e20.
Refresh with ``scripts/refresh_codex_schema_capture.py``; do not add method tables here.
"""

import json
from importlib.resources import files
from typing import Any

from jsonschema import Draft7Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

_CAPTURE = files("interact.agents").joinpath("codex_app_server_schema")
_SCHEMA_BASE = "https://schemas.interact.invalid/codex-app-server/"
_MANIFEST = json.loads((_CAPTURE / "manifest.json").read_text())
_SERVER_REQUEST = json.loads((_CAPTURE / "ServerRequest.json").read_text())
_SERVER_REQUEST["$id"] = _SCHEMA_BASE + "ServerRequest.json"
_REGISTRY = Registry().with_resources(
    (
        _SCHEMA_BASE + path.name,
        Resource.from_contents(json.loads(path.read_text()), default_specification=DRAFT7),
    )
    for path in _CAPTURE.iterdir()
    if path.name.endswith(".json")
    if path.name != "manifest.json"
)
_VALIDATOR = Draft7Validator(_SERVER_REQUEST, registry=_REGISTRY)
_ENTRIES = {entry["method"]: entry for entry in _MANIFEST["server_requests"]}
SUPPORTED_REQUEST_HANDLERS = frozenset(
    method for method, entry in _ENTRIES.items() if entry["policy"] == "supported"
)
REJECTED_REQUEST_POLICIES = frozenset(_ENTRIES).difference(SUPPORTED_REQUEST_HANDLERS)


def validate_request(frame: dict[str, Any]) -> dict[str, Any]:
    _VALIDATOR.validate(frame)
    return frame


def encode_supported_response(
    request: dict[str, Any], values: dict[str, Any]
) -> dict[str, Any]:
    method = request["method"]
    if method not in SUPPORTED_REQUEST_HANDLERS:
        raise ValueError("request method is not interactive")
    if method == "item/tool/requestUserInput":
        answers = {
            key: {"answers": [str(value).lower() if isinstance(value, bool) else value]}
            for key, value in values.items()
        }
        result: dict[str, Any] = {"answers": answers}
    else:
        result = {"decision": values.get("decision", "decline")}
    entry = _ENTRIES[method]
    response_schema = json.loads((_CAPTURE / entry["response_ref"]).read_text())
    Draft7Validator(response_schema, registry=_REGISTRY).validate(result)
    return result


def reject_unsupported(request: dict[str, Any]) -> dict[str, Any]:
    if request["method"] not in REJECTED_REQUEST_POLICIES:
        raise ValueError("request method is not rejected by policy")
    return {
        "id": request["id"],
        "error": {"code": -32601, "message": "Method not supported by this client."},
    }


__all__: list[str] = []
