"""The hook a Claude agent turn runs after each of its own tool calls: it hands the turn the
messages sent to its run meanwhile (`galaius.agents.agent_queue.inject`), as the editor does with a
message typed while it works.

Called as `python -m galaius.inbox_hook RUN_ID QUEUE_FILE`, with the hook's event on stdin. It
runs after EVERY tool call, so the common answer (nothing waiting) costs one small file read and
imports nothing of the launcher; only a waiting message starts the process that claims it. A tool
call of a sub-agent (its event names an `agent_id`) is skipped: the message is for the run's own
agent, which reads it after its next call."""

import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    run_id, queue = sys.argv[1], Path(sys.argv[2])
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(event, dict) or event.get("agent_id"):
        return
    try:
        waiting = b'"pending"' in queue.read_bytes()
    except OSError:
        return
    if waiting:
        raise SystemExit(subprocess.call([sys.executable, "-m", "galaius.agents.agent_queue", "--inject", run_id], stdin=subprocess.DEVNULL))


if __name__ == "__main__":
    main()
