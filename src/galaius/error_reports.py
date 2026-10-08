"""Error reports this computer prepares when it meets a problem, and sends only after its owner says yes
on the PC's page in the web (owner 2026-10-08: « on error, ask to send the logs to the server to
understand ? (just a normal report thing) »; « After install, everything should be done from the web »:
never a question in a terminal).

Every problem the PC already tells its page (`MachineRunner.report_problem`: it could not start or reach
the server, its connection crashed) also prepares a report: the problem in one line and the lines leading
to it — galaius's own recent log, the crash's traceback — with credentials and this person's home folder
masked. The draft stays here (`<config>/error-reports/<id>.json`, private) and only a QUESTION goes to the
server (`MachineErrorAsk`: one line, no log). The background connection looks while a draft waits
(`deliver`): the one report its owner accepted is uploaded once (`MachineErrorUpload`) and its draft
removed; a draft the server no longer waits for, or older than a week, is dropped."""

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from uuid import UUID, uuid4

import httpx
from galaius_core.error_reports import MachineErrorAsk, MachineErrorKind, MachineErrorUpload, MachineErrorUploadRequest
from pydantic import BaseModel, ConfigDict, ValidationError

from .machine_agents import redact
from .private_files import PRIVATE_FILES

logger = logging.getLogger(__name__)
#: The line a Python traceback ends on: the raised error, named.
_RAISED = re.compile(r"^\w+(\.\w+)*(Error|Exception|Exit|Interrupt)\b")


class ErrorReportDraft(BaseModel):
    """One report prepared here and not sent: what its question said, and what its upload will carry."""

    model_config = ConfigDict(frozen=True)
    id: UUID
    kind: MachineErrorKind
    message: str
    detail: str
    created_at: datetime


class MachineErrorReports(BaseModel):
    """The drafts of one computer (`folder`) and their way to the server, with the machine's own token."""

    model_config = ConfigDict(frozen=True)
    folder: Path
    #: The server takes at most this many characters of log (`MachineReportText`): the END is kept.
    MAX_DETAIL: ClassVar[int] = 262_144
    #: One line of the question (`MachineErrorAsk.message`).
    MAX_MESSAGE: ClassVar[int] = 600
    keep: timedelta = timedelta(days=7)

    def prepare(self, config, kind: MachineErrorKind, message: str, lines: Iterable[str], secrets: tuple[str, ...] = ()) -> UUID | None:
        """Keeps the report and asks its owner (best effort, never raises): its id, None when it could not
        be kept. The same problem already waiting is asked once, never again per restart."""
        token = config.token.get_secret_value()
        said = self._masked(message, (token, *secrets)).strip().splitlines()[0][: self.MAX_MESSAGE] if message.strip() else kind
        waiting = next((draft for draft in self.drafts() if draft.message == said), None)
        if waiting is not None:
            return waiting.id
        detail = self._masked("\n".join(lines), (token, *secrets))[-self.MAX_DETAIL:].lstrip("\n") or said
        draft = ErrorReportDraft(id=uuid4(), kind=kind, message=said, detail=detail, created_at=datetime.now(UTC))
        try:
            PRIVATE_FILES.write_text(PRIVATE_FILES.directory(self.folder) / f"{draft.id}.json", draft.model_dump_json())
        except OSError:
            logger.exception("error report not kept")
            return None
        try:
            httpx.post(config.endpoint("/v1/machine/error-report"), json=MachineErrorAsk(id=draft.id, kind=kind, message=said).model_dump(mode="json"),
                       headers=config.authorization, timeout=10)
        except httpx.HTTPError:
            pass  # the question is asked again with the next problem; the draft waits either way
        return draft.id

    def deliver(self, config) -> int:
        """Uploads the report its owner accepted, if it is one of these drafts: 1 when one left, else 0.
        Drafts past `keep`, and one the server no longer waits for, are removed."""
        drafts = {draft.id: draft for draft in self.drafts()}
        if not drafts:
            return 0
        try:
            asked = httpx.get(config.endpoint("/v1/machine/error-report"), headers=config.authorization, timeout=10)
            if asked.status_code != 200:
                return 0
            wanted = MachineErrorUploadRequest.model_validate_json(asked.content).id
            draft = drafts.get(wanted)
            if draft is None:
                return 0
            sent = httpx.put(config.endpoint(f"/v1/machine/error-report/{draft.id}"), json=MachineErrorUpload(detail=draft.detail).model_dump(mode="json"),
                             headers=config.authorization, timeout=60)
        except (httpx.HTTPError, ValidationError):
            return 0
        if sent.status_code in (201, 404, 409):  # sent now, or no longer wanted: the draft has done its part
            (self.folder / f"{draft.id}.json").unlink(missing_ok=True)
        return int(sent.status_code == 201)

    def drafts(self) -> list[ErrorReportDraft]:
        """The drafts kept here, oldest first; an unreadable one, or one past `keep`, is removed."""
        if not self.folder.is_dir():
            return []
        kept: list[ErrorReportDraft] = []
        for path in sorted(self.folder.glob("*.json")):
            try:
                draft = ErrorReportDraft.model_validate_json(PRIVATE_FILES.read_text(path))
            except (OSError, ValueError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
                continue
            if draft.created_at < datetime.now(UTC) - self.keep:
                path.unlink(missing_ok=True)
                continue
            kept.append(draft)
        return sorted(kept, key=lambda draft: draft.created_at)

    @staticmethod
    def own_lines(text: str) -> tuple[str, ...]:
        """From a service's log (`MachineService.last_words`), only what galaius itself wrote: its JSON
        lines (`JsonLines`, read back as one plain line each) and Python's traceback lines. A program the
        PC ran may write into the same log (its children share the streams); its words are its owner's
        data, never part of a report (threat model 2026-10-08 S1)."""
        kept: list[str] = []
        traceback = False  # inside « Traceback (most recent call last): », its indented frames, until the error line
        for line in text.splitlines():
            if line.startswith("Traceback (most recent call last):") or (traceback and line.startswith("  ")):
                traceback = True
                kept.append(line)
                continue
            if traceback and _RAISED.match(line):
                kept.append(line)
            traceback = False
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and str(record.get("name", "")).startswith("galaius"):
                exception = f" ({record['exception']})" if record.get("exception") else ""
                kept.append(f"{record.get('ts', '')} {record.get('level', '')} {record['name']}: {record.get('message', '')}{exception}")
        return tuple(kept)

    @staticmethod
    def _masked(text: str, secrets: tuple[str, ...]) -> str:
        """Credentials (`redact`) and this person's home folder (`~`) out of `text`."""
        home = str(Path.home())
        return redact(text, secrets).replace(home, "~") if len(home) > 1 else redact(text, secrets)
