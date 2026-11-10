"""A conversation is held by the server, not by a PC (`PcFeature.RUN_HOLD`): the PC running it sends the server a copy
between turns, and any of its owner's PCs takes it from that copy as the SAME conversation — its id, history and CLI
session, its next turn there resuming that session. The PC that ran it is never asked at that time.

On the PC copying it (`RunPackFiles`): the run's registry files (record, streams, messages, images) and its CLI session's
files (`AgentProvider.session_files`; gone here, as a sign-out deleted them, left out when its program writes them again
from the stream, `rebuilds_sessions`), plain files only, never a credential, never a link, within `RUN_PACK_MAX_BYTES` /
`_FILES`; packed as a gzip'd tar and uploaded through the server's relay as a project copy is (`WorkspaceTransfers`).

On the PC taking it (`RunPackReader`): downloaded, its digest checked, extracted into a fresh staging folder taking only
`run/<id>.<registry suffix>`, `run/<id>.media.<key>` and the session's own names (`AgentProvider.carries`: never the
CLI folder's memory or settings, threat-modeler 2026-11-09 Q4), then each file placed, never over one there unless that
PC held this conversation before (it comes back). A copy without its session has it written from its stream at its
next turn (`registry.resumable`)."""

import os
import re
import shutil
import stat
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, ClassVar
from uuid import UUID

from galaius_core import MEDIA_KEY, RUN_PACK_MAX_BYTES, RUN_PACK_MAX_FILES, AgentRefused, RunPack, TooLarge, WorkspaceArchive
from pydantic import BaseModel, ConfigDict

from galaius.agents import registry as reg
from galaius.agents.limits.subscriptions import Subscription
from galaius.agents.providers import AgentProvider
from galaius.machine.projects.workspace_copy import WorkspaceArchiveReader, WorkspaceRefused

#: The archive's two folders: the run's registry files, and its CLI session's files below `AgentProvider.session_folder`.
RUN, SESSION = "run", "session"
#: A run's registry files carried, by what follows its id: never its lock, error output, queue or kept downloads.
SUFFIXES: tuple[str, ...] = (".json", ".jsonl", ".raw.jsonl", ".messages.jsonl")


def session_of(run: reg.AgentRun) -> UUID:
    """The CLI session run `run`'s next turn resumes, refused typed when it is no UUID (it becomes a path and an argument
    on the other PC)."""
    try:
        return UUID(run.provider_session_id or run.run_id)
    except ValueError:
        raise AgentRefused("not_here", "this conversation's CLI session cannot move to another computer") from None


def session_home(program: AgentProvider, run: reg.AgentRun) -> Path:
    """The login folder holding `run`'s CLI session: its fence's own state folder, else its login's."""
    return run.fence.state / program.name if run.fence is not None else reg.login_folder(program, run.subscription)


def whole(program: AgentProvider, run_id: str, names: set[str]) -> bool:
    """Whether a conversation of `program` whose archive holds `names` continues: its CLI session is in it, or its stream
    writes that session again on the PC taking it (`AgentProvider.rebuilds_sessions`)."""
    return any(name.startswith(f"{SESSION}/") for name in names) or program.rebuilds_sessions and f"{RUN}/{run_id}.raw.jsonl" in names


class RunPackFiles(BaseModel):
    """What carrying run `run` sends: each archive name with the file behind it, measured (`measure`) and packed (`write`)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    run: reg.AgentRun
    program: AgentProvider

    def files(self) -> Iterator[tuple[str, Path]]:
        run_id = self.run.run_id
        registry = reg.agents_dir()
        for suffix in SUFFIXES:
            yield f"{RUN}/{run_id}{suffix}", registry / f"{run_id}{suffix}"
        for path in sorted(registry.glob(f"{run_id}.media.*")):
            yield f"{RUN}/{path.name}", path
        home, session = session_home(self.program, self.run), str(session_of(self.run))
        folder = self.program.session_folder(home, Path(self.run.cwd))
        for path in self.program.session_files(home, Path(self.run.cwd), session):
            if path.name not in Subscription.CREDENTIALS and self.program.carries(parts := path.relative_to(folder).parts, session):
                yield f"{SESSION}/{'/'.join(parts)}", path

    @staticmethod
    def plain(path: Path) -> os.stat_result | None:
        try:
            facts = path.lstat()
        except FileNotFoundError:
            return None
        return facts if stat.S_ISREG(facts.st_mode) else None

    def measure(self) -> tuple[RunPack, list[tuple[str, BinaryIO, int]]]:
        """Its plain files (a missing one, as a run without images, is left out), each OPENED now, between turns, with its size
        then: its copy holds exactly those bytes, whatever the next turn appends or a record rewritten whole meanwhile
        (code-reviewer 1a9989c7 L1); refused typed when its record is missing, its CLI session too and no stream writes it
        again (`whole`), or it is past `RUN_PACK_MAX_BYTES` / `_FILES`. The caller closes them (`write` does)."""
        found = [(name, path) for name, path in self.files() if self.plain(path) is not None]
        names = {name for name, _ in found}
        if f"{RUN}/{self.run.run_id}.json" not in names:
            raise WorkspaceRefused("source_missing", "this conversation's record is not on this computer")
        if not whole(self.program, self.run.run_id, names):
            raise WorkspaceRefused("source_missing", f"this conversation's CLI session is not on this computer and {self.program.name} cannot write it from its stream")
        if len(found) > RUN_PACK_MAX_FILES:
            raise AgentRefused(TooLarge(limit=RUN_PACK_MAX_FILES, unit="entries"), f"this conversation holds {len(found)} files: more than one move carries")
        opened: list[tuple[str, BinaryIO, int]] = []
        try:
            for name, path in found:
                try:
                    stream = os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)), "rb")
                except FileNotFoundError:
                    continue   # gone since it was listed (an image removed): left out, as a missing one is
                facts = os.fstat(stream.fileno())
                if not stat.S_ISREG(facts.st_mode):
                    stream.close()
                    raise WorkspaceRefused("failed", f"{name} is no plain file")
                opened.append((name, stream, facts.st_size))
            size = sum(length for _, _, length in opened)
            if size > RUN_PACK_MAX_BYTES:
                raise AgentRefused(TooLarge(limit=RUN_PACK_MAX_BYTES, unit="bytes"), f"this conversation holds {size:,} bytes: more than one move carries")
        except BaseException:
            for _, stream, _ in opened:
                stream.close()
            raise
        return RunPack(files=len(opened), size=size, session=session_of(self.run)), opened

    @staticmethod
    def write(files: list[tuple[str, BinaryIO, int]], archive: Path) -> None:
        """`files` (opened by `measure`) as a gzip'd tar at `archive`, each its first `size` bytes, every one closed after."""
        try:
            with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as packed:
                for name, stream, size in files:
                    member = tarfile.TarInfo(name)
                    member.size, member.mtime, member.mode = size, int(os.fstat(stream.fileno()).st_mtime), 0o644
                    stream.seek(0)
                    packed.addfile(member, stream)
        finally:
            for _, stream, _ in files:
                stream.close()


