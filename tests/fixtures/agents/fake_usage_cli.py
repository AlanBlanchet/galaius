#!/usr/bin/env python3
"""Stand-in `claude` / `codex` answering only their usage question the way 2.1.288 / 0.155.1 do (shapes recorded
2026-10-13): Claude's `get_usage` control request, Codex's `account/rateLimits/read` on its app-server. A login folder
named by CLAUDE_CONFIG_DIR is a subscription's (80 % of its week); Codex is signed out; a file `silent` in HOME makes
it end without answering, a file `failing` makes Claude answer its request's error (the vendor unreachable)."""

import json
import os
import sys
from pathlib import Path

if (Path(os.environ["HOME"]) / "silent").exists():
    sys.exit(0)
if "app-server" in sys.argv:
    for raw in sys.stdin:
        line = json.loads(raw)
        if line.get("id") == 1:
            print(json.dumps({"id": 1, "result": {"userAgent": "galaius/0.155.1", "codexHome": "", "platformFamily": "unix", "platformOs": "linux"}}), flush=True)
        elif line.get("id") == 2:
            print(json.dumps({"id": 2, "error": {"code": -32600, "message": "codex account authentication required to read rate limits"}}), flush=True)
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
