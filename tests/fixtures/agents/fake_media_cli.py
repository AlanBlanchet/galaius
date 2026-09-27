#!/usr/bin/env python3
"""Deterministic Claude/Codex JSONL fixture; filename selects provider and scenario.

Tests copy this to ``fake-<provider>-<mode>`` and execute it as a real child process.  Keeping the
program here makes the fake independently readable and prevents test-string quoting from becoming
part of the protocol under test.
"""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time


def _identity() -> tuple[str, str]:
    marker = Path(sys.argv[0]).name.rsplit("--provider-", 1)[1]
    provider, mode = marker.split("--mode-", 1)
    return provider, mode


def _hang(args: list[str]) -> None:
    child = subprocess.Popen([sys.executable, __file__, "sleep-child"])
    Path(args[1]).write_text(str(child.pid))
    time.sleep(30)


def _payload(args: list[str], prompt_stdin: str) -> str:
    cwd = Path.cwd()
    files = [
        {"name": path.name, "mode": stat.S_IMODE(path.stat().st_mode)}
        for path in sorted(cwd.iterdir())
        if path.is_file()
    ]
    return json.dumps(
        {
            "argv": args,
            "stdin": prompt_stdin,
            "cwd": str(cwd),
            "cwd_mode": stat.S_IMODE(cwd.stat().st_mode),
            "files": files,
            "auth_home": {
                key: os.environ[key]
                for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")
                if key in os.environ
            },
            "secret_env": sorted(
                key
                for key in os.environ
                if key.endswith(("_API_KEY", "_AUTH_TOKEN", "_BASE_URL"))
            ),
        }
    )


def _claude(mode: str, payload: str) -> None:
    if mode == "parsed_then_exit":
        print(json.dumps({"type": "assistant", "session_id": "session-parsed", "message": {
            "content": [{"type": "text", "text": "RAW_SECRET_PROMPT " + payload}],
            "usage": {"input_tokens": 17, "output_tokens": 9}}}))
        print("bounded failure", file=sys.stderr)
        raise SystemExit(9)
    if mode == "rate_limit":
        print(json.dumps({"type": "assistant", "session_id": "session-failed", "message": {
            "content": [{"type": "text", "text": "RAW_SECRET_PROMPT"}],
            "usage": {"input_tokens": 17, "output_tokens": 9}}}))
        print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "rateLimitType": "five_hour"}}))
        print(json.dumps({"type": "result", "is_error": True, "result": "quota exhausted"}))
    elif mode == "missing_final":
        print(json.dumps({"type": "assistant", "session_id": "session-failed", "message": {
            "content": [{"type": "text", "text": "RAW_SECRET_PROMPT"}],
            "usage": {"input_tokens": 17, "output_tokens": 9}}}))
    elif mode == "no_message":
        print(json.dumps({"type": "result", "session_id": "session-failed", "is_error": False,
                          "usage": {"input_tokens": 17, "output_tokens": 9}}))
    elif mode == "schema_invalid":
        print(json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": '{"answer":"no","extra":1}'}]}}))
        print(json.dumps({"type": "result", "is_error": False}))
    elif mode == "schema_valid":
        final = json.dumps({"kind": "text", "text": "yes"})
        print(json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": final}]}}))
        print(json.dumps({"type": "result", "is_error": False}))
    elif mode == "structured_only":
        print(json.dumps({"type": "result", "is_error": False,
                          "structured_output": {"kind": "text", "text": "yes"},
                          "usage": {"input_tokens": 4, "output_tokens": 2}}))
    else:
        print(json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": payload}],
            "usage": {"input_tokens": 11, "output_tokens": 7}}}))
        print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.002,
                          "usage": {"input_tokens": 11, "output_tokens": 7}}))


def _codex(mode: str, payload: str) -> None:
    text = json.dumps({"kind": "text", "text": "yes"}) if mode == "schema_valid" else payload
    print(json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": text}}))
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 13, "cached_input_tokens": 2, "output_tokens": 5}}))


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "sleep-child":
        time.sleep(30)
        return
    if args and args[0] == "hang":
        _hang(args)
        return
    provider, mode = _identity()
    if provider == "claude" and args[:2] == ["auth", "status"]:
        authenticated = mode != "unauthenticated"
        print(json.dumps({
            "loggedIn": authenticated,
            "authMethod": "claude.ai" if authenticated else "api_key",
            "email": "must-not-leak",
        }))
        return
    if provider == "codex" and args[:2] == ["login", "status"]:
        print("Logged in using an API key" if mode == "unauthenticated" else "Logged in using ChatGPT")
        return
    if provider == "codex" and args[:2] == ["features", "list"]:
        if mode == "feature_list_failure":
            print("feature discovery unavailable", file=sys.stderr)
            raise SystemExit(2)
        print("apps stable true")
        print("image_generation stable true")
        print("multi_agent stable true")
        print("shell_tool stable true")
        print("unified_exec stable true")
        print("web_search stable false")
        # Proves isolation follows the CLI's capability inventory instead of a stale local list.
        print("future_action_tool under-development true")
        print("memories stable false")
        return
    if args == ["--version"]:
        print(f"fake-{provider} 1.0")
        return
    if mode == "stderr_echo":
        print(args[-1], file=sys.stderr)
        raise SystemExit(7)
    if mode == "timeout":
        time.sleep(30)
        return
    payload = _payload(args, sys.stdin.read())
    _claude(mode, payload) if provider == "claude" else _codex(mode, payload)


if __name__ == "__main__":
    main()
