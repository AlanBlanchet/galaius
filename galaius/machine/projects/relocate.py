"""Projects an earlier Galaius put on this PC, moved to its projects folder (`UserPaths.projects`, `~/galaius/projects`)
with every record of this PC naming them (owner 2026-11-11 « A normal user wouldn't have the dev project »).

Earlier homes: the hidden `~/.galaius/projects` (wire root `PROJECTS_ROOT`, the token naming the new folder too) and the
agent folder `EARLIER_AGENT_ROOT` an earlier server opened in the runner's working directory. Each project folder there
moves whole (`os.rename`) unless a process works in it: that one stays, named, and moves at a later run. Each moved
folder takes along: the agent registry's runs started in it (`cwd`, fence start / project / levels), each Claude Code
login's session folder keyed by its path (`ClaudeCodeProvider.project_folder`, so a resume finds the conversation), and
this PC's web runs and workspace jobs (`rehome`). The earlier agent folder leaves the agent roots once emptied (`drop`).

`plan` writes nothing (`galaius machine projects-move` prints it); `apply` carries it out. The runner applies the
hidden folder's part at connect (`hidden_only`): Galaius's own folder, no person's."""

import os
import sys
from collections.abc import Callable
from pathlib import Path

from galaius_core import PROJECTS_ROOT
from pydantic import BaseModel, ConfigDict

from galaius.agents import registry as reg
from galaius.agents.limits.subscriptions import Subscription
from galaius.agents.providers import PROVIDERS, ClaudeCodeProvider
from galaius.host.paths import UserPaths

#: The agent folder an earlier server opened in every PC's working directory for its projects.
EARLIER_AGENT_ROOT = "galaius/projets"


