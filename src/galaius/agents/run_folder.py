"""A run's folder put back before one of its turns resumes. Claude Code and Codex keep a session
under the folder it ran in, so a run whose folder was removed (a slice's git worktree removed once
merged) resumes only once that same path exists again: as the worktree it was, on its branch when
the branch still exists, else on its repository's current line; else as the files its enclosing
repository holds there; else empty. What was done is one French line of the run's conversation
(`FolderRestored.sentence`); a folder that cannot be put back fails the turn with why and what to
do (`FolderUnrecoverable`), never a bare errno."""

from collections.abc import Mapping
import errno
import json
import os
from pathlib import Path
import subprocess
from typing import Literal

from pydantic import BaseModel, ConfigDict

from galaius.agents import registry as reg
from galaius.agents.providers import PROJECT_SETTINGS_OFF, UNTRUSTED_FOLDERS, AgentProvider

#: How long one git command may take while a turn waits on it.
GIT_SECONDS = 60.0
#: How much of the vendor's saved conversation is read for the branch the run worked on (its end).
TRANSCRIPT_TAIL = 4 << 20
#: How the line saying a folder was put back, and the refusal saying it could not be, open: the web reads both by them.
RESTORED_PREFIX = "Dossier recréé : "
MISSING_PREFIX = "Dossier introuvable : "
#: Never a repository's own code while a folder is put back: its hooks and file monitor stay off.
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")

RestoredAs = Literal["worktree_branch", "worktree_detached", "repo_files", "empty"]
#: An OS refusal in the conversation's words: its `strerror` is the PC's language, often English (visual-critic 2026-10-12 r1:
#: « Galaius n’a pas pu le recréer (Permission denied) »). A refusal of a write right is said with the folder it concerns.
OS_REFUSALS: Mapping[int, str] = {code: words for name, words in (
    ("ENOSPC", "le disque est plein"),
    ("EDQUOT", "votre quota de disque est atteint"),
    ("EROFS", "ce disque est en lecture seule"),
    ("ENAMETOOLONG", "le chemin est trop long"),
    ("ENOTDIR", "un fichier occupe le chemin d’un de ses dossiers parents"),
) if (code := getattr(errno, name, None)) is not None}  # an OS without one never raises it
DENIED = (errno.EACCES, errno.EPERM)


def shown(path: Path) -> str:
    """`path` as the conversation names a PC's folder: its home folder as `~` (the web says every folder so)."""
    try:
        inside = path.relative_to(Path.home())
    except (ValueError, RuntimeError):
        return str(path)
    return "~" if inside == Path() else f"~/{inside.as_posix()}"


class FolderUnrecoverable(RuntimeError):
    """The run's folder is missing and could not be put back: the sentence says why and what to do."""

    def __init__(self, path: Path, why: str, action: str = "Recréez ce dossier à cet emplacement, puis appuyez de nouveau sur « Continuer ».") -> None:
        super().__init__(f"{MISSING_PREFIX}{shown(path)}. {why} {action}")


class FolderRestored(BaseModel):
    """A missing run folder put back at its path, and how. `top`: the folder actually rebuilt, the
    run's own or the removed folder it lay in."""

    model_config = ConfigDict(frozen=True)
    path: Path
    top: Path
    restored_as: RestoredAs
    #: The repository it came from (its main checkout); None for an empty folder.
    repository: Path | None = None
    #: The branch checked out, or the line whose commit a detached worktree holds.
    branch: str = ""
    commit: str = ""
    #: The branch the run worked on, gone from its repository (a detached worktree replaces it).
    missing_branch: str = ""

    def sentence(self) -> str:
        repository = self.repository.name if self.repository is not None else ""
        how = {
            "worktree_branch": f"worktree de {repository} sur la branche {self.branch}",
            "worktree_detached": f"worktree de {repository} au commit {self.commit[:8]} de {self.branch or 'HEAD'}"
                                 + (f", la branche {self.missing_branch} n’existe plus" if self.missing_branch else ""),
            "repo_files": f"fichiers suivis de {repository}",
            "empty": "dossier vide : ce n’était le dossier d’aucun dépôt git connu",
        }[self.restored_as]
        inside = f", dans {shown(self.top)}" if self.top != self.path else ""
        return f"{RESTORED_PREFIX}{shown(self.path)} ({how}{inside})."


def _git(folder: Path, *arguments: str) -> str:
    """git's trimmed output in `folder`, whatever `GIT_*` this process inherited (a `GIT_DIR` would
    point it at another repository); raises `subprocess.CalledProcessError` / `OSError` on failure."""
    return subprocess.run(
        ("git", *GIT_SAFE, "-C", str(folder), *arguments), check=True, capture_output=True, text=True, timeout=GIT_SECONDS,
        env={**{name: value for name, value in os.environ.items() if not name.startswith("GIT_")}, "GIT_TERMINAL_PROMPT": "0"},
    ).stdout.strip()


