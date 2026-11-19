#!/usr/bin/env python3
"""A stand-in `claude` for sign-in tests: its login lives in `$CLAUDE_CONFIG_DIR` (else `~/.claude`)
as Claude Code's does, `auth login` writes it there and waits on stdin like the real one, `auth
logout` removes it; every `auth status` is counted in `$CLAUDE_STATUS_LOG` when set."""

import json
import os
import pathlib
import sys

home = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path(os.environ["HOME"]) / ".claude")
credential = home / ".credentials.json"
said = sys.argv[1:]
if said == ["--version"]:
    print("2.1.999 (Claude Code)")
elif said == ["auth", "status"]:
    if os.environ.get("CLAUDE_STATUS_LOG"):
        with open(os.environ["CLAUDE_STATUS_LOG"], "a") as log:
            log.write(f"{home}\n")
    email = json.loads(credential.read_text())["email"] if credential.exists() else None
    print(json.dumps({"loggedIn": email is not None, "authMethod": "claude.ai", "email": email}))
elif said == ["auth", "login"]:
    home.mkdir(parents=True, exist_ok=True)
    credential.write_text(json.dumps({"email": "second@example.com", "claudeAiOauth": {"accessToken": "sk-ant-oat01-" + "s" * 48}}))
    sys.stdin.read()
elif said == ["auth", "logout"]:
    credential.unlink(missing_ok=True)
