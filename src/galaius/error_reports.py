"""Error reports this computer prepares when it meets a problem, and sends only after its owner says yes
on the PC's page in the web (owner 2026-10-08: « on error, ask to send the logs to the server to
understand ? (just a normal report thing) »; « After install, everything should be done from the web »:
never a question in a terminal).

Every problem the PC already tells its page (`MachineRunner.report_problem`: it could not start or reach
the server, its connection crashed) also prepares a report: the problem in one line and the lines leading
to it — galaius's own recent log, the crash's traceback — with credentials, control characters and this
person's home folder masked. The draft stays here (`<config>/error-reports/<id>.json`, private) and only a
QUESTION goes to the server (`MachineErrorAsk`: one line, no log), once per problem (`problem`: its line,
dates and clock times aside), asked again until the server took it — after its Retry-After, else a doubling wait. The
server keeps one open question a PC: a newer problem's question replaces an older one still waiting here.
The background connection looks while a draft waits (`deliver`): the one report its owner accepted is
uploaded once (`MachineErrorUpload`) and its draft removed; a draft the server no longer waits for, or
older than `keep`, is dropped — each drop logged."""

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import ClassVar, Protocol
from uuid import UUID, uuid4

import httpx
from galaius_core.error_reports import MachineErrorAsk, MachineErrorKind, MachineErrorUpload, MachineErrorUploadRequest
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

import galaius

from .machine_agents import redact
from .private_files import PRIVATE_FILES

logger = logging.getLogger(__name__)
#: Characters a report never carries (the server refuses them): controls but tab and newline, ANSI escapes included.
_CONTROLS = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: Where galaius's own code lives: a traceback is galaius's only when one of its frames is in there.
_OWN_CODE = str(Path(galaius.__file__).resolve().parent)
#: A date or clock time in a problem's line (a log line's stamp): the same problem met again differs only there.
_CLOCK = re.compile(r"(?<![\d.:])(?:\d{4}-\d{2}-\d{2}[T ])?\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?(?![\d:])")


class MachineEndpoint(Protocol):
    """What a report needs of this computer's machine settings (`MachineConfig`): its token, and its
    server's addresses with that token on every request."""

    token: SecretStr

    def endpoint(self, path: str, *, socket: bool = False) -> str: ...

    @property
    def authorization(self) -> dict[str, str]: ...


class ErrorReportDraft(BaseModel):
    """One report prepared here and not sent: its question, whether the server took it, and what its
    upload will carry — both already valid wire bodies, so a draft kept here can always be sent."""

    model_config = ConfigDict(frozen=True)
    question: MachineErrorAsk
    upload: MachineErrorUpload
    created_at: datetime
    asked: bool = False
    tries: int = 0
    """Questions the server did not take so far."""
    ask_after: datetime | None = None
    """Its question is not asked before (`MachineErrorReports._later`)."""

    @property
    def id(self) -> UUID:
        return self.question.id

    @property
    def problem(self) -> str:
        """What makes two drafts one problem: its kind and line, dates and clock times aside (2026-10-09:
        the same dropped channel, its line stamped with a new time, was asked again on every drop)."""
        return f"{self.question.kind}: {_CLOCK.sub('', self.question.message)}"


