"""A subscription's held login: the credential its CLI's held sign-in leaves (`AgentProgram.held`, a `HeldLoginForm`),
signed in once and put on each of the owner's PCs, kept in that subscription's folder (`path`: private to this user,
written in one step, never through a link). A credential read from a variable (Claude Code's token) lives in `HELD`
and is set only in the launches whose environment names that folder for its CLI (`environment`); one read from a file
(Codex's `auth.json`) is that file of the folder, read by the CLI itself."""

import json
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path

from galaius_core import HeldLoginForm

from galaius.host.paths import UserPaths
from galaius.host.private_files import PRIVATE_FILES

logger = logging.getLogger(__name__)

#: A variable-read credential's file inside a subscription's folder.
HELD = "held-token"
#: A variable-read credential: one printable word, never a second line or variable smuggled in.
WORD = re.compile(r"[\x21-\x7e]{16,4096}")


def path(folder: Path, form: HeldLoginForm) -> Path:
    """Where `form`'s credential lives in `folder`: its CLI's own file, else `HELD`."""
    return folder / (form.file or HELD)


def fits(form: HeldLoginForm, credential: str) -> bool:
    """Whether `credential` is what `form`'s CLI reads: one word (`WORD`) from a variable, a JSON object from a file."""
    if form.variable is not None:
        return WORD.fullmatch(credential) is not None
    try:
        return isinstance(json.loads(credential), dict)
    except ValueError:
        return False


def credential_in(folder: Path, form: HeldLoginForm) -> str | None:
    """The credential `folder` holds; None: none, or a file that is a link or not private to this user (never read
    through, said once in the log by its folder, never its value)."""
    where = path(folder, form)
    if not os.path.lexists(where):
        return None
    try:
        return PRIVATE_FILES.read_secret(where) if form.variable is not None else PRIVATE_FILES.read_text(where)
    except (OSError, ValueError):
        logger.warning("subscription folder %s: its held login is a link or not private; not used", folder.name)
        return None


def hold(folder: Path, form: HeldLoginForm, credential: str) -> bool:
    """`credential` kept in `folder`; whether it changed (the same one again writes nothing). Refused (PermissionError)
    when the file there is a link: whoever made it could point the write anywhere."""
    where = path(folder, form)
    if where.is_symlink():
        raise PermissionError(f"subscription folder {folder.name}: its held login file is a link; remove the subscription and connect it again")
    if credential_in(folder, form) == credential:
        return False
    if form.variable is not None:
        PRIVATE_FILES.write_secret(where, credential)
    else:  # its CLI reads it as written: never sealed
        PRIVATE_FILES.write_text(where, credential)
    return True


def forget(folder: Path, form: HeldLoginForm) -> None:
    """`folder`'s held credential removed (one that failed its check is never handed on)."""
    path(folder, form).unlink(missing_ok=True)


def mark(variable: str) -> str:
    """The variable naming the folder whose held login galaius set in `variable` (`environment`): a token the person set
    himself carries none. Never secret-shaped (`fence.SECRET_SUFFIXES`): the fence would strip it and keep the token."""
    return f"{variable}_HELD_FOLDER"


def environment(environment: Mapping[str, str], logins: Mapping[str, HeldLoginForm]) -> dict[str, str]:
    """`environment` with each variable-read credential of `logins` (keyed by its CLI's login-folder variable) set from
    the subscription folder that variable names when it holds one (marked, `mark`). Removed otherwise when galaius set it
    (marked: inherited from another launch) or when the launch names any subscription folder (a token outranks that
    folder's own login). The person's own token (unmarked) stays only on launches naming no subscription folder: a child
    of a held run launched on his own login runs with neither."""
    root = UserPaths.subscriptions().resolve()
    found = dict(environment)
    for home, form in logins.items():
        assert form.variable is not None
        named = environment.get(home)
        folder = Path(named).resolve() if named else None
        if found.pop(mark(form.variable), None) is not None or (folder is not None and folder.parent == root):
            found.pop(form.variable, None)
        if folder is not None and folder.parent == root and (token := credential_in(folder, form)) is not None:
            found[form.variable], found[mark(form.variable)] = token, str(folder)
    return found
