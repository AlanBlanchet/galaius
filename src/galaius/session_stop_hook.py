"""The hook every Claude Code session of this computer's user runs at each tool call and prompt: a stop
asked from the web for one of HIS editor sessions (`galaius.agents.session_stops`) ends that session's
turn at its next tool call — the tool is not run and the turn ends, as his Esc would end it — and ends
any turn the session starts by itself afterwards, until he types in it again or `HOLD_SECONDS` pass.
A tool call of one of the session's sub-agents is stopped too: the sub-agent ends, then the session's
own turn at the call that started it.

Run from a copy of this file beside the stops (`SessionStops.install`), as `python -I -S hook.py
STOPS_DIR`, the hook's event on stdin: standard library only, so it needs no galaius runtime and outlives
an upgrade. Where a POSIX shell runs it, the shell starts Python only when a stop is waiting
(`SessionStops.hook`), so a session nobody stopped pays nothing. It never fails his tool call:
anything it cannot read leaves the turn alone."""

import json
import sys
import time
from pathlib import Path

#: How long a stop holds a session that keeps starting turns on its own (a background task's
#: completion wakes it): long enough that nothing he stopped is back two minutes later.
HOLD_SECONDS = 600
#: What his editor shows where the turn ended.
SAID = "Stopped from Galaius on the web."


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
        return None
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
