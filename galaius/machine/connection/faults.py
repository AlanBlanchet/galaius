"""Refusals and faults of the machine link, worded for the person and for the server."""

import subprocess
from typing import NamedTuple
from pydantic import ValidationError
from galaius_core import refusal_of, MachineRefusal, OwnFault, OwnFaultDoing, OwnFaultKind
from galaius_core.wire import internals
import logging

logger = logging.getLogger(__package__)


#: A fault of this computer's own code is told as `OwnFault` (what it was doing, its kind): its own text names fields,
#: classes and files nobody reading a screen wrote, so it stays in this PC's log.
#: The exceptions raised here that are already WRITTEN FOR A READER: their own sentence travels to the web as it is.
#: Anything else is this PC's code failing, whatever its text reads like — a `KeyError`'s « 'token' » reached the web
#: as though the PC had refused in those words, once a catch-all started answering every request (2026-10-23).
WORDED: tuple[type[BaseException], ...] = (PermissionError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired)


def fault_of(error: BaseException) -> OwnFaultKind | None:
    """The kind of fault `error` is when it is of this PC's own making — a contract `ValidationError`, a text written for a
    program, an exception nothing here words for a reader (`WORDED`) or one with no words at all; None for a refusal
    written for its reader."""
    words = str(error).strip()
    if isinstance(error, ValidationError):
        return "contract"
    if not isinstance(error, WORDED) or internals(words) or not (words or isinstance(error, OSError)):
        return "code"
    return None


class Said(NamedTuple):
    """What this PC answers the web for an error: its English sentence (`said`) and, typed, why (`refusal`), the one
    the server words in its reader's language."""

    said: str
    refusal: MachineRefusal | None


def said_of(error: BaseException, about: str = "", *, doing: OwnFaultDoing = "answering_web") -> Said:
    """What this PC answers the web when it could not do what was asked, `doing` naming what it was at.

    A typed refusal (`Coded`) travels with its type, whatever its sentence names (a sentence that is a text written
    for a program stays in the log). Any other refusal's own sentence
    (every `PermissionError`, `ValueError` and `RuntimeError` raised here is written for a reader) travels as it is;
    an OS fault says what it could not do `about` the path it touched; a program that ran too long says so without
    its argv. A fault of this PC's own making travels as its `OwnFault`: a contract `ValidationError`, a text written
    for a program, an exception nothing here words for a reader (`WORDED`), or no words at all. Its own text is logged
    here and never relayed (2026-10-15: a 464-character fence reason reached the web as « 1 validation error for
    MachineFence … » and the Data page showed it).
    """
    words = str(error).strip()
    if (typed := refusal_of(error)) is not None:
        if words and not internals(words):
            return Said(words, typed)
        logger.warning("%s", words or type(error).__name__, exc_info=error)  # a text written for a program, kept to the log
        return Said(OwnFault(doing=doing, fault="code").said, typed)
    if isinstance(error, subprocess.TimeoutExpired):
        return Said(f"a program this computer ran took longer than {error.timeout:.0f}s", None)
    if isinstance(error, OSError) and not isinstance(error, PermissionError) and (said := ": ".join(part for part in (about, error.strerror or words) if part)):
        return Said(said, None)
    if (fault := fault_of(error)) is None and words:
        return Said(words, None)
    own = OwnFault(doing=doing, fault=fault or "code")
    logger.warning(own.said, exc_info=error)
    return Said(own.said, own)


class EnrollmentChanged(PermissionError):
    """The machine file on disk names another enrollment (machine, workspace, server or token)
    than the connection holds: nothing runs under the old one, the runner reconnects as the new."""


class Paused(Exception):
    """The owner paused this computer from its tray icon: the connection closes and stays away until resumed."""


class Woke(Exception):
    """This computer slept (`MachineRunner.slept`): its socket is gone, connect again at once."""


class TokenSwapped(Exception):
    """The server swapped this machine's bootstrap token and the new one is saved: the runner starts
    again on it, every task with it (none keeps the spent token)."""


#: The refusals an agent request from the web is ANSWERED with (`MachineRunner._answer_agent_request`): each
#: says why it could not be performed, and the asker gets those words. Anything outside this tuple is answered
#: all the same — as a failure, with its words — because a request left unanswered reads at the server as a PC
#: that never confirmed, which is neither true nor actionable.
ANSWERED_REFUSALS: tuple[type[BaseException], ...] = (PermissionError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired)


def own_fault(error: BaseException, said: Said) -> bool:
    """Whether an answered refusal is this computer failing rather than refusing: an OS error no typed refusal names
    (2026-11-09: every agent start on a Windows PC answered « Nom de fichier ou extension trop long », WinError 206, as if
    the PC had refused; one warning line, no traceback, no report). A `PermissionError` is a refusal written here."""
    return isinstance(error, OSError) and not isinstance(error, PermissionError) and said.refusal is None
