#!/usr/bin/env python3
"""Scan MCP-client transcripts for real-world `interact` tool failures across all projects.

`interact` is consumed by many projects as an MCP server; those clients (Claude Code) record
every tool call + result under ~/.claude/projects. This surfaces the failures that interact's
own tests and the maintainer's runs never reproduce — run it BEFORE each iteration, not only
when a bug is reported (see the "Dogfood Consumer Telemetry" coding rule).

Usage:
    python scripts/scan_client_errors.py                 # errors in the last 24h
    python scripts/scan_client_errors.py --since 2026-06-05T12:00
    python scripts/scan_client_errors.py --hours 72
    python scripts/scan_client_errors.py --all           # whole history
    python scripts/scan_client_errors.py --projects-root ~/.claude/projects

Exit code is always 0 (it is a report, not a gate); a one-line "no new errors" is success.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone

# Markers of an interact tool failure in the returned text (is_error already covers hard errors).
_ERROR_MARKERS = (
    "ERROR:",
    "No window",
    "No monitor",
    "Could not",
    "[Vision",
    "validation error",
    "Timeout",
    "timed out",
    "strict mode",
    "Illegal return",
    "await is only",
    "unavailable",
    "Error executing",
    "Traceback",
)


def _content_blocks(message: object) -> list:
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            return content
    return []


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content)


def _collect(root: str) -> tuple[dict, dict]:
    """Return (uses, results): tool_use_id → (tool, input, project, ts) and id → (text, is_error)."""
    uses: dict[str, tuple] = {}
    results: dict[str, tuple] = {}
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        project = path[len(root):].lstrip("/").split("/", 1)[0]
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            ts = obj.get("timestamp", "")
            for block in _content_blocks(obj.get("message")):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and str(block.get("name", "")).startswith(
                    "mcp__interact__"
                ):
                    uses[block["id"]] = (
                        block["name"].replace("mcp__interact__", ""),
                        block.get("input", {}),
                        project,
                        ts,
                    )
                elif block.get("type") == "tool_result":
                    results[block.get("tool_use_id")] = (
                        _result_text(block),
                        bool(block.get("is_error")),
                    )
    return uses, results


def _normalize(line: str) -> str:
    line = re.sub(r"0x[0-9a-fA-F]+", "0xHEX", line[:160])
    return re.sub(r"\s+", " ", re.sub(r"\d+", "N", line)).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="ISO timestamp; only calls at/after it")
    parser.add_argument("--hours", type=float, default=24.0, help="look back this many hours (default 24)")
    parser.add_argument("--all", action="store_true", help="whole history (ignore --since/--hours)")
    parser.add_argument(
        "--projects-root",
        default=os.path.expanduser("~/.claude/projects"),
        help="MCP client transcript root",
    )
    args = parser.parse_args()

    if args.all:
        since = ""
    elif args.since:
        since = args.since
    else:
        since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()

    uses, results = _collect(args.projects_root)
    recent = [(t, inp, proj, ts, *results.get(tid, ("", False))) for tid, (t, inp, proj, ts) in uses.items() if ts >= since]

    window = "all history" if args.all else f"since {since}"
    print(f"interact tool calls ({window}): {len(recent)} across {len({r[2] for r in recent})} project(s)")

    grouped: dict[tuple, int] = collections.Counter()
    example: dict[tuple, tuple] = {}
    for tool, inp, proj, _ts, text, is_err in recent:
        if not (is_err or any(m in text for m in _ERROR_MARKERS)):
            continue
        line = next((ln for ln in text.splitlines() if any(m in ln for m in _ERROR_MARKERS)), text[:160])
        key = (tool, _normalize(line))
        grouped[key] += 1
        example.setdefault(key, (proj, json.dumps(inp)[:200]))

    if not grouped:
        print("No errors in window. ✅")
        return 0

    print(f"\n{sum(grouped.values())} error result(s):")
    for (tool, msg), count in grouped.most_common():
        proj, inp = example[(tool, msg)]
        print(f"\n[{count}x] {tool}  ({proj})")
        print(f"   {msg}")
        print(f"   in: {inp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