class MachineErrorReports(BaseModel):
    """The drafts of one computer (`folder`) and their way to the server, with the machine's own token."""

    model_config = ConfigDict(frozen=True)
    folder: Path
    #: The server takes at most this many characters of log (`MachineReportText`): the END is kept.
    MAX_DETAIL: ClassVar[int] = 262_144
    #: One line of the question (`MachineErrorAsk.message`).
    MAX_MESSAGE: ClassVar[int] = 600
    #: Lines of a service's log read for a report (`MachineService.last_words`).
    LOG_LINES: ClassVar[int] = 400
    keep: timedelta = timedelta(days=2)
    """A draft older than this is dropped: its owner declined it, or let it pass (an accepted one is
    uploaded within a day); the same problem then asks again."""
    retry: timedelta = timedelta(minutes=1)
    """The wait after a first question not taken, doubled at each next one up to `retry_cap`."""
    retry_cap: timedelta = timedelta(hours=1)
    """The longest wait of its own; the server's `Retry-After` may set a longer one, up to `keep`."""

    def prepare(self, config: MachineEndpoint, kind: MachineErrorKind, message: str, lines: Iterable[str], secrets: tuple[str, ...] = ()) -> ErrorReportDraft | None:
        """Keeps the report and asks its owner (best effort, never raises): the draft, None when it could
        not be kept or the server refused its question for good. The same problem already kept is one
        draft, its question asked until taken; a new problem's question replaces the one still waiting
        here, and takes over its wait."""
        hidden = (config.token.get_secret_value(), *secrets)
        said = " ".join(self._masked(message, hidden).split())[: self.MAX_MESSAGE] or kind  # one line: tabs, newlines folded
        detail = self._masked("\n".join(lines), hidden)[-self.MAX_DETAIL:].lstrip("\n") or said
        try:
            fresh = ErrorReportDraft(question=MachineErrorAsk(id=uuid4(), kind=kind, message=said), upload=MachineErrorUpload(detail=detail), created_at=datetime.now(UTC))
        except ValidationError:
            logger.exception("error report not kept")
            return None
        drafts = self.drafts()
        waiting = next((draft for draft in drafts if draft.problem == fresh.problem), None)
        if waiting is None:
            replaced = next((draft for draft in reversed(drafts) if not draft.asked), None)
            waiting = fresh if replaced is None else fresh.model_copy(update={"tries": replaced.tries, "ask_after": replaced.ask_after})
            try:
                self._keep(waiting)
            except OSError:
                logger.exception("error report not kept")
                return None
            self._replaced([*drafts, waiting])
        return self._ask(config, waiting)

    def deliver(self, config: MachineEndpoint) -> int:
        """Asks again the questions the server has not taken, then uploads the report its owner accepted,
        if it is one of these drafts: 1 when one left, else 0. A draft the server no longer waits for is
        removed."""
        asked = [self._ask(config, kept) for kept in self._replaced(self.drafts())]
        drafts = {draft.id: draft for draft in asked if draft is not None}
        if not drafts:
            return 0
        try:
            asked = httpx.get(config.endpoint("/v1/machine/error-report"), headers=config.authorization, timeout=10)
            if asked.status_code != 200:
                return 0
            draft = drafts.get(MachineErrorUploadRequest.model_validate_json(asked.content).id)
            if draft is None:
                return 0
            sent = httpx.put(config.endpoint(f"/v1/machine/error-report/{draft.id}"), json=draft.upload.model_dump(mode="json"), headers=config.authorization, timeout=60)
        except (httpx.HTTPError, ValidationError) as error:
            logger.warning("error report not delivered now (%s); asked again in a minute", type(error).__name__)
            return 0
        if sent.status_code in (201, 404, 409):  # sent now, or no longer wanted: the draft has done its part
            if sent.status_code != 201:
                logger.info("error report %s no longer wanted by the server (%s): dropped", draft.id, sent.status_code)
            self._path(draft.id).unlink(missing_ok=True)
        else:
            logger.warning("error report %s refused by the server (%s); kept", draft.id, sent.status_code)
        return int(sent.status_code == 201)

    def drafts(self) -> list[ErrorReportDraft]:
        """The drafts kept here, oldest first; an unreadable one, or one past `keep`, is removed (logged)."""
        if not self.folder.is_dir():
            return []
        kept: list[ErrorReportDraft] = []
        for path in sorted(self.folder.glob("*.json")):
            try:
                draft = ErrorReportDraft.model_validate_json(PRIVATE_FILES.read_text(path))
            except (OSError, ValueError) as error:
                logger.warning("error report draft %s unreadable (%s): dropped", path.name, type(error).__name__)
                path.unlink(missing_ok=True)
                continue
            if draft.created_at < datetime.now(UTC) - self.keep:
                logger.info("error report %s not accepted within %s: dropped", draft.id, self.keep)
                path.unlink(missing_ok=True)
                continue
            kept.append(draft)
        return sorted(kept, key=lambda draft: draft.created_at)

    def asked(self) -> bool:
        """Whether a report waits here whose question the server took (its owner can answer it now)."""
        return any(draft.asked for draft in self.drafts())

    def _replaced(self, drafts: list[ErrorReportDraft]) -> list[ErrorReportDraft]:
        """`drafts` (oldest first) but the questions not yet taken older than the newest one: the server
        keeps one open question a PC, so each would only replace the next (logged, dropped)."""
        older = [draft for draft in drafts if not draft.asked][:-1]
        for draft in older:
            logger.info("error report %s replaced by a newer problem before its question was taken: dropped", draft.id)
            self._path(draft.id).unlink(missing_ok=True)
        return [draft for draft in drafts if draft not in older]

    def _ask(self, config: MachineEndpoint, draft: ErrorReportDraft) -> ErrorReportDraft | None:
        """The draft, its question posted unless the server already took it (then marked asked) or its
        wait runs (`ask_after`); None when the server refused it for good (the draft is dropped)."""
        if draft.asked or (draft.ask_after is not None and datetime.now(UTC) < draft.ask_after):
            return draft
        try:
            answer = httpx.post(config.endpoint("/v1/machine/error-report"), json=draft.question.model_dump(mode="json"), headers=config.authorization, timeout=10)
        except httpx.HTTPError as error:
            return self._later(draft, type(error).__name__)
        if not answer.is_success:
            if 400 <= answer.status_code < 500 and answer.status_code not in (408, 429):  # refused for good: never asked again
                logger.warning("error report %s question refused (%s): dropped", draft.id, answer.status_code)
                self._path(draft.id).unlink(missing_ok=True)
                return None
            return self._later(draft, str(answer.status_code), answer.headers.get("Retry-After", ""))
        taken = draft.model_copy(update={"asked": True})
        try:
            self._keep(taken)
        except OSError:
            logger.exception("error report draft not updated")
        return taken

    def _later(self, draft: ErrorReportDraft, cause: str, retry_after: str = "") -> ErrorReportDraft:
        """The draft whose question was not taken, asked again after the server's `Retry-After` (seconds or
        an HTTP date, at most `keep`), else after `retry` doubled at each try up to `retry_cap`: one line
        logged a try, none between."""
        now = datetime.now(UTC)
        wait = min(self.retry * 2 ** min(draft.tries, 16), self.retry_cap)
        if retry_after.isascii() and retry_after.isdecimal():
            wait = timedelta(seconds=min(int(retry_after), int(self.keep.total_seconds())))
        elif retry_after:
            try:
                wait = min(max(parsedate_to_datetime(retry_after) - now, timedelta(0)), self.keep)
            except (TypeError, ValueError):
                pass  # unreadable: its own doubling wait
        delayed = draft.model_copy(update={"tries": draft.tries + 1, "ask_after": now + wait})
        logger.warning("error report question not taken now (%s); asked again in %s", cause, wait)
        try:
            self._keep(delayed)
        except OSError:
            logger.exception("error report draft not updated")
        return delayed

    def _keep(self, draft: ErrorReportDraft) -> None:
        PRIVATE_FILES.directory(self.folder)
        PRIVATE_FILES.write_text(self._path(draft.id), draft.model_dump_json())

    def _path(self, identifier: UUID) -> Path:
        return self.folder / f"{identifier}.json"

    @staticmethod
    def own_lines(text: str) -> tuple[str, ...]:
        """From a service's log (`MachineService.last_words`), only what galaius itself wrote: its JSON
        lines (`JsonLines`, read back as one plain line each) and the tracebacks raised in its own code
        (a frame under the galaius package). A program the PC ran may write into the same log (its
        children share the streams); its words are its owner's data, never part of a report (threat
        model 2026-10-08 S1)."""
        kept: list[str] = []
        block: list[str] = []  # a traceback being read: its head and indented frames, then the error line

        def close(error: str | None) -> None:
            if block and any(line.startswith('  File "') and _OWN_CODE in line for line in block):
                kept.extend((*block, *([error] if error is not None else [])))
            block.clear()

        for line in text.splitlines():
            if line.startswith("Traceback (most recent call last):"):
                close(None)
                block.append(line)
                continue
            if block and line.startswith("  "):
                block.append(line)
                continue
            if block:
                close(line)  # the first line after the frames is the raised error
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and str(record.get("name", "")).startswith("galaius"):
                exception = f" ({record['exception']})" if record.get("exception") else ""
                kept.append(f"{record.get('ts', '')} {record.get('level', '')} {record['name']}: {record.get('message', '')}{exception}")
        close(None)
        return tuple(kept)

    @staticmethod
    def _masked(text: str, secrets: tuple[str, ...]) -> str:
        """Credentials (`redact`), control characters and this person's home folder (`~`) out of `text`."""
        home = str(Path.home())
        masked = _CONTROLS.sub("", redact(text, secrets))
        return masked.replace(home, "~") if len(home) > 1 else masked
