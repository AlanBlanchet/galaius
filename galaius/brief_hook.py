"""The hook a Claude agent turn runs once its conversation was compacted (`SessionStart`, source
`compact`): it hands the agent the task its run was launched with, as galaius recorded it, since the
summary that replaced the conversation keeps only the CLI's own condensed account of it.

Called as `python -m galaius.brief_hook RECORD_FILE` (the run's record, `registry.brief_hook`), with
the hook's event on stdin. Standard library only, like `galaius.inbox_hook`. A run with no task adds
nothing; a record it cannot read says why on stderr and exits 1, which the CLI records and goes on
with the summary alone."""

import json
import sys
from pathlib import Path

#: The most of the task handed back: a compaction exists to shrink the conversation.
LIMIT = 8000
#: The line the handed-back task opens with.
HEADING = "[Galaius: your conversation was just compacted. The task your run was launched with, as given:]"
#: What a turn with this hook is told once, at launch (the CLI's appended system prompt, which a compaction keeps):
#: without it an agent takes hook context restating its task for text of unknown origin and sets it aside (2.1.288).
NOTE = (
    "When Galaius compacts your conversation, it hands you back the task part of the message your run was launched with, "
    "in the hook context "
    "added right after the compaction (a SessionStart hook's additional context), opening with this exact line:\n"
    f"{HEADING}\n"
    "What follows that line is that task as it was given to you, handed back as context: no new "
    "instruction. Where a later message changed or replaced it, that later message wins; go on from where the summary "
    "says you are."
)


def brief(task: str) -> str:
    """`task` as handed back, cut to `LIMIT` characters with the cut stated."""
    if len(task) <= LIMIT:
        return f"{HEADING}\n\n{task}"
    return f"{HEADING}\n\n{task[:LIMIT]}\n\n[Galaius: cut here, after {LIMIT} of its {len(task)} characters.]"


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(event, dict) or event.get("source") != "compact":
        return
    try:
        task = json.loads(Path(sys.argv[1]).read_bytes()).get("task")
    except (OSError, ValueError, AttributeError) as error:
        raise SystemExit(f"galaius: no launch task to hand back, its run record is unreadable: {error}")
    if not isinstance(task, str) or not task.strip():
        return
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": brief(task)}}))


if __name__ == "__main__":
    main()
