"""Projects an earlier Galaius put on this PC, moved to its projects folder (`UserPaths.projects`, `~/.galaius/projects`,
inside galaius's hidden folder on every OS; owner 2026-11-17 « Projects should be created in there ») with every record of
this PC naming them.

Earlier homes: the visible `~/galaius/projects` (`UserPaths.earlier_projects`, wire root `PROJECTS_ROOT`, the token naming
the new folder too) and the agent folder `EARLIER_AGENT_ROOT` an earlier server opened in the runner's working directory.
Each project folder there moves whole (`os.rename`, one entry at a time: never the folder holding them, which a POSIX
rename would swap for an empty target and which would carry a link, a file or a project in use along) unless a process
works in it: that one stays, named, and moves at a later run. Never moved: a hidden name (the `.<id>.partial` of a clone in
progress), anything while a project is being put in place, a link, a name already taken in the target, a folder whose way
(home to it, on either side) holds a link or a file. Each moved
folder takes along: the agent registry's runs started in it (`cwd`, fence start / project / levels), each Claude Code
login's session folder keyed by its path (`ClaudeCodeProvider.project_folder`, so a resume finds the conversation), and
this PC's web runs and workspace jobs (`rehome`). The earlier agent folder leaves the agent roots once emptied (`drop`).

`plan` writes nothing (`galaius machine projects-move` prints it); `apply` carries it out. The runner applies it at
connect: both homes are folders Galaius made, and a folder in use waits for a later connect."""

import contextlib
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from galaius_core import PROJECTS_ROOT
from pydantic import BaseModel, ConfigDict

from galaius.agents import registry as reg
from galaius.agents.limits.subscriptions import Subscription
from galaius.agents.providers import PROVIDERS, ClaudeCodeProvider
from galaius.host.paths import UserPaths
from galaius.host.pinned_directory import PinnedDirectory

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


def _homes(working_directory: Path, agent_roots: tuple[str, ...]) -> tuple[tuple[Path, str], ...]:
    """The earlier folders holding projects, each with the wire root its runs and jobs named it by: never the projects
    folder itself (a source equal to the target is no source)."""
    earlier = [(UserPaths.earlier_projects(), PROJECTS_ROOT)]
    if EARLIER_AGENT_ROOT in agent_roots:
        earlier.append((working_directory.resolve().joinpath(*EARLIER_AGENT_ROOT.split("/")), EARLIER_AGENT_ROOT))
    target = UserPaths.projects()
    return tuple((home, root) for home, root in earlier if home != target and not (home.exists() and target.exists() and home.samefile(target)))


def unplain(folder: Path) -> str | None:
    """Why `folder` cannot be moved from or into: a link, a junction or a file sits on the way from the home folder to it
    (an absent folder and the parents above the home folder are not judged), else None."""
    home = Path.home().resolve()
    for part in (*reversed(folder.parents), folder):
        if part != home and home not in part.parents:
            continue
        try:
            facts = part.lstat()
        except FileNotFoundError:
            return None   # not made yet: nothing below it exists
        if not stat.S_ISDIR(facts.st_mode) or PinnedDirectory.link_like(facts):
            return f"{part} is a link or a file"
    return None


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