class RunPackReader(BaseModel):
    """A carried conversation's archive, received for run `run_id` and CLI session `session` of `program`: extracted into
    `staging` (a fresh folder) taking only its own names, then read (`record`) and placed (`place`)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    run_id: str
    session: str
    program: AgentProvider
    staging: Path
    #: The registry file name of an image a run kept (`RunMedia.path`).
    MEDIA: ClassVar[str] = r"\.media\." + MEDIA_KEY.removeprefix("^").removesuffix("$")

    def allowed(self, parts: tuple[str, ...]) -> bool:
        if len(parts) == 2 and parts[0] == RUN:
            return parts[1] in {f"{self.run_id}{suffix}" for suffix in SUFFIXES} or re.fullmatch(re.escape(self.run_id) + self.MEDIA, parts[1]) is not None
        return len(parts) > 1 and parts[0] == SESSION and self.program.carries(parts[1:], self.session)

    def extract(self, archive: WorkspaceArchive, received: Path) -> int:
        if archive.files > RUN_PACK_MAX_FILES:
            raise WorkspaceRefused("unsafe_archive", f"the conversation declares {archive.files} files: more than one move carries")
        self.staging.mkdir(mode=0o700)
        return WorkspaceArchiveReader(archive=archive, base=self.staging.parent, parts=(self.staging.name,), most=RUN_PACK_MAX_BYTES, allowed=self.allowed).extract(received)

    def record(self) -> dict:
        """The sending PC's record, as JSON: refused `unsafe_archive` when absent, unreadable or naming another session."""
        try:
            raw = reg.AgentRun.model_validate_json((self.staging / RUN / f"{self.run_id}.json").read_bytes())
        except (OSError, ValueError) as error:
            raise WorkspaceRefused("unsafe_archive", f"the conversation's record is missing or unreadable ({type(error).__name__})") from None
        if raw.run_id != self.run_id or (raw.provider_session_id or raw.run_id) != self.session:
            raise WorkspaceRefused("unsafe_archive", "the conversation's record names another run or CLI session")
        names = {f"{RUN}/{path.name}" for path in self._below(RUN)} | {
            f"{SESSION}/{path.relative_to(self.staging / SESSION).as_posix()}" for path in self._below(SESSION)
            if self.program.carries(path.relative_to(self.staging / SESSION).parts, self.session)}
        if not whole(self.program, self.run_id, names):
            raise WorkspaceRefused("unsafe_archive", "the conversation arrived without its CLI session or a stream that writes it")
        return raw.model_dump(mode="json")

    def _below(self, folder: str) -> list[Path]:
        root = self.staging / folder
        return sorted(path for path in root.rglob("*") if path.is_file()) if root.is_dir() else []

    def targets(self, session_folder: Path) -> list[tuple[Path, Path]]:
        """Each staged file with where it goes: the registry's own files beside every run's, the session's below
        `session_folder` (this PC's login folder for its CLI). The record is written apart (`MachineAgents._import`)."""
        registry = reg.agents_dir()
        placed = [(path, registry / path.name) for path in self._below(RUN) if path.name != f"{self.run_id}.json"]
        return placed + [(path, session_folder.joinpath(*path.relative_to(self.staging / SESSION).parts)) for path in self._below(SESSION)]

    @staticmethod
    def place(targets: list[tuple[Path, Path]], replace: bool) -> None:
        """Every staged file moved to its place: refused `exists` before any is moved when one is there already and
        `replace` is not set (this PC never held this conversation); the session's folders made private."""
        if not replace and (taken := next((target for _, target in targets if os.path.lexists(target)), None)) is not None:
            raise WorkspaceRefused("exists", f"{taken.name} is already on this computer: this conversation is never written over another")
        for source, target in targets:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source.chmod(0o600)
            os.replace(source, target)

    def discard(self) -> None:
        shutil.rmtree(self.staging, ignore_errors=True)
