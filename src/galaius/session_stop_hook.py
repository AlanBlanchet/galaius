"""The hook every Claude Code session of this computer's user runs at each tool call and prompt: a stop
asked from the web for one of HIS editor sessions (`galaius.agents.session_stops`) ends that session's
turn at its next tool call — the tool is not run and the turn ends, as his Esc would end it — and ends
any turn the session starts by itself afterwards, until he types in it again or `HOLD_SECONDS` pass.

Called as `python -m galaius.session_stop_hook STOPS_DIR`, the hook's event on stdin. Standard library
only, like `galaius.inbox_hook`: where a POSIX shell runs it, the shell starts Python only when a stop is
waiting (`SessionStops.command`), so a session nobody stopped pays nothing. It never fails his tool
call: anything it cannot read leaves the turn alone."""

import json
import sys
import time
from pathlib import Path

#: How long a stop holds a session that keeps starting turns on its own (a background task's
#: completion wakes it): long enough that nothing he stopped is back two minutes later.
HOLD_SECONDS = 600
#: What his editor shows where the turn ended.
SAID = "Stopped from Galaius on the web."


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
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(event, dict) or event.get("agent_id"):
        return  # a sub-agent's call: the session's own turn is the one stopped
    said = answer(event, Path(sys.argv[1]), time.time())
    if said is not None:
        print(json.dumps(said))


if __name__ == "__main__":
    main()
