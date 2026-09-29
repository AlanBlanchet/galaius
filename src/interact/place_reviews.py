"""Write after review: writes into a `write_on_review` folder land in a staging copy held by the
runner, never in the folder; the owner reads the diff and accepts it ON THE PC by its digest; the
PC then applies exactly that diff.

One review = `<root>/<id>/`: `meta.json` (place, origin, run), `base.json` (sha256 of each file as
it was when staged: the diff is the writer's changes, never someone else's edit made meanwhile) and
`tree/` (an agent's full copy of the folder, or only the files a workflow step wrote). Accepting
re-computes the diff, requires the same digest, requires every touched file of the folder to still
be what it was when staged, then writes through `PinnedDirectory` (no link followed). Anything
under `.git` is never applied; a link or special file in the copy blocks the review."""

import difflib
import hashlib
import json
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from interact_core import MachinePlaceReview, MachineReviewFile

from interact.agents import registry as reg
from interact.pinned_directory import PinnedDirectory
from interact.fence import STEERING
from interact.places import NEVER_GRANTABLE
from interact.paths import UserPaths

Origin = Literal["agent", "workflow"]


def write_plain(folder: PinnedDirectory, name: str, content: bytes) -> None:
    """`content` as `name` in `folder`: written beside it, then moved over it in one step, never
    through a link or a hard link planted at the destination (the link would carry the write elsewhere)."""
    try:
        existing = folder.stat(name)
    except FileNotFoundError:
        existing = None
    if existing is not None and (folder.link_like(existing) or not stat.S_ISREG(existing.st_mode) or existing.st_nlink > 1):
        raise PermissionError(f"{name} is a link or not a plain file: it is never overwritten")
    partial = f".{name}.{uuid4().hex}.part"
    descriptor = folder.file(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        folder.replace(partial, folder, name)
    finally:
        try:
            folder.unlink(partial)
        except FileNotFoundError:
            pass


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            sha.update(chunk)
    return sha.hexdigest()


def _live(folder: Path, relative: str) -> str | None:
    """sha256 of `relative` in `folder` reached without a link, None when absent."""
    parts = relative.split("/")
    try:
        with PinnedDirectory.open(folder, *parts[:-1]) as pinned:
            descriptor = pinned.file(parts[-1], os.O_RDONLY | PinnedDirectory.NONBLOCK)
    except (FileNotFoundError, NotADirectoryError):
        return None
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return "not-a-file"
        sha = hashlib.sha256()
        while chunk := stream.read(1 << 20):
            sha.update(chunk)
        return sha.hexdigest()


class Manifest(BaseModel):
    """What a review would apply: (path, change, new sha256, base sha256) per file, sorted."""

    model_config = ConfigDict(frozen=True)
    entries: tuple[tuple[str, Literal["added", "changed", "deleted"], str | None, str | None], ...] = ()
    blocked: str = ""
    #: Changes inside `.git` left out (never applied).
    dropped: int = 0
    #: The new bytes of each added / changed file, read once with their digest (`manifest(keep=True)`).
    contents: dict[str, bytes] = {}

    def digest(self, review_id: UUID, place: str) -> str:
        canonical = json.dumps({"id": str(review_id), "place": place, "entries": [list(entry) for entry in self.entries]}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class PlaceReviews(BaseModel):
    """The staging store of one runner (owner-only folder beside its data)."""

    model_config = ConfigDict(frozen=True)
    root: Path
    #: Largest folder an agent's staging copy takes (files, bytes): a bigger folder is refused.
    max_files: int = 20_000
    max_bytes: int = 1 << 30
    #: Files one review lists; diff lines one read carries.
    LISTED: ClassVar[int] = 500
    DIFF_LINES: ClassVar[int] = 4000

    @classmethod
    def default(cls) -> "PlaceReviews":
        return cls(root=UserPaths.data() / "place-reviews")

    def _open(self, place: str, origin: Origin, run_id: UUID | None, mode: Literal["copy", "files"]) -> tuple[UUID, Path]:
        review_id = uuid4()
        folder = self.root / str(review_id)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (folder / "tree").mkdir(mode=0o700, parents=True)
        (folder / "meta.json").write_text(json.dumps({"id": str(review_id), "place": place, "origin": origin, "run_id": str(run_id) if run_id else None,
                                                      "mode": mode, "created_at": datetime.now(UTC).isoformat()}))
        return review_id, folder

    def stage_copy(self, place: str, folder: Path, *, origin: Origin, run_id: UUID | None) -> tuple[UUID, Path]:
        """A full copy of `folder` an agent then works in (links copied as links, special files
        left out): the review id and the copy's path. Refused past `max_files` / `max_bytes`."""
        review_id, staging = self._open(place, origin, run_id, "copy")
        tree, base, files, size = staging / "tree", {}, 0, 0
        try:
            for current, directories, names in os.walk(folder, followlinks=False):
                relative = Path(current).relative_to(folder)
                for name in directories:
                    source = Path(current) / name
                    if source.is_symlink():
                        (tree / relative / name).symlink_to(os.readlink(source))
                        base[(relative / name).as_posix()] = "link:" + os.readlink(source)
                    else:
                        (tree / relative / name).mkdir(mode=0o700)
                directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
                for name in names:
                    source, key = Path(current) / name, (relative / name).as_posix()
                    facts = source.lstat()
                    if stat.S_ISLNK(facts.st_mode):
                        (tree / key).symlink_to(os.readlink(source))
                        base[key] = "link:" + os.readlink(source)
                    elif stat.S_ISREG(facts.st_mode):
                        files, size = files + 1, size + facts.st_size
                        if files > self.max_files or size > self.max_bytes:
                            raise PermissionError(f"{place} is too large to stage for review (over {self.max_files} files or {self.max_bytes >> 20} MiB); "
                                                  "give the agent a smaller folder, or a sandbox")
                        shutil.copy2(source, tree / key, follow_symlinks=False)
                        base[key] = _digest(tree / key)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        (staging / "base.json").write_text(json.dumps(base))
        return review_id, tree

    def stage_file(self, place: str, folder: Path, parts: tuple[str, ...], content: bytes, *, origin: Origin, run_id: UUID | None, new: bool = False) -> UUID:
        """One file a workflow step writes into `folder` at `parts`, held in this run's review
        (`new`: the file was not in the folder before its writer, whatever is there now)."""
        found = next((meta for meta in self._metas() if meta["place"] == place and meta["mode"] == "files" and meta["run_id"] == (str(run_id) if run_id else None)), None)
        review_id, staging = (UUID(found["id"]), self.root / found["id"]) if found else self._open(place, origin, run_id, "files")
        base = json.loads((staging / "base.json").read_text()) if (staging / "base.json").exists() else {}
        key = "/".join(parts)
        base.setdefault(key, None if new else _live(folder, key))
        target = staging / "tree" / key
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(content)
        (staging / "base.json").write_text(json.dumps(base))
        return review_id

    def hold(self, place: str, folder: Path, path: Path) -> UUID:
        """`path` (a file or a folder inside `folder`) moved out of the folder into a review: its
        plain files copied (links and special files dropped), then removed from the folder without
        following a link. What a fenced agent creates that runs later outside the fence waits here."""
        review_id = None
        entries = [path] if not path.is_dir() or path.is_symlink() else [Path(current) / name for current, _, names in os.walk(path) for name in names]
        for entry in entries:
            facts = entry.lstat()
            if stat.S_ISREG(facts.st_mode):
                review_id = self.stage_file(place, folder, entry.relative_to(folder).parts, entry.read_bytes(), origin="agent", run_id=None, new=True)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        return review_id or self._open(place, "agent", None, "files")[0]

    def _metas(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        found = []
        for folder in sorted(self.root.iterdir()):
            try:
                found.append(json.loads((folder / "meta.json").read_text()))
            except (OSError, ValueError):
                continue
        return found

    def _meta(self, review_id: UUID) -> dict:
        try:
            return json.loads((self.root / str(review_id) / "meta.json").read_text())
        except (OSError, ValueError):
            raise PermissionError("no such review on this PC (already accepted or discarded)") from None

    def place(self, review_id: UUID) -> str:
        """The folder (its place path) a review writes into."""
        return self._meta(review_id)["place"]

    def attach(self, review_id: UUID, run_id: UUID) -> None:
        """The agent run working in this review's copy (known once it started)."""
        meta = self._meta(review_id)
        (self.root / str(review_id) / "meta.json").write_text(json.dumps({**meta, "run_id": str(run_id)}))

    def manifest(self, review_id: UUID, keep: bool = False) -> Manifest:
        """What the review would apply, steering files first (`STEERING`: what an editor, git or an
        agent runs later); `keep`: with the bytes each digest was computed from."""
        staging = self.root / str(review_id)
        meta, tree = self._meta(review_id), staging / "tree"
        base: dict[str, str | None] = json.loads((staging / "base.json").read_text()) if (staging / "base.json").exists() else {}
        entries, blocked, seen, contents, dropped = [], "", set(), {}, 0
        for current, directories, names in os.walk(tree, followlinks=False):
            relative = Path(current).relative_to(tree)
            inside_git = ".git" in {part.casefold() for part in relative.parts}
            for name in (*names, *(name for name in directories if (Path(current) / name).is_symlink())):
                key, facts = (relative / name).as_posix(), (Path(current) / name).lstat()
                seen.add(key)
                if inside_git:
                    dropped += stat.S_ISREG(facts.st_mode) and base.get(key) != _digest(Path(current) / name)
                    continue
                if stat.S_ISLNK(facts.st_mode):
                    if base.get(key) != "link:" + os.readlink(Path(current) / name):
                        blocked = blocked or f"{key} is a link: links are never applied"
                    continue
                if not stat.S_ISREG(facts.st_mode):
                    blocked = blocked or f"{key} is not a plain file"
                    continue
                if NEVER_GRANTABLE.refusal(tuple(part for part in key.split("/") if not part.startswith(".")), None) is not None:
                    blocked = blocked or f"{key}: credential stores are never written"
                data = (Path(current) / name).read_bytes()
                new, old = hashlib.sha256(data).hexdigest(), base.get(key)
                if old is None or new != old:
                    entries.append((key, "added" if old is None else "changed", new, old))
                    if keep:
                        contents[key] = data
        if meta["mode"] == "copy":
            gone = [(key, old) for key, old in base.items() if key not in seen and old is not None and not old.startswith("link:")]
            dropped += sum(1 for key, _ in gone if ".git" in key.casefold().split("/"))
            entries += [(key, "deleted", None, old) for key, old in gone if ".git" not in key.casefold().split("/")]
        entries.sort(key=lambda entry: (not any(part.casefold() in STEERING for part in entry[0].split("/")), entry[0]))
        return Manifest(entries=tuple(entries), blocked=blocked, dropped=dropped, contents=contents)

    def _review(self, meta: dict, manifest: Manifest) -> MachinePlaceReview:
        run_id = UUID(meta["run_id"]) if meta.get("run_id") else None
        run = reg.get_run(str(run_id)) if run_id and meta["origin"] == "agent" else None
        # Its agent's process, not what its record says: nothing is accepted while it can still write.
        state = "blocked" if manifest.blocked else "working" if run is not None and run.process_running() else "ready"
        note = f"{manifest.dropped} change{'s' if manifest.dropped != 1 else ''} inside .git {'are' if manifest.dropped != 1 else 'is'} never applied" if manifest.dropped else ""
        files = tuple(MachineReviewFile(path=key, change=change, size=(self.root / meta["id"] / "tree" / key).stat().st_size if new else None)
                      for key, change, new, _ in manifest.entries[:self.LISTED])
        return MachinePlaceReview(id=UUID(meta["id"]), place=meta["place"], origin=meta["origin"], run_id=run_id, created_at=datetime.fromisoformat(meta["created_at"]),
                                  state=state, reason="; ".join(filter(None, (manifest.blocked, note))), files=files, truncated=len(manifest.entries) > self.LISTED,
                                  digest=manifest.digest(UUID(meta["id"]), meta["place"]))

    def list(self) -> tuple[MachinePlaceReview, ...]:
        """Every review holding a change, oldest first (one that holds none is not listed)."""
        reviews = []
        for meta in self._metas():
            manifest = self.manifest(UUID(meta["id"]))
            if manifest.entries or manifest.blocked:
                reviews.append(self._review(meta, manifest))
        return tuple(reviews)

    def read(self, review_id: UUID, folder: Path, limit: int | None = DIFF_LINES) -> tuple[MachinePlaceReview, tuple[str, ...]]:
        """The review and its unified diff against what was staged (binary files named only), cut
        after `limit` lines (None: whole, as the PC shows it before an accept)."""
        meta, manifest = self._meta(review_id), self.manifest(review_id)
        tree, lines = self.root / str(review_id) / "tree", []
        for key, change, _, _ in manifest.entries:
            after = (tree / key).read_bytes() if change != "deleted" else b""
            before = (folder / key).read_bytes() if change != "added" and (folder / key).is_file() else b""
            try:
                lines += difflib.unified_diff(before.decode().splitlines(), after.decode().splitlines(), f"a/{key}", f"b/{key}", lineterm="")
            except UnicodeDecodeError:
                lines.append(f"Binary file {key} {change}")
            if limit is not None and len(lines) >= limit:
                lines = [*lines[:limit], "… diff cut here: read it whole on the PC (interact machine review)"]
                break
        return self._review(meta, manifest), tuple(line[:2000] for line in lines)

    def accept(self, review_id: UUID, digest: str, folder: Path) -> MachinePlaceReview:
        """Apply exactly the diff whose digest the owner read, or nothing (see module docstring)."""
        # Bytes read once: what is written below is what this digest covers, whatever the copy
        # holds by then (its agent may still reach it).
        meta, manifest = self._meta(review_id), self.manifest(review_id, keep=True)
        review = self._review(meta, manifest)
        if review.state != "ready":
            raise PermissionError(f"this review cannot be applied now ({review.state}{': ' + review.reason if review.reason else ''})")
        if digest != review.digest:
            raise PermissionError("the digest differs from this review's: read it again (interact machine reviews) before accepting")
        stale = [key for key, change, new, old in manifest.entries if _live(folder, key) not in ({old} if change != "added" else {None, new})]
        if stale:
            raise PermissionError(f"{', '.join(stale[:5])} changed since the review was made: nothing applied; discard it and ask again")
        for key, change, _, _ in manifest.entries:
            parts = key.split("/")
            with PinnedDirectory.open(folder, *parts[:-1], create=change != "deleted") as pinned:
                if change == "deleted":
                    pinned.unlink(parts[-1])
                else:
                    write_plain(pinned, parts[-1], manifest.contents[key])
        shutil.rmtree(self.root / str(review_id), ignore_errors=True)
        return review

    def discard(self, review_id: UUID) -> MachinePlaceReview | None:
        meta = self._meta(review_id)
        review = self._review(meta, self.manifest(review_id))
        shutil.rmtree(self.root / str(review_id), ignore_errors=True)
        return review
