"""The hook every Claude Code session of this computer's user runs at each tool call and prompt: a stop
asked from the web for one of HIS editor sessions (`galaius.agents.turns.session_stops`) ends that session's
turn at its next tool call — the tool is not run and the turn ends, as his Esc would end it — and ends
any turn the session starts by itself afterwards, until he types in it again or `HOLD_SECONDS` pass.
A tool call of one of the session's sub-agents is stopped too: the sub-agent ends, then the session's
own turn at the call that started it.

Run from a copy of this file beside the stops (`SessionStops.install`), as `python -I -S hook.py
STOPS_DIR`, the hook's event on stdin: standard library only, so it needs no galaius runtime and outlives
an upgrade. Where a POSIX shell runs it, the shell starts Python only when a stop is waiting
(`SessionStops.hook`), so a session nobody stopped pays nothing. It never fails his tool call:
anything it cannot read leaves the turn alone. Each turn it ends, and each stop his own prompt clears, is one line of
`LOG` beside the stops, so a stop pressed on the web can be read on this computer afterwards (`trace`)."""

import json
import sys
import time
from pathlib import Path

#: How long a stop holds a session that keeps starting turns on its own (a background task's
#: completion wakes it): long enough that nothing he stopped is back two minutes later.
HOLD_SECONDS = 600
#: What his editor shows where the turn ended.
SAID = "Stopped from Galaius on the web."
#: The hook's own trace beside the stops: one line per turn it ended (`TURN_SECONDS`) or stop his prompt cleared — the session id, the
#: event, the tool's name; never the tool's input nor his prompt. Never `*.json`: every `*.json` there is a stop.
LOG = "stops.log"
#: Past this size the trace keeps only its newer half: it lives as long as his account on this computer.
LOG_BYTES = 64_000
#: Tool calls of one turn reach the hook within this many seconds of each other (parallel calls, the call that
#: ran plus the next): the trace names that turn once. A turn the session starts by itself later is another line.
TURN_SECONDS = 10


def sweep(stops: Path, now: float) -> None:
    """Every stop older than `HOLD_SECONDS` removed: one for a session closed before its next tool call is
    read by no hook of its own, and while any stop file is there every session's pre-check starts Python."""
    try:
        flags = list(stops.glob("*.json"))
    except OSError:
        return
    for flag in flags:
        try:
            if now - flag.stat().st_mtime > HOLD_SECONDS:
                flag.unlink(missing_ok=True)
        except OSError:
            continue


def trace(stops: Path, now: float, line: str, turn_of: str | None = None) -> None:
    """`line` appended to the hook's trace (`LOG`), its older bytes dropped past `LOG_BYTES`. With `turn_of`, a line
    ending a turn: skipped when that session's last line already ended one in the last `TURN_SECONDS` (the same
    turn's other tool calls); a stop his prompt cleared since starts anew. Never raises: a trace it cannot write
    leaves the stop as it is."""
    log = stops / LOG
    try:
        kept = log.read_bytes() if log.is_file() else b""
        if turn_of is not None:
            last = next((old for old in reversed(kept.decode("utf-8", "replace").splitlines()) if f" {turn_of} " in old), "")
            stamp, _, rest = last.partition(" ")
            if rest.startswith("ended ") and stamp >= time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - TURN_SECONDS)):
                return
        if len(kept) > LOG_BYTES:
            kept = kept[-(LOG_BYTES // 2):]
            log.write_bytes(kept[kept.find(b"\n") + 1:])
        with log.open("a", encoding="utf-8", errors="replace") as out:
            out.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))} {' '.join(line.split())[:300]}\n")
    except (OSError, ValueError):  # a name it cannot encode included: the stop still answers
        return


def answer(event: dict, stops: Path, now: float) -> dict | None:
    """What this hook answers `event` with: None to leave the turn alone, else the stop."""
    session = event.get("session_id")
    if not isinstance(session, str) or not session or "/" in session or "\\" in session or session.startswith("."):
        return None
    flag = stops / f"{session}.json"
    try:
        asked = json.loads(flag.read_text(encoding="utf-8")).get("at", 0)
    except (OSError, ValueError, AttributeError):
        return None
    name = event.get("hook_event_name")
    if name == "UserPromptSubmit" or not isinstance(asked, int | float) or now - asked > HOLD_SECONDS:
        flag.unlink(missing_ok=True)  # he typed: his turn; or the stop is old: it holds nothing any more
        if name == "UserPromptSubmit":
            trace(stops, now, f"cleared {session} by his prompt")
        return None
    if name in ("PreToolUse", "PostToolUse"):
        tool = event.get("tool_name")
        trace(stops, now, f"ended {session} at {name} {tool if isinstance(tool, str) else '?'}", turn_of=session)
    if name == "PreToolUse":
        return {"continue": False, "stopReason": SAID,
                "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": SAID}}
    if name == "PostToolUse":
        return {"continue": False, "stopReason": SAID}
    return None


def main() -> None:
    stops, now = Path(sys.argv[1]), time.time()
    sweep(stops, now)
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    said = answer(event, stops, now) if isinstance(event, dict) else None
    if said is not None:
        print(json.dumps(said))


if __name__ == "__main__":
    main()
