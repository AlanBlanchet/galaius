"""The hook a Claude agent turn runs as it ends (`Stop`): a close saying « RUNNING » with no run reporting back to it is
refused, with the reason (`galaius.agents.turns.running_claim.refusal`), so the agent dispatches the work or closes on
what is true.

Called as `python -m galaius.close_hook RUN_ID` (`registry.close_hook`), with the hook's event on stdin. Standard library
only, like `galaius.inbox_hook`: it runs at every turn's end, and only a close stating RUNNING (`claimed`) pays for the
runtime's run records, read by `running_claim` in its own process. Refused at most `CAP` times in a row: a close the
agent keeps after that stands, never a turn looping on its own end."""

import json
import re
import subprocess
import sys
from pathlib import Path

CAP = 2
#: What the records' read may take; past it the close stands (the CLI's own limit for this hook is `TIMEOUT` + 15).
TIMEOUT = 45
#: A status cell or stamp line saying RUNNING, after a list mark or one label (« 1. », « Stamp: »), never the word inside
#: prose (« a RUNNING close never … ») nor a name it begins (`RUNNING-CHECK.md`).
STATUS = re.compile(r"(?:^|\|)[\s*_`>-]*(?:\d+[.)]\s*)?(?:[A-Za-z]+[*_`]*\s*:[\s*_`]*)?RUNNING[*_`]*(?:\s*[:(—|]|\s+-|\s*$)")


def claimed(text: str) -> tuple[str, ...]:
    """The lines of `text` stating RUNNING."""
    return tuple(line.strip() for line in text.splitlines() if STATUS.search(line))


def _closing_words(event: dict) -> str:
    """The turn's last words: the event's, else the newest assistant text of its transcript."""
    if isinstance(said := event.get("last_assistant_message"), str):
        return said
    try:
        lines = Path(str(event.get("transcript_path") or "")).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) and entry.get("type") == "assistant" else None
        texts = [part.get("text", "") for part in content or () if isinstance(part, dict) and part.get("type") == "text"]
        if any(texts):
            return "\n".join(texts)
    return ""


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(event, dict) or not claimed(words := _closing_words(event)):
        return
    run_id, records = sys.argv[1], sys.argv[2]
    count = Path(records) / f"{run_id}.close-refusals"
    said = count.read_text().strip() if event.get("stop_hook_active") and count.is_file() else ""
    refused = int(said) if said.isdigit() else 0
    if refused >= CAP:
        count.unlink(missing_ok=True)
        return
    try:
        reason = subprocess.run([sys.executable, "-P", "-m", "galaius.agents.turns.running_claim", run_id], input=words,
                                capture_output=True, text=True, timeout=TIMEOUT, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"galaius: the RUNNING close was not checked: {error}")
    if not reason:
        count.unlink(missing_ok=True)
        return
    count.write_text(str(refused + 1))
    print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