class Move(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: Path
    target: Path
    #: The agent root runs and jobs named it by before.
    root: str


class Kept(BaseModel):
    model_config = ConfigDict(frozen=True)

    folder: Path
    why: str


class SessionMove(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: Path
    target: Path


class MovePlan(BaseModel):
    """What `apply` does, every path named: the dry run."""

    model_config = ConfigDict(frozen=True)

    moves: tuple[Move, ...] = ()
    kept: tuple[Kept, ...] = ()
    runs: tuple[str, ...] = ()
    sessions: tuple[SessionMove, ...] = ()
    #: The earlier agent roots `apply` empties: dropped from this PC's settings once it did.
    drops: tuple[str, ...] = ()

    def lines(self) -> list[str]:
        return [*(f"move {move.source} -> {move.target}" for move in self.moves), *(f"keep {kept.folder}: {kept.why}" for kept in self.kept),
                f"{len(self.runs)} conversation record(s) follow their folder", *(f"session {item.source} -> {item.target.name}" for item in self.sessions),
                *(f"agent folder {root} removed from this computer's settings" for root in self.drops)]


def inside(path: str | Path, folder: Path) -> bool:
    text, prefix = str(path), str(folder)
    return text == prefix or text.startswith(prefix + os.sep)


def _homes(working_directory: Path, agent_roots: tuple[str, ...], hidden_only: bool) -> tuple[tuple[Path, str], ...]:
    hidden = (Path.home() / ".galaius" / "projects", PROJECTS_ROOT)
    if hidden_only or EARLIER_AGENT_ROOT not in agent_roots:
        return (hidden,)
    return (hidden, (working_directory.resolve().joinpath(*EARLIER_AGENT_ROOT.split("/")), EARLIER_AGENT_ROOT))


def _worked_in() -> tuple[str, ...]:
    """This user's processes' working folders (Linux `/proc`; elsewhere the registry's live runs alone say it)."""
    if not sys.platform.startswith("linux"):
        return ()
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                found.append(os.readlink(entry / "cwd"))
            except OSError:
                pass
    return tuple(found)


def _claude_homes(runs: list[reg.AgentRun], moved: set[str]) -> tuple[Path, ...]:
    """Every Claude Code login folder of this PC, and the fence state folder of each moved run."""
    logins = Subscription.session_homes(PROVIDERS["claude"], os.environ)
    fenced = (Path(run.fence.state) / "claude" for run in runs if run.run_id in moved and run.fence is not None and run.fence.state is not None)
    return tuple(home for home in dict.fromkeys((*logins, *fenced)) if (home / "projects").is_dir())


def plan(working_directory: Path, agent_roots: tuple[str, ...], hidden_only: bool = False) -> MovePlan:
    runs, cwds, new = reg.list_runs(include_foreign=True), _worked_in(), UserPaths.projects()
    moves, kept = [], []
    homes = _homes(working_directory, agent_roots, hidden_only)
    for home, root in homes:
        try:
            folders = sorted(entry for entry in home.iterdir() if entry.is_dir() and not entry.is_symlink())
        except (FileNotFoundError, NotADirectoryError):
            continue
        for folder in folders:
            live = [run.run_id[:8] for run in runs if run.cwd and inside(run.cwd, folder) and run.process_running()]
            if live or any(inside(cwd, folder) for cwd in cwds):
                kept.append(Kept(folder=folder, why="in use now" + (f" by {', '.join(live[:4])}" if live else "")))
            elif (new / folder.name).exists() or (new / folder.name).is_symlink():
                kept.append(Kept(folder=folder, why=f"{new / folder.name} exists"))
            else:
                moves.append(Move(source=folder, target=new / folder.name, root=root))
    moved = {run.run_id for run in runs if run.cwd and any(inside(run.cwd, move.source) for move in moves)}
    sessions = []
    for home in _claude_homes(runs, moved):
        keys = [(move, ClaudeCodeProvider.project_folder(home, move.source).name) for move in moves]
        for entry in sorted((home / "projects").iterdir()):
            match = max(((move, key) for move, key in keys if entry.name == key or entry.name.startswith(key + "-")), key=lambda pair: len(pair[1]), default=None)
            if match is not None:
                move, key = match
                sessions.append(SessionMove(source=entry, target=entry.with_name(ClaudeCodeProvider.project_folder(home, move.target).name + entry.name[len(key):])))
    drops = tuple(root for home, root in homes if root != PROJECTS_ROOT and not any(item.folder.parent == home for item in kept))
    return MovePlan(moves=tuple(moves), kept=tuple(kept), runs=tuple(sorted(moved)), sessions=tuple(sessions), drops=drops)


def _moved(path: Path, moves: tuple[Move, ...]) -> Path:
    move = next((move for move in moves if inside(path, move.source)), None)
    return path if move is None else move.target / path.relative_to(move.source)


def _fence(run: reg.AgentRun, moves: tuple[Move, ...]):
    fence = run.fence
    if fence is None:
        return None
    base = fence.working_directory
    levels = {}
    for name, level in fence.levels.items():
        after = _moved(base.joinpath(*name.split("/")), moves)
        levels[after.relative_to(base).as_posix() if after.is_relative_to(base) else name] = level
    return fence.model_copy(update={"start": _moved(fence.start, moves), "levels": levels, "project": None if fence.project is None else _moved(fence.project, moves)})


def _merge(source: Path, target: Path) -> list[str]:
    """`source`'s files into the existing `target`, none replaced: each one left behind is named."""
    left = []
    for item in sorted(source.rglob("*"), key=lambda path: len(path.parts)):
        destination = target / item.relative_to(source)
        if item.is_dir():
            destination.mkdir(exist_ok=True)
        elif destination.exists():
            left.append(f"{item}: {destination} exists, left in place")
        else:
            os.rename(item, destination)
    return left


def apply(done: MovePlan, rehome: Callable[[str, frozenset[str]], None], drop: Callable[[tuple[str, ...]], None]) -> list[str]:
    """`done` carried out: folders, then the records naming them, then their sessions; `rehome(root, names)` re-roots this
    PC's web runs and jobs, `drop(roots)` removes the emptied earlier agent roots. Returns what was left undone, each named."""
    failed, moves = [], []
    UserPaths.projects().mkdir(parents=True, exist_ok=True)
    for move in done.moves:
        try:
            os.rename(move.source, move.target)
            moves.append(move)
        except OSError as error:
            failed.append(f"{move.source}: {error}")
    landed = tuple(moves)
    for run_id in done.runs:
        run = reg.get_run(run_id)
        if run is not None and run.cwd and any(inside(run.cwd, move.source) for move in landed):
            reg.save_run(run.model_copy(update={"cwd": str(_moved(Path(run.cwd), landed)), "fence": _fence(run, landed)}))
    sources = {str(move.source) for move in landed}
    for session in done.sessions:
        if not any(session.source.name.startswith(ClaudeCodeProvider.project_folder(session.source.parent.parent, Path(source)).name) for source in sources):
            continue
        if session.target.exists():
            failed += _merge(session.source, session.target)
        else:
            os.rename(session.source, session.target)
    for root in sorted({move.root for move in landed} - {PROJECTS_ROOT}):
        rehome(root, frozenset(move.source.name for move in landed if move.root == root))
    if done.drops and not failed:
        drop(done.drops)
    return failed
