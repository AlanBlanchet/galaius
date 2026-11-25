"""The stand-in vendor program: answers like the Claude CLI's stream, and refuses on demand.

`python model_table_standin.py --model M` prints one turn of stream-json and exits as the plan says. The plan is the JSON file
named by `STANDIN_PLAN`: `{"<model or *>": {"<turn>": "<cause>"}}`; a turn is counted per session folder (`STANDIN_STATE`), so « the
Nth turn » is the Nth time any model was asked in that run. Causes: `pool` (a Fable-pool limit), `week` (the login's own window),
`overloaded` (a 529), `login_lost`, `refusal0` (the limit said as a finished answer, exit 0). A turn the plan does not name works. `STANDIN_DELAY` seconds pass before a turn answers; with `STANDIN_WORK` it speaks first, so a refusal after it cuts a run that worked.
Never a real vendor, never an account: it reads and writes nothing but those two paths."""

import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta

SAID = {
    "pool": "You've reached your Fable limit. Switch to another model, or manage usage credits at https://claude.ai/settings/usage.",
    "overloaded": "API Error: 529 Overloaded. This is a server-side issue, usually temporary — try again in a moment.",
    "login_lost": "Not logged in · Please run /login",
}


def _week() -> str:
    reset = datetime.now(UTC) + timedelta(days=2)
    return f"You've hit your weekly limit · resets {reset:%b} {reset.day}, {reset.hour % 12 or 12}{'am' if reset.hour < 12 else 'pm'} (UTC)"


def main() -> int:
    model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else ""
    state = os.environ["STANDIN_STATE"]
    turn = (int(open(state).read()) if os.path.exists(state) else 0) + 1
    open(state, "w").write(str(turn))
    open(state + ".log", "a").write(f"{turn} {model}\n")
    plan = json.load(open(os.environ["STANDIN_PLAN"]))
    time.sleep(float(os.environ.get("STANDIN_DELAY", "0")))   # a refusal that comes after the launch's own probe window: the run is cut mid-work
    cause = (plan.get(model) or {}).get(str(turn)) or (plan.get("*") or {}).get(str(turn))
    sys.stdout.write(json.dumps({"type": "system", "subtype": "init", "session_id": "vendor"}) + "\n")
    if cause is None:
        sys.stdout.write(json.dumps({"type": "assistant", "session_id": "vendor", "message": {"content": [{"type": "text", "text": f"turn {turn} on {model}"}]}}) + "\n")
        sys.stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": "vendor", "result": f"turn {turn} on {model}"}) + "\n")
        return 0
    if os.environ.get("STANDIN_WORK"):   # it spoke before it was refused: a run that worked, resumed (never relaunched as a refused first turn)
        sys.stdout.write(json.dumps({"type": "assistant", "session_id": "vendor", "message": {"model": model, "content": [{"type": "text", "text": "working"}]}}) + "\n")
    said = _week() if cause == "week" else SAID.get(cause, SAID["pool"])
    sys.stdout.write(json.dumps({"type": "result", "subtype": "success" if cause == "refusal0" else "error", "is_error": cause != "refusal0",
                                 "session_id": "vendor", "result": said}) + "\n")
    return 0 if cause == "refusal0" else 1


if __name__ == "__main__":
    raise SystemExit(main())
