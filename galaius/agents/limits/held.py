"""A subscription's held login: its CLI's long-lived token (`AgentProvider.token_arguments`), signed in once and put on
each of the owner's PCs, kept in that subscription's folder (`HELD`: private to this user, written in one step, never
through a link). Every launch whose environment names that folder for its CLI runs with the token; every other launch
(another subscription, the PC's own login, a lent or closed folder, whatever it inherited) runs without it
(`environment`)."""

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from galaius.host.paths import UserPaths
from galaius.host.private_files import PRIVATE_FILES

logger = logging.getLogger(__name__)

#: The token's file inside a subscription's folder.
HELD = "held-token"


def token_in(folder: Path) -> str | None:
    """The token `folder` holds; None: none, or a file that is a link or not private to this user (never read through,
    said once in the log by its folder, never its value)."""
    path = folder / HELD
    if not os.path.lexists(path):
        return None
    try:
        return PRIVATE_FILES.read_secret(path)
    except ValueError:
        logger.warning("subscription folder %s: its held login is a link or not private; not used", folder.name)
        return None


def hold(folder: Path, token: str) -> bool:
    """`token` kept in `folder`; whether it changed (the same token again writes nothing). Refused (PermissionError)
    when the file there is a link: whoever made it could point the write anywhere."""
    path = folder / HELD
    if path.is_symlink():
        raise PermissionError(f"subscription folder {folder.name}: its held login file is a link; remove the subscription and connect it again")
    if token_in(folder) == token:
        return False
    PRIVATE_FILES.write_secret(path, token)
    return True


def mark(variable: str) -> str:
    """The variable naming the folder whose held login galaius set in `variable` (`environment`): a token the person set
    himself carries none, and is never removed."""
    return f"GALAIUS_HELD_{variable}"


def environment(environment: Mapping[str, str], logins: Mapping[str, str]) -> dict[str, str]:
    """`environment` with each token variable of `logins` (keyed by its CLI's login-folder variable) set from the
    subscription folder that variable names when it holds a token (marked, `mark`); a held token it inherited from
    another launch (marked) removed; the person's own token (unmarked) kept as it is."""
    root = UserPaths.subscriptions().resolve()
    found = dict(environment)
    for home, variable in logins.items():
        if found.pop(mark(variable), None) is not None:
            found.pop(variable, None)
        named = environment.get(home)
        if named and (folder := Path(named).resolve()).parent == root and (token := token_in(folder)) is not None:
            found[variable], found[mark(variable)] = token, str(folder)
    return found
