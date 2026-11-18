"""What the CLI says about placing a run on another of this person's PCs: choosing the PC and the project by name, and reading the result."""

from uuid import UUID

from galaius_core import AGENT_TOUCH_SCOPES, PcFleet, PcState, PeerProjects

from galaius.machine.peer.client import PeerClient


def pick(fleet: PcFleet, which: str) -> PcState:
    """The PC named `which` (name or id), or with `auto` the other PC with the most free places, memory then name breaking a tie. ValueError says why not."""
    others = [pc for pc in fleet.pcs if not pc.this]
    if which == "auto":
        ready = [pc for pc in others if pc.dispatchable]
        if not ready:
            raise ValueError(f"no other PC of yours takes a run now ({', '.join(f'{pc.name}: {pc.state}' for pc in others) or 'none linked'})")
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


def place(client: PeerClient, machine: str, project: str, task: str, *, role: str | None, path: str | None, permission: str | None, provider: str | None, model: str | None) -> str:
    """The run id of an agent started on another PC of yours: `role` in `project` there. ValueError / PeerError / PeerUnavailable say why not."""
    if role is None:
        raise ValueError("--machine needs --agent: the role to start there")
    if provider is not None or model is not None:
        raise ValueError("a run placed on another PC runs on the model its rule picks there: leave out --provider and --model")
    if permission is not None and permission not in AGENT_TOUCH_SCOPES:
        raise ValueError(f"--permission-mode on another PC is one of {', '.join(AGENT_TOUCH_SCOPES)}")
    target = pick(client.fleet(), machine)
    started = client.start(target.id, project_of(client.projects(), project), role, task, path=path or "", permission=permission)
    return f"{started.run_id}  {started.machine_name}  {started.permission}"
