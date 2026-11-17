"""A project folder's git repositories as this PC holds them (`GitFolder.state`), and their history carried from one of
the owner's PCs to another with a copy of the folder (`GitFolder.bundles` on the PC sending it, `GitFolder.rebuild` on
the one receiving it).

git runs here on a folder an agent may have written in, outside any fence: never a command its repository's own
config or attributes could turn into another program. So no `git status` / `diff` (a clean filter, an fsmonitor hook
or an external diff would run): the changed files are found by comparing each tracked file with what the index records
(`ls-files --stage --debug`, its size and time, then its git blob hash computed here), and only plumbing that reads
objects runs (`rev-parse`, `for-each-ref`, `ls-files`, `bundle create`). A received history is unbundled into a
repository made here with no template and no hook, then checked out by its index only: the files stay as they came, so
the uncommitted work the sending PC had shows as uncommitted here."""

import hashlib
import json
import logging
import os
import re
import shutil
import socket
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from galaius_core import FOLDER_REPOS_MAX, REPO_BRANCHES_MAX, REPO_FILES_MAX, BranchTip, ChangedFile, FolderState, GitRemote, RepoPath, RepoState
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from galaius.machine.projects.workspace_copy import CARRIED_GIT
from galaius.machine.projects.workspace_git import Git, WorkspaceRefused

logger = logging.getLogger(__name__)

#: How git fails here, each one leaving that repository (or that history) out, never the copy: refused or missing,
#: unreadable, past its deadline (git's own `TimeoutExpired` too), a value no contract holds.
GIT_FAILED = (WorkspaceRefused, OSError, TimeoutError, subprocess.TimeoutExpired, ValueError)
REPO_PATH: TypeAdapter[str] = TypeAdapter(RepoPath)
#: Past this size a file is hashed as it is read, its CRLF twin never compared (a Windows checkout's large files).
WHOLE_MAX = 16 * 1024**2


def contained(base: Path, path: str) -> Path | None:
    """`base / path` when `path` is a `RepoPath` whose folder resolves inside `base` (no link on the way out), else None
    (threat review 2026-11-17 C4: an index or another PC names it)."""
    try:
        REPO_PATH.validate_python(path)
    except ValidationError:
        return None
    file = base / path
    return file if file.parent.resolve().is_relative_to(base.resolve()) else None


def digests(file: Path, blob_length: int) -> tuple[set[str], str]:
    """`file`'s git blob ids (as it is, and with its line ends as LF while small) and its sha256, read once in parts."""
    facts = file.lstat()
    if not (stat.S_ISREG(facts.st_mode) or stat.S_ISLNK(facts.st_mode)):   # a FIFO or a device is never opened (threat r2 C4)
        raise IsADirectoryError(f"{file} is no plain file")
    if stat.S_ISLNK(facts.st_mode):
        body = os.readlink(file).encode()
        return {GitFolder.blob(body, blob_length)}, hashlib.sha256(body).hexdigest()
    if facts.st_size <= WHOLE_MAX:
        body = file.read_bytes()
        return {GitFolder.blob(body, blob_length), GitFolder.blob(body.replace(b"\r\n", b"\n"), blob_length)}, hashlib.sha256(body).hexdigest()
    blob, whole = (hashlib.sha1 if blob_length == 40 else hashlib.sha256)(b"blob %d\0" % facts.st_size), hashlib.sha256()
    with file.open("rb") as stream:
        while part := stream.read(1 << 20):
            blob.update(part)
            whole.update(part)
    return {blob.hexdigest()}, whole.hexdigest()


