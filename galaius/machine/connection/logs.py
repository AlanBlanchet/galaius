"""Log lines of one command, kept and forwarded as JSON lines."""

import json
import logging
import os
from datetime import UTC, datetime
from galaius_core import MachineCommand
from galaius.diagnostics.redaction import redact
from galaius.workflows.functions import FunctionRegistry


#: Runner log lines at or above this level travel to the server inside the command they belong to.
FORWARDED_LOG_LEVEL = logging.INFO


def level_word(record: logging.LogRecord) -> str:
    """The server's level vocabulary (debug / info / warn / error) for a stdlib record."""
    return {"WARNING": "warn", "CRITICAL": "error"}.get(record.levelname, record.levelname.lower())


class JsonLines(logging.Formatter):
    """One JSON object per line (journald, or the Windows service's log file, keeps it): the
    server's record fields, `source=machine`, plus whatever the call site bound in `extra`
    (machine, workspace, command, run, node); credential shapes masked (`redact`)."""

    _BOUND = ("machine_id", "workspace_id", "command_id", "run_id", "node_id", "action", "status", "duration_ms")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(), "level": level_word(record),
            "source": "machine", "name": record.name, "message": redact(record.getMessage()),
        }
        payload.update({key: str(getattr(record, key)) for key in self._BOUND if getattr(record, key, None) is not None})
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = redact(f"{type(record.exc_info[1]).__name__}: {record.exc_info[1]}")
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


class CommandLogs(logging.Handler):
    """Collects the runner's own log lines while one command runs, so they reach the server as
    `progress` events with `kind: log` — inside that command's span on the run's trace."""

    def __init__(self) -> None:
        super().__init__(FORWARDED_LOG_LEVEL)
        self.lines: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.lines) < 200:
            # `at`: when the line was written; the lines travel after the step, so the send time would lie.
            self.lines.append({"kind": "log", "level": level_word(record), "logger": record.name, "text": record.getMessage()[:2000], "at": datetime.fromtimestamp(record.created, UTC).isoformat()})

    def drain(self) -> list[dict[str, object]]:
        lines, self.lines = self.lines, []
        return lines


#: Agent events a machine forwards to the server while a step runs (the step's live transcript).
FORWARDED_EVENT_KINDS: frozenset[str] = frozenset({"text", "tool", "tool_result", "thinking", "error", "rate_limit", "done"})


def audit(log: str, entry: dict[str, object]) -> None:
    """Appends one timestamped JSON line to a local, owner-only audit log beside the runner's
    configuration: what workflows made this machine do, surviving a server outage."""
    path = FunctionRegistry().config_path.parent / log
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp": datetime.now(UTC).isoformat(), **entry}, separators=(",", ":")) + "\n")


def audit_script(command: MachineCommand, path: str | None = None) -> None:
    """A durable local record of every script ATTEMPT (not just successes): source digest,
    the workflow/node/run that triggered it. Threat-modeler mitigation #4 — deliberately
    machine-side, next to the running process, so the record survives even a server outage;
    pairs with the server's OWN `machine_script_approvals.approved_by_account_id` for who
    allowlisted the digest in the first place."""
    audit("script-audit.log", {
        "language": command.impl.language, "source_digest": command.impl.source_digest, "approval_digest": command.impl.approval_digest(command.config), "workflow_id": str(command.workflow.key.id),
        "workflow_revision": str(command.workflow.revision), "node_id": str(command.node_id), "run_id": str(command.run_id),
        **({"path": path} if path is not None else {}),
    })