def plan(working_directory: Path, agent_roots: tuple[str, ...], busy: bool = False) -> MovePlan:
    """`busy`: a project is being put in place or sent now (`MachineWorkspaces.busy`): nothing moves."""
    runs, cwds, new = reg.list_runs(include_foreign=True), _worked_in(), UserPaths.projects()
    moves, kept, every = [], [], []
    homes = _homes(working_directory, agent_roots)
    blocked = unplain(new)
    for home, root in homes:
        if (way := unplain(home) or blocked) is not None:
            kept.append(Kept(folder=home, why=f"not moved: {way}"))
            continue
        try:
            entries = sorted(home.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            continue
        every += entries
        for folder in entries:
            if folder.name.startswith("."):
                kept.append(Kept(folder=folder, why="hidden: a clone in progress, or none of galaius's"))
            elif busy:
                kept.append(Kept(folder=folder, why="a project is being put in place or sent now"))
            elif folder.is_symlink() or not folder.is_dir():
                kept.append(Kept(folder=folder, why="a link or a file: left as it is"))
            else:
                live = [run.run_id[:8] for run in runs if run.cwd and inside(run.cwd, folder) and run.process_running()]
                if live or any(inside(cwd, folder) for cwd in cwds):
                    kept.append(Kept(folder=folder, why="in use now" + (f" by {', '.join(live[:4])}" if live else "")))
                elif (new / folder.name).exists() or (new / folder.name).is_symlink():
                    kept.append(Kept(folder=folder, why=f"{new / folder.name} exists"))
                elif any(move.target == new / folder.name for move in moves):
                    kept.append(Kept(folder=folder, why=f"another earlier folder of that name moves to {new / folder.name} first"))
                else:
                    moves.append(Move(source=folder, target=new / folder.name, root=root))
    moved = {run.run_id for run in runs if run.cwd and any(inside(run.cwd, move.source) for move in moves)}
    sessions = []
    by_source = {move.source: move for move in moves}
    for home in _claude_homes(runs, moved):
        # Keyed by the folder's path with every other character a dash: `horizon` is a prefix of `horizon-mods`, so the
        # longest key among EVERY earlier folder (moving or kept) decides whose session folder it is.
        keys = [(folder, ClaudeCodeProvider.project_folder(home, folder).name) for folder in every]
        for entry in sorted((home / "projects").iterdir()):
            if not _real_folder(entry):   # planted by a run that writes its own session folder: never followed
                kept.append(Kept(folder=entry, why="a link or a file among the sessions: left as it is"))
                continue
            match = max(((folder, key) for folder, key in keys if entry.name == key or entry.name.startswith(key + "-")), key=lambda pair: len(pair[1]), default=None)
            if match is not None and match[0] in by_source:
                move, key = by_source[match[0]], match[1]
                sessions.append(SessionMove(source=entry, target=entry.with_name(ClaudeCodeProvider.project_folder(home, move.target).name + entry.name[len(key):])))
    drops = tuple(root for home, root in homes if root != PROJECTS_ROOT and not any(item.folder == home or item.folder.parent == home for item in kept))   # emptied by `apply`
    return MovePlan(moves=tuple(moves), kept=tuple(kept), runs=tuple(sorted(moved)), sessions=tuple(sessions), drops=drops)


def _moved(path: Path, moves: tuple[Move, ...]) -> Path:
    move = next((move for move in moves if inside(path, move.source)), None)
    return path if move is None else move.target / path.relative_to(move.source)


def _fence(run: reg.AgentRun, moves: tuple[Move, ...]):
    """The run's fence for its folder's new place. A folder now in the projects folder is the run's PROJECT (written, no
    level; `FenceSpec.build` checks it again each turn): a run from an earlier agent folder kept a level on it, which a
    hidden path (`.galaius/projects/x`) never takes, so the conversation could not resume."""
    fence = run.fence
    if fence is None:
        return None
    base, start = fence.working_directory, _moved(fence.start, moves)
    levels = {}
    for name, level in fence.levels.items():
        after = _moved(base.joinpath(*name.split("/")), moves)
        if not any(inside(after, move.target) for move in moves):
            levels[after.relative_to(base).as_posix() if after.is_relative_to(base) else name] = level
    moved = next((move for move in moves if inside(start, move.target)), None)
    project = _moved(fence.project, moves) if fence.project is not None else None if moved is None else moved.target
    return fence.model_copy(update={"start": start, "levels": levels, "project": project})


def _real_folder(path: Path) -> bool:
    """Whether `path` is a plain folder itself: a link (dangling too) or a file is not."""
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _merge(source: Path, target: Path) -> list[str]:
    """`source`'s files into the existing `target`, none replaced: each one left behind is named. Session folders are written
    by agents (the fence lets a run write its own): nothing is followed, a link in either tree is left where it is, never
    moved or written through (threat review 2026-11-20 N1: a planted link moved `~/.ssh/id_ed25519` into an agent's folder,
    another put a file in `~/.config/autostart`)."""
    left = []
    if not _real_folder(source) or not _real_folder(target):
        return [f"{source}: it or {target} is a link or a file, left in place"]
    for folder, names, files in os.walk(source, followlinks=False):
        here = Path(folder)
        destination = target / here.relative_to(source)
        for name in list(names):
            if (here / name).is_symlink() or not (_real_folder(destination / name) or not os.path.lexists(destination / name)):
                left.append(f"{here / name}: a link, or {destination / name} is not a plain folder: left in place")
                names.remove(name)
            elif not os.path.lexists(destination / name):
                (destination / name).mkdir()
        for name in files:
            item = here / name
            if item.is_symlink() or os.path.lexists(destination / name):
                left.append(f"{item}: a link, or {destination / name} exists, left in place")
            else:
                os.rename(item, destination / name)
    return left


def _session(item: SessionMove) -> list[str]:
    """One session folder to its new key, never through a link; each error named, none raised (the rest of `apply` still runs)."""
    try:
        if not _real_folder(item.source):
            return [f"{item.source}: a link or a file, left in place"]
        if os.path.lexists(item.target):
            return _merge(item.source, item.target)
        os.rename(item.source, item.target)
        return []
    except OSError as error:
        return [f"{item.source}: {error}"]


def apply(done: MovePlan, rehome: Callable[[str, frozenset[str]], None], drop: Callable[[tuple[str, ...]], None]) -> list[str]:
    """`done` carried out: folders, then the records naming them, then their sessions; `rehome(root, names)` re-roots this
    PC's web runs and jobs, `drop(roots)` removes the emptied earlier agent roots. Returns what was left undone, each named."""
    failed, moves = [], []
    if done.moves:
        # Made folder by folder: `mkdir(parents=True)` would follow a link planted on the way (checked again here, `plan` ran before).
        if (way := unplain(UserPaths.projects())) is not None:
            return [f"{UserPaths.projects()}: {way}"]
        UserPaths.projects().mkdir(parents=True, exist_ok=True)
    for move in done.moves:
        try:
            if move.target.exists() or move.target.is_symlink() or move.source.is_symlink() or unplain(move.source.parent) is not None:   # a POSIX rename swaps an empty folder for the source
                raise FileExistsError(f"{move.target} exists, or a link is on the way or is the folder itself")
            os.rename(move.source, move.target)
            moves.append(move)
        except OSError as error:
            failed.append(f"{move.source}: {error}")
    landed = tuple(moves)
    for emptied in {move.source.parent for move in landed}:   # the folder an earlier Galaius made, left empty: never anything in it removed
        with contextlib.suppress(OSError):
            emptied.rmdir()
    for run_id in done.runs:
        run = reg.get_run(run_id)
        if run is not None and run.cwd and any(inside(run.cwd, move.source) for move in landed):
            reg.save_run(run.model_copy(update={"cwd": str(_moved(Path(run.cwd), landed)), "fence": _fence(run, landed)}))
    sources = {str(move.source) for move in landed}
    for session in done.sessions:
        if not any(session.source.name.startswith(ClaudeCodeProvider.project_folder(session.source.parent.parent, Path(source)).name) for source in sources):
            continue
        failed += _session(session)
    for root in sorted({move.root for move in landed} - {PROJECTS_ROOT}):
        rehome(root, frozenset(move.source.name for move in landed if move.root == root))
    if done.drops and not failed:
        drop(done.drops)
    return failed