def _quiet_git(folder: Path, *arguments: str) -> str:
    try:
        return _git(folder, *arguments)
    except (OSError, subprocess.SubprocessError):
        return ""


def _main_checkout(folder: Path) -> Path | None:
    """The main checkout of the repository `folder` is a checkout of (`git worktree list`'s first)."""
    first = _quiet_git(folder, "worktree", "list", "--porcelain").partition("\n")[0]
    return Path(first.removeprefix("worktree ")) if first.startswith("worktree ") else None


def _enclosing(existing: Path) -> Path | None:
    top = _quiet_git(existing, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def _within(path: Path, folders: tuple[Path, ...]) -> bool:
    resolved = Path(os.path.realpath(path))
    return any(resolved == folder or folder in resolved.parents for folder in folders)


def _repositories(top: Path, existing: Path, untrusted: tuple[Path, ...]) -> list[Path]:
    """The repositories the removed folder `top` may have been a worktree of: those its sibling
    folders check out (a folder of slice worktrees), the one enclosing them and that one's submodules.
    Never one holding `top` in its own checkout (an ignored build folder is no worktree), never one
    put here from the web (`untrusted`: no git runs in it)."""
    found: list[Path] = []
    enclosing = _enclosing(existing)
    nested = [enclosing / line.split(" ", 1)[1] for line in _quiet_git(enclosing, "config", "-f", ".gitmodules", "--get-regexp", r"\.path$").splitlines()
              if " " in line] if enclosing is not None else []
    siblings = sorted(child for child in existing.iterdir() if child != top and (child / ".git").exists())
    for folder in (*siblings, *((enclosing,) if enclosing is not None else ()), *nested):
        if _within(folder, untrusted) or (main := _main_checkout(folder)) is None or main in found:
            continue
        if main.is_dir() and not _within(main, untrusted) and not _within(top, (Path(os.path.realpath(main)),)):
            found.append(main)
    return found


def _worked_branch(provider: AgentProvider, environment: Mapping[str, str], path: Path, session: str) -> str:
    """The branch the vendor's saved conversation last says the run worked on; "" when none says (or it was detached)."""
    try:
        files = provider.session_files(provider.auth_home(environment), path, session)
    except (OSError, ValueError, NotImplementedError):
        return ""
    for file in files[:1]:
        try:
            with file.open("rb") as handle:
                handle.seek(max(0, file.stat().st_size - TRANSCRIPT_TAIL))
                lines = handle.read().decode(errors="replace").splitlines()
        except OSError:
            return ""
        for line in reversed(lines):
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if isinstance(raw, dict) and (branch := provider.transcript_branch(raw)):
                return "" if branch == "HEAD" else branch
    return ""


def _named_after(repositories: list[Path], top: Path) -> Path | None:
    """The repository whose folder name opens `top`'s (`app` for `app-fix-login`), longest first."""
    matches = [repository for repository in repositories
               if top.name == repository.name or top.name.startswith((f"{repository.name}-", f"{repository.name}_"))]
    return max(matches, key=lambda repository: len(repository.name), default=None)


def _has_branch(repository: Path, branch: str) -> bool:
    return bool(branch) and bool(_quiet_git(repository, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"))


def _checked_out(repository: Path, branch: str) -> bool:
    """Whether a worktree of `repository` has `branch` checked out (a removed slice's branch never is)."""
    return f"branch refs/heads/{branch}\n" in _quiet_git(repository, "worktree", "list", "--porcelain") + "\n"


def _as_worktree(path: Path, top: Path, repositories: list[Path], branch: str) -> FolderRestored | None:
    """`top` put back as a worktree of ONE repository: the one it is named after, else the only one
    holding `branch`. On `branch` when that repository has it (detached at its commit while another
    worktree has it checked out), else detached at the repository's current commit."""
    named = _named_after(repositories, top)
    holders = [repository for repository in repositories if _has_branch(repository, branch) and not _checked_out(repository, branch)] if named is None else []
    repository = named or (holders[0] if len(holders) == 1 else None)
    if repository is None:
        return None
    kept = _has_branch(repository, branch)
    _quiet_git(repository, "worktree", "prune")  # a removed folder git still lists refuses its path
    if kept and not _checked_out(repository, branch):
        _git(repository, "worktree", "add", str(top), branch)
        restored = FolderRestored(path=path, top=top, restored_as="worktree_branch", repository=repository, branch=branch, commit=_git(top, "rev-parse", "HEAD"))
    else:
        line = branch if kept else _quiet_git(repository, "branch", "--show-current")
        commit = _git(repository, "rev-parse", f"refs/heads/{branch}" if kept else "HEAD")
        _git(repository, "worktree", "add", "--detach", str(top), commit)
        restored = FolderRestored(path=path, top=top, restored_as="worktree_detached", repository=repository, branch=line, commit=commit,
                                  missing_branch="" if kept else branch)
    path.mkdir(parents=True, exist_ok=True)  # a folder of the run's the commit does not hold (build output): empty, inside it
    return restored


def _as_tracked(path: Path, existing: Path) -> FolderRestored | None:
    """`path` put back from the commit its enclosing repository has checked out, when that commit
    holds it, as its index has it (staged edits kept). Its removal staged for commit is someone's
    decision: refused, never undone."""
    enclosing = _enclosing(existing)
    if enclosing is None:
        return None
    try:
        relative = Path(os.path.realpath(path)).relative_to(os.path.realpath(enclosing)).as_posix()
    except ValueError:
        return None
    if relative == "." or not _quiet_git(enclosing, "ls-tree", "-d", "HEAD", "--", relative):
        return None
    if _quiet_git(enclosing, "diff", "--cached", "--name-only", "--diff-filter=D", "--", relative):
        raise FolderUnrecoverable(path, f"Sa suppression est prête à être commitée dans {enclosing.name} : Galaius ne l’annule pas.",
                                  f"Annulez-la (git restore --staged -- {relative}) ou recréez ce dossier, puis appuyez de nouveau sur « Continuer ».")
    _git(enclosing, "restore", "--worktree", "--", relative)
    return FolderRestored(path=path, top=path, restored_as="repo_files", repository=enclosing)


def _as_empty(path: Path) -> FolderRestored:
    path.mkdir(parents=True, exist_ok=True)
    return FolderRestored(path=path, top=path, restored_as="empty")


def restore(cwd: str, provider: AgentProvider, environment: Mapping[str, str], session: str) -> FolderRestored | None:
    """Put the run folder `cwd` back when it is missing (None when it is there, or names none), once
    however many runs resume in it at once (`registry.folder_lock`). `session`: the vendor session the
    turn resumes, whose saved conversation names the branch. In a folder put here from the web
    (`UNTRUSTED_FOLDERS`, `PROJECT_SETTINGS_OFF` for the run's own) no git runs: code its owner never
    read would get to run (its config's filters, its hooks), so it comes back empty."""
    if not cwd:
        return None
    path = Path(cwd).expanduser()
    if path.is_dir():
        return None
    existing = next((folder for folder in path.parents if folder.is_dir()), None)
    if path.exists() or existing is None:
        raise FolderUnrecoverable(path, f"{'Un fichier occupe ce chemin' if path.exists() else 'Aucun dossier parent n’existe'} : Galaius ne peut pas le recréer.",
                                  "Libérez ou recréez ce chemin, puis appuyez de nouveau sur « Continuer ».")
    if not os.access(existing, os.W_OK):
        raise _denied(path, existing)  # said by the right itself, never by an OS or git sentence in the PC's language
    # The removed folder itself: the run's own, or the one it lay in (`<worktree>/frontend`).
    top = existing / path.relative_to(existing).parts[0]
    untrusted = tuple(Path(os.path.realpath(folder)) for folder in environment.get(UNTRUSTED_FOLDERS, "").split(os.pathsep) if folder)
    with reg.folder_lock(top):
        if path.is_dir():
            return None  # another run's resume put it back meanwhile
        try:
            if environment.get(PROJECT_SETTINGS_OFF) or _within(path, untrusted):
                return _as_empty(path)
            return (_as_tracked(path, existing)
                    or _as_worktree(path, top, _repositories(top, existing, untrusted), _worked_branch(provider, environment, path, session))
                    or _as_empty(path))
        except subprocess.CalledProcessError as error:
            said = next((line.strip() for line in reversed((error.stderr or "").splitlines()) if line.strip()), f"code {error.returncode}")
            raise FolderUnrecoverable(path, f"git n’a pas pu le recréer ({said}).") from error
        except OSError as error:
            if error.errno in DENIED:
                raise _denied(path, Path(error.filename).parent if isinstance(error.filename, str) else existing) from error
            said = OS_REFUSALS.get(error.errno or 0, f"erreur système {errno.errorcode.get(error.errno or 0, error.errno)}")
            raise FolderUnrecoverable(path, f"Galaius n’a pas pu le recréer : {said}.") from error
        except subprocess.TimeoutExpired as error:
            raise FolderUnrecoverable(path, f"git n’a pas répondu en {GIT_SECONDS:.0f} s.") from error


def _denied(path: Path, folder: Path) -> FolderUnrecoverable:
    """No right to write in `folder`, where the run's folder goes back."""
    return FolderUnrecoverable(path, f"Pas le droit d’écrire dans {shown(folder)} : Galaius ne peut pas y recréer ce dossier.",
                               "Donnez-vous le droit d’écrire dans ce dossier, ou recréez vous-même le dossier de la conversation, puis appuyez de nouveau sur « Continuer ».")
