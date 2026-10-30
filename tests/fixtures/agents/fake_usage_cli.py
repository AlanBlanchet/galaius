#!/usr/bin/env python3
"""Stand-in `claude` / `codex` answering only their usage question the way 2.1.288 / 0.155.1 do (shapes recorded
2026-10-13): Claude's `get_usage` control request, Codex's `account/rateLimits/read` on its app-server. A login folder
named by CLAUDE_CONFIG_DIR is a subscription's (80 % of its week); Codex is signed out; a file `silent` in HOME makes
it end without answering, a file `failing` makes Claude answer its request's error (the vendor unreachable), a file
`signed_out` makes it answer as signed out (no plan windows). Claude's `auth status` names one account in every folder."""

import json
import os
import sys
from pathlib import Path

if (Path(os.environ["HOME"]) / "silent").exists():
    sys.exit(0)
if sys.argv[1:3] == ["auth", "status"]:  # one account in every folder: only where it is kept differs (2.1.288's shape)
    folder = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path(os.environ["HOME"]) / ".claude")
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "analyticsDisabled": False,
                      "projectsDirectory": f"{folder}/projects", "configDirectory": folder, "email": "owner@example.com",
                      "orgId": "00000000-0000-4000-8000-000000000001", "orgName": "owner@example.com's Organization", "subscriptionType": "max"}, indent=2))
    sys.exit(0)
if "app-server" in sys.argv:
    for raw in sys.stdin:
        line = json.loads(raw)
        if line.get("id") == 1:
            print(json.dumps({"id": 1, "result": {"userAgent": "galaius/0.155.1", "codexHome": "", "platformFamily": "unix", "platformOs": "linux"}}), flush=True)
        elif line.get("id") == 2:
            print(json.dumps({"id": 2, "error": {"code": -32600, "message": "codex account authentication required to read rate limits"}}), flush=True)
            sys.exit(0)
if (Path(os.environ["HOME"]) / "signed_out").exists():
    request = json.loads(sys.stdin.readline())
    print(json.dumps({"type": "control_response", "response": {"subtype": "success", "request_id": request["request_id"], "response": {
        "subscription_type": None, "rate_limits_available": False, "rate_limits": None, "behaviors": None}}}), flush=True)
    sys.exit(0)
week = 80 if os.environ.get("CLAUDE_CONFIG_DIR") else 36
request = json.loads(sys.stdin.readline())
if (Path(os.environ["HOME"]) / "failing").exists():
    print(json.dumps({"type": "control_response", "response": {"subtype": "error", "request_id": request["request_id"], "error": "fetch failed"}}), flush=True)
    sys.exit(0)
print(json.dumps({"type": "control_response", "response": {"subtype": "success", "request_id": request["request_id"], "response": {
    "subscription_type": "max", "rate_limits_available": True, "rate_limits": {
        "five_hour": {"utilization": 13, "resets_at": "2026-10-13T11:40:00.942517+00:00", "limit_dollars": None},
        "seven_day": {"utilization": week, "resets_at": "2026-10-18T19:00:00.942540+00:00", "limit_dollars": None},
        "seven_day_opus": None, "limits": [{"kind": "session", "group": "session", "percent": 13}]}}}}), flush=True)