class GitFolder(BaseModel):
    """`folder`'s git repositories: its own (when it is a checkout's top) and its submodules', nested ones too."""

    model_config = ConfigDict(frozen=True)
    folder: Path
    environment: dict[str, str]
    #: Longest all of one folder's reads may take together.
    seconds: float = 60
    #: Options no repository config overrides here.
    OPTIONS: ClassVar[tuple[str, ...]] = (
        "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.quotePath=false", "-c", "core.hooksPath=/dev/null",
        "-c", "protocol.allow=never",
    )
    #: Where a copy carries the history inside its archive: one bundle per repository and the list naming them.
    CARRIED: ClassVar[str] = CARRIED_GIT
    MANIFEST: ClassVar[str] = "repos.json"
    #: What git leaves in a repository's folder while an operation is under way, by `RepoState.in_progress`.
    UNDER_WAY: ClassVar[dict[str, str]] = {"MERGE_HEAD": "merge", "rebase-merge": "rebase", "rebase-apply": "rebase", "CHERRY_PICK_HEAD": "cherry-pick",
                                          "REVERT_HEAD": "revert", "BISECT_LOG": "bisect"}
    #: One `ls-files --stage --debug -z` entry: mode, blob, stage, path, then the stat the index keeps of it.
    _ENTRY: ClassVar[re.Pattern[str]] = re.compile(
        r"(\d{6}) ([0-9a-f]{40,64}) (\d)\t([^\0]*)\0\s*ctime: \d+:\d+\s*mtime: (\d+):(\d+)\s*dev: \d+\s*ino: \d+\s*uid: \d+\s*gid: \d+\s*size: (\d+)\s*flags: [0-9a-f]+\s*")

    def _git(self) -> Git:
        return Git(options=self.OPTIONS, environment={**self.environment, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}, host="", deadline=time.monotonic() + self.seconds)

    def repos(self) -> list[tuple[str, Path]]:
        """Its repositories as (path below the folder, checkout): its own first when it is a checkout's top, then each
        submodule checked out in one (a gitlink whose folder holds its own `.git`), at most `FOLDER_REPOS_MAX`."""
        if not Git.installed(self.environment) or not (self.folder / ".git").exists():
            return []
        if not self.own(self.folder):
            return []
        git, found, queue = self._git(), [("", self.folder)], [("", self.folder)]
        while queue and len(found) < FOLDER_REPOS_MAX:
            path, checkout = queue.pop(0)
            for entry in git.run("-C", str(checkout), "ls-files", "-z", "--stage").split("\0"):
                if not entry.startswith("160000 "):
                    continue
                inner = entry.split("\t", 1)[1]
                nested = checkout / inner
                if nested.is_symlink() or not (nested / ".git").exists() or not nested.resolve().is_relative_to(self.folder.resolve()) or not self.own(nested):
                    continue
                child = (f"{path}/{inner}" if path else inner, nested)
                found.append(child)
                queue.append(child)
        return found[:FOLDER_REPOS_MAX]

    def state(self, pc: str | None = None) -> FolderState:
        """The folder now, as this PC holds it (`pc`: its name, this computer's host name by default); a repository git
        cannot read is left out."""
        repos = []
        for path, checkout in self.repos():
            try:
                repos.append(self.repo(path, checkout))
            except GIT_FAILED:
                continue
        return FolderState(pc=(pc or socket.gethostname() or "?")[:80], at=datetime.now(UTC), repos=tuple(repos))

    def repo(self, path: str, checkout: Path) -> RepoState:
        """One repository: its branch, commit, local branches, origin, uncommitted and untracked files."""
        git = self._git()
        head = git.run("-C", str(checkout), "rev-parse", "--verify", "-q", "HEAD", allow=(1,)).strip() or None
        branch = git.run("-C", str(checkout), "symbolic-ref", "-q", "--short", "HEAD", allow=(1,)).strip() or None
        tips = [line.split("\0") for line in git.run("-C", str(checkout), "for-each-ref", "--format=%(refname:short)%00%(objectname)", "refs/heads").splitlines() if "\0" in line]
        origin = git.run("-C", str(checkout), "config", "--get", "remote.origin.url", allow=(1,)).strip()
        changed = self.changed(git, checkout)
        untracked = [name.rstrip("/") for name in git.run("-C", str(checkout), "ls-files", "-z", "--others", "--exclude-standard", "--directory", "--no-empty-directory").split("\0")
                     if name and contained(checkout, name.rstrip("/")) is not None]
        line_ends = git.run("-C", str(checkout), "config", "--get", "core.autocrlf", allow=(1,)).strip().lower()
        repository = Path(git.run("-C", str(checkout), "rev-parse", "--absolute-git-dir").strip())
        under_way = next((operation for name, operation in self.UNDER_WAY.items() if (repository / name).exists()), None)
        tips = [tip for tip in tips if self.shown(tip[0])]   # a branch name no card may show is left out, never the repository
        branch = branch if branch is None or self.shown(branch) else None
        return RepoState(path=path, branch=branch, head=head, origin=GitRemote.stripped(origin) if origin else None,
                         line_ends=line_ends if line_ends in ("true", "input", "false") else None, in_progress=under_way,
                         branches=tuple(BranchTip(name=name, head=sha) for name, sha in tips[:REPO_BRANCHES_MAX]), branches_more=max(0, len(tips) - REPO_BRANCHES_MAX),
                         changed=tuple(changed[:REPO_FILES_MAX]), changed_more=max(0, len(changed) - REPO_FILES_MAX),
                         untracked=tuple(untracked[:REPO_FILES_MAX]), untracked_more=max(0, len(untracked) - REPO_FILES_MAX))

    @staticmethod
    def shown(name: str) -> bool:
        """Whether a branch name may be reported (`BranchTip.name`'s rule)."""
        try:
            BranchTip(name=name, head="0" * 40)
        except ValidationError:
            return False
        return True

    def changed(self, git: Git, checkout: Path) -> list[ChangedFile]:
        """The tracked files whose content is not what the index records: deleted, or a size, time and then a git
        blob hash (also with its line ends as LF, a Windows checkout's) that differs. Conflicted entries count once."""
        listed = git.run("-C", str(checkout), "ls-files", "-z", "--stage", "--debug")
        changed, seen = [], set()
        for mode, blob, _stage, path, seconds, nanos, size in self._ENTRY.findall(listed):
            if mode == "160000" or path in seen or (file := contained(checkout, path)) is None:
                continue
            seen.add(path)
            try:
                facts = file.lstat()
            except FileNotFoundError:
                changed.append(ChangedFile(path=path, digest=None))
                continue
            if int(seconds) and facts.st_size == int(size) and facts.st_mtime_ns == int(seconds) * 10**9 + int(nanos):
                continue
            if not (stat.S_ISREG(facts.st_mode) or stat.S_ISLNK(facts.st_mode)):
                continue
            blobs, whole = digests(file, len(blob))
            if blob not in blobs:
                changed.append(ChangedFile(path=path, digest=whole))
        return changed

    @staticmethod
    def blob(body: bytes, length: int) -> str:
        """`body`'s git blob id, in the object format a `length`-character id names."""
        algorithm = hashlib.sha1 if length == 40 else hashlib.sha256
        return algorithm(b"blob %d\0" % len(body) + body).hexdigest()

    def has(self, checkout: Path, commit: str) -> bool:
        """Whether repository `checkout` holds commit `commit`."""
        try:
            return bool(self._git().run("-C", str(checkout), "rev-parse", "-q", "--verify", f"{commit}^{{commit}}", allow=(1, 128)).strip())
        except GIT_FAILED:
            return False

    # ---- carried with a copy ---------------------------------------------------------------------------------

    def bundles(self, into: Path, room: int) -> list[tuple[str, Path, int]]:
        """Each repository's history on its checked-out commit as a git bundle in `into`, while they fit in `room` bytes,
        plus the list naming them (`MANIFEST`): (name in the archive, file, size) each. A repository with no commit, or
        whose bundle git could not make in time or that does not fit, is named without one (it arrives as files only)."""
        into.mkdir(parents=True, exist_ok=True)
        carried, named, used = [], [], 0
        for index, (path, checkout) in enumerate(self.repos()):
            try:
                state = self.repo(path, checkout)
            except GIT_FAILED:
                continue
            bundle, file = None, into / f"{index}.bundle"
            try:
                if state.head is not None:   # a detached checkout (a submodule as `git submodule update` leaves it) by its HEAD
                    self._git().run("-C", str(checkout), "bundle", "create", "-q", str(file), "HEAD" if state.branch is None else f"refs/heads/{state.branch}")
                    if used + (size := file.stat().st_size) <= room:
                        used, bundle = used + size, file.name
                        carried.append((f"{self.CARRIED}/{file.name}", file, size))
            except GIT_FAILED as error:
                logger.warning("the history of %r is not carried: %s", path, error)
            if bundle is None:
                file.unlink(missing_ok=True)
            named.append({"state": state.model_dump(mode="json"), "bundle": bundle})
        manifest = into / self.MANIFEST
        manifest.write_text(json.dumps(named), encoding="utf-8")
        return [*carried, (f"{self.CARRIED}/{self.MANIFEST}", manifest, manifest.stat().st_size)]

    def own(self, checkout: Path) -> bool:
        """Whether repository `checkout` is the project's own, read and carried from here (threat review 2026-11-17 C2: an
        agent writing in the folder could make a `.git` name any repository of this PC): no link among the parts git reads
        objects and refs from, no `alternates`, and its git folders inside the project folder, or outside it only where
        git's own back-link there names this checkout (a worktree's `gitdir`, a submodule's `core.worktree`: tester r2,
        his `.worktrees/*` and `worktrees/*`), which nothing inside the folder can write."""
        try:
            git, base = self._git(), self.folder.resolve()
            git_dir, common = (Path(git.run("-C", str(checkout), "rev-parse", "--path-format=absolute", option).strip()).resolve()
                               for option in ("--git-dir", "--git-common-dir"))
            if any(self.linked(folder) for folder in {git_dir, common}) or (common / "objects" / "info" / "alternates").exists():
                return False
            if git_dir.is_relative_to(base) and common.is_relative_to(base):
                return True
            marker = checkout / ".git"
            if not marker.is_file() or marker.is_symlink():
                return False
            if (git_dir / "gitdir").is_file() and Path((git_dir / "gitdir").read_text(encoding="utf-8").strip()).resolve() == marker.resolve():
                return True
            worktree = git.run("config", "-f", str(git_dir / "config"), "--get", "core.worktree", allow=(1,)).strip()
            return bool(worktree) and (git_dir / worktree).resolve() == checkout.resolve()
        except GIT_FAILED:
            return False

    @staticmethod
    def linked(folder: Path) -> bool:
        """Whether git folder `folder` reads anything through a link (threat review r3 C2: one level below the names git
        reads, `objects/ab` or `refs/heads/x`, leaks another repository as well): `HEAD`, `index`, `packed-refs`,
        `objects`, `refs`, each entry directly in `objects`, `objects/pack`, `objects/info`, and every entry under `refs`."""
        names = [folder / name for name in ("HEAD", "index", "packed-refs", "objects", "refs")]
        for below in ("objects", "objects/pack", "objects/info"):
            if (folder / below).is_dir() and not (folder / below).is_symlink():
                names += list((folder / below).iterdir())
        refs = folder / "refs"
        if refs.is_dir() and not refs.is_symlink():
            for top, folders, files in os.walk(refs, followlinks=False):
                names += [Path(top) / name for name in (*folders, *files)]
        return any(name.is_symlink() for name in names)

    def rebuild(self) -> tuple[RepoState, ...]:
        """The history a copy carried (`CARRIED` in `folder`, removed after) made git repositories again: each one's
        branch at its commit, its origin, its index that commit (its files as they came: uncommitted work stays so);
        what arrived, as this PC reads it now. A copy without one, or git missing here: as it came, files only."""
        carried = self.folder / self.CARRIED
        try:
            if not (carried / self.MANIFEST).is_file() or not Git.installed(self.environment):
                return ()
            try:
                listed = [(RepoState.model_validate(item["state"]), item.get("bundle")) for item in json.loads((carried / self.MANIFEST).read_text(encoding="utf-8"))]
            except (*GIT_FAILED, KeyError, TypeError) as error:   # a list no sending PC writes: files only
                logger.warning("the copy's history list is unreadable: %s", error)
                return ()
            for state, bundle in sorted(listed, key=lambda item: item[0].path.count("/")):
                if not isinstance(bundle, str) or state.head is None:
                    continue
                try:
                    self._restore(state, carried / os.path.basename(bundle))
                except GIT_FAILED as error:   # that repository arrives as files only, said in the log
                    logger.warning("the history carried for %r was not restored: %s", state.path, error)
        finally:
            shutil.rmtree(carried, ignore_errors=True)
        return tuple(self.state().repos)

    def _restore(self, state: RepoState, bundle: Path) -> None:
        """One repository from its bundle, at its place in the folder (never outside it, never over a `.git` there); the
        `.git` it made removed again when it fails."""
        checkout = (self.folder / state.path).resolve()
        if not checkout.is_relative_to(self.folder.resolve()) or (checkout / ".git").exists() or not checkout.is_dir():
            raise WorkspaceRefused("unsafe_archive", f"the copy's history names {state.path!r}, which is no folder it carried")
        # Neither this PC's global nor system git config: none of its filters (git-lfs's smudge fetching) runs here (tester r2).
        git = self._git().model_copy(update={"environment": {**self._git().environment, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}})
        git.run("init", "-q", "--template=", str(checkout))
        # Empty but there: the fence keeps a repository's hooks and info read-only only where they exist (F1).
        for name in ("hooks", "info"):
            (checkout / ".git" / name).mkdir(exist_ok=True)
        try:
            self._fill(git, checkout, state, bundle)
        except BaseException:
            shutil.rmtree(checkout / ".git", ignore_errors=True)
            raise

    @staticmethod
    def _fill(git: Git, checkout: Path, state: RepoState, bundle: Path) -> None:
        if state.line_ends is not None:   # before the index is read: a CRLF checkout reads unchanged here too
            git.run("-C", str(checkout), "config", "core.autocrlf", state.line_ends)
        git.run("-C", str(checkout), "bundle", "unbundle", str(bundle))
        assert state.head is not None
        if state.branch is not None:
            git.run("-C", str(checkout), "update-ref", f"refs/heads/{state.branch}", state.head)
            git.run("-C", str(checkout), "symbolic-ref", "HEAD", f"refs/heads/{state.branch}")
        else:
            git.run("-C", str(checkout), "update-ref", "--no-deref", "HEAD", state.head)
        git.run("-C", str(checkout), "read-tree", state.head)
        # A tracked file the copy left out (a credential-named one, a link): from the commit, never shown deleted (C3).
        deleted = {file.path for file in state.changed if file.digest is None}
        missing = [path for path in git.run("-C", str(checkout), "ls-files", "-z").split("\0")
                   if path and path not in deleted and (file := contained(checkout, path)) is not None and not os.path.lexists(file)]
        for start in range(0, len(missing), 200):
            git.run("-C", str(checkout), "checkout-index", "-f", "--", *missing[start:start + 200])
        git.run("-C", str(checkout), "update-index", "-q", "--refresh", allow=(1,))
        if state.origin is not None:
            git.run("-C", str(checkout), "remote", "add", "origin", state.origin.url)
            if state.branch is not None:
                git.run("-C", str(checkout), "config", f"branch.{state.branch}.remote", "origin")
                git.run("-C", str(checkout), "config", f"branch.{state.branch}.merge", f"refs/heads/{state.branch}")
