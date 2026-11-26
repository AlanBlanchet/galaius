"""What the CLI says about placing a run on another of this person's PCs: choosing the PC and the project by name, and reading the result."""

import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from uuid import UUID

from galaius_core import AGENT_TOUCH_SCOPES, GitRemote, PcFleet, PcState, PeerProjects

from galaius.account.caller import Caller
from galaius.agents.catalog import relations
from galaius.machine.peer.client import PeerClient, PeerError

#: The role a start on another PC takes when none is named: the coordinator, as the web's « Lancer ».
DEFAULT_ROLE = "main"
#: Refusals that mean the start is on its way (its PC slow to answer, or the project still being put there): asked again with the same key.
ON_ITS_WAY = frozenset({"pc_slow", "in_progress"})
#: How long the caller keeps asking for such a start's run id; inside the key's life (`PeerClient.WINDOW`) so every ask is the same request.
START_PATIENCE = 300.0
#: The most seconds between two such asks.
ASK_AGAIN = 15


def pick(fleet: PcFleet, which: str) -> PcState:
    """The PC named `which` (name or id), or with `auto` the other PC with the most free places, memory then name breaking a tie. ValueError says why not."""
    others = [pc for pc in fleet.pcs if not pc.this]
    if which == "auto":
        ready = [pc for pc in others if pc.dispatchable]
        if not ready:
            raise ValueError(f"no other PC of yours takes a run now: {'; '.join(f'{pc.name}: {pc.why_not or pc.state}' for pc in others) or 'none linked'}")
        return min(ready, key=lambda pc: (-(pc.places_free or 0), -(pc.memory_free_mb or 0), pc.name))
    found = [pc for pc in others if which in (str(pc.id), pc.name)]
    if len(found) != 1:
        raise ValueError(f"{'no' if not found else 'more than one'} other PC named {which!r} (yours: {', '.join(sorted(pc.name for pc in others)) or 'none'})")
    return found[0]


def project_of(projects: PeerProjects, which: str) -> UUID:
    """The project named `which` (name, case aside, or id)."""
    found = [project for project in projects.projects if which.casefold() in (str(project.id), project.name.casefold())]
    if len(found) != 1:
        raise ValueError(f"{'no' if not found else 'more than one'} project named {which!r} among yours ({', '.join(sorted(project.name for project in projects.projects)) or 'none'})")
    return found[0].id


def _slug(name: str) -> str:
    """A project's name and its folder's compared alike: accents, case and punctuation aside."""
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold())


def standing_folders(here: str | None) -> tuple[Path, ...]:
    """The project folders the sender stands in: the one holding `here`, then its conversation's (an agent run's)."""
    run = Caller.current().environment.get("GALAIUS_RUN_ID")
    found = (relations.folder_project(here) if here else None, relations.project(run) if run else None)
    return tuple(dict.fromkeys(folder for folder in found if folder is not None))


def repository_name(folder: Path) -> str | None:
    """The name of the repository `folder`'s `origin` points to (a checkout renamed on disk keeps it, and a project the server made is named after it); None without one."""
    try:
        said = subprocess.run(["git", "-C", str(folder), "remote", "get-url", "origin"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    remote = GitRemote.stripped(said.stdout.strip()) if said.returncode == 0 else None
    return remote.name if remote is not None else None


def project_standing(projects: PeerProjects, folders: tuple[Path, ...]) -> UUID:
    """The project named like the first of `folders` (its name, else its origin repository's) that names exactly one. ValueError lists theirs to pass with --project."""
    for folder in folders:
        for name in (folder.name, repository_name(folder)):
            found = [project for project in projects.projects if name and _slug(name) and _slug(project.name) == _slug(name)]
            if len(found) == 1:
                return found[0].id
    named = " or ".join(repr(folder.name) for folder in folders) or "this folder"
    raise ValueError(f"no project of yours is named like {named}: pass --project <name or id> (yours: {', '.join(sorted(project.name for project in projects.projects)) or 'none'})")


def place(client: PeerClient, machine: str, project: str | None, task: str, *, role: str | None, path: str | None, permission: str | None, provider: str | None, model: str | None,
          here: str | None = None, patience: float = START_PATIENCE) -> str:
    """The run id of an agent started on another PC of yours: `role` (`DEFAULT_ROLE`) in `project` there (None: the project of folder `here`, else of the
    conversation). A start still on its way (`ON_ITS_WAY`: the project being put there, a slow answer) is asked again for `patience` seconds.
    ValueError / PeerError / PeerUnavailable say why not."""
    if provider is not None or model is not None:
        raise ValueError("a run placed on another PC runs on the model its rule picks there: leave out --provider and --model")
    if permission is not None and permission not in AGENT_TOUCH_SCOPES:
        raise ValueError(f"--permission-mode on another PC is one of {', '.join(AGENT_TOUCH_SCOPES)}")
    target = pick(client.fleet(), machine)
    if not target.dispatchable and target.why_not:
        raise ValueError(f"{target.name} takes no run now: {target.why_not}")
    projects = client.projects()
    chosen = project_of(projects, project) if project is not None else project_standing(projects, standing_folders(here))
    until, told = time.monotonic() + patience, False
    while True:
        try:
            started = client.start(target.id, chosen, role or DEFAULT_ROLE, task, path=path or "", permission=permission)
            return f"{started.run_id}  {started.machine_name}  {started.permission}"
        except PeerError as refused:
            if refused.refusal.code not in ON_ITS_WAY or time.monotonic() >= until:
                raise
            if not told:
                print(f"{refused.refusal.message}. Waiting for its run id (the same command later reads it too).", file=sys.stderr)
                told = True
            time.sleep(min(refused.refusal.retry_after_s or ASK_AGAIN, ASK_AGAIN))
