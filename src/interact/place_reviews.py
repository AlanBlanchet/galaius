"""Write after review: writes into a `write_on_review` folder land in a staging copy held by the
runner, never in the folder; the owner reads the diff and accepts it ON THE PC by its digest; the
PC then applies exactly that diff (M9).

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

    def stage_file(self, place: str, folder: Path, parts: tuple[str, ...], content: bytes, *, origin: Origin, run_id: UUID | None) -> UUID:
        """One file a workflow step writes into `folder` at `parts`, held in this run's review."""
        found = next((meta for meta in self._metas() if meta["place"] == place and meta["mode"] == "files" and meta["run_id"] == (str(run_id) if run_id else None)), None)
        review_id, staging = (UUID(found["id"]), self.root / found["id"]) if found else self._open(place, origin, run_id, "files")
        base = json.loads((staging / "base.json").read_text()) if (staging / "base.json").exists() else {}
        key = "/".join(parts)
        base.setdefault(key, _live(folder, key))
        target = staging / "tree" / key
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(content)
        (staging / "base.json").write_text(json.dumps(base))
        return review_id

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

    def manifest(self, review_id: UUID) -> Manifest:
        staging = self.root / str(review_id)
        meta, tree = self._meta(review_id), staging / "tree"
        base: dict[str, str | None] = json.loads((staging / "base.json").read_text()) if (staging / "base.json").exists() else {}
        entries, blocked, seen = [], "", set()
        for current, directories, names in os.walk(tree, followlinks=False):
            relative = Path(current).relative_to(tree)
            directories[:] = [name for name in directories if name.casefold() != ".git"]
            for name in (*names, *(name for name in directories if (Path(current) / name).is_symlink())):
                key, facts = (relative / name).as_posix(), (Path(current) / name).lstat()
                seen.add(key)
                if stat.S_ISLNK(facts.st_mode):
                    if base.get(key) != "link:" + os.readlink(Path(current) / name):
                        blocked = blocked or f"{key} is a link: links are never applied"
                    continue
                if not stat.S_ISREG(facts.st_mode):
                    blocked = blocked or f"{key} is not a plain file"
                    continue
                if NEVER_GRANTABLE.refusal(tuple(part for part in key.split("/") if not part.startswith(".")), None) is not None:
                    blocked = blocked or f"{key}: credential stores are never written"
                new, old = _digest(Path(current) / name), base.get(key)
                if old is None:
                    entries.append((key, "added", new, None))
                elif new != old:
                    entries.append((key, "changed", new, old))
        if meta["mode"] == "copy":
            entries += [(key, "deleted", None, old) for key, old in base.items()
                        if key not in seen and old is not None and not old.startswith("link:") and ".git" not in key.casefold().split("/")]
        return Manifest(entries=tuple(sorted(entries)), blocked=blocked)

    def _review(self, meta: dict, manifest: Manifest) -> MachinePlaceReview:
        run_id = UUID(meta["run_id"]) if meta.get("run_id") else None
        run = reg.get_run(str(run_id)) if run_id and meta["origin"] == "agent" else None
        state = "blocked" if manifest.blocked else "working" if run is not None and run.status in {"running", "waiting"} else "ready"
        files = tuple(MachineReviewFile(path=key, change=change, size=(self.root / meta["id"] / "tree" / key).stat().st_size if new else None)
                      for key, change, new, _ in manifest.entries[:self.LISTED])
        return MachinePlaceReview(id=UUID(meta["id"]), place=meta["place"], origin=meta["origin"], run_id=run_id, created_at=datetime.fromisoformat(meta["created_at"]),
                                  state=state, reason=manifest.blocked, files=files, truncated=len(manifest.entries) > self.LISTED,
                                  digest=manifest.digest(UUID(meta["id"]), meta["place"]))

    def list(self) -> tuple[MachinePlaceReview, ...]:
        """Every review holding a change, oldest first (one that holds none is not listed)."""
        reviews = []
        for meta in self._metas():
            manifest = self.manifest(UUID(meta["id"]))
            if manifest.entries or manifest.blocked:
                reviews.append(self._review(meta, manifest))
        return tuple(reviews)

    def read(self, review_id: UUID, folder: Path) -> tuple[MachinePlaceReview, tuple[str, ...]]:
        """The review and its unified diff against what was staged (binary files named only)."""
        meta, manifest = self._meta(review_id), self.manifest(review_id)
        tree, lines = self.root / str(review_id) / "tree", []
        for key, change, _, _ in manifest.entries:
            after = (tree / key).read_bytes() if change != "deleted" else b""
            before = (folder / key).read_bytes() if change != "added" and (folder / key).is_file() else b""
            try:
                lines += difflib.unified_diff(before.decode().splitlines(), after.decode().splitlines(), f"a/{key}", f"b/{key}", lineterm="")
            except UnicodeDecodeError:
                lines.append(f"Binary file {key} {change}")
            if len(lines) >= self.DIFF_LINES:
                lines = [*lines[:self.DIFF_LINES], "… diff cut here"]
                break
        return self._review(meta, manifest), tuple(line[:2000] for line in lines)

    def accept(self, review_id: UUID, digest: str, folder: Path) -> MachinePlaceReview:
        """Apply exactly the diff whose digest the owner read, or nothing (see module docstring)."""
        meta, manifest = self._meta(review_id), self.manifest(review_id)
        review = self._review(meta, manifest)
        if review.state != "ready":
            raise PermissionError(f"this review cannot be applied now ({review.state}{': ' + review.reason if review.reason else ''})")
        if digest != review.digest:
            raise PermissionError("the digest differs from this review's: read it again (interact machine reviews) before accepting")
        stale = [key for key, change, new, old in manifest.entries if _live(folder, key) not in ({old} if change != "added" else {None, new})]
        if stale:
            raise PermissionError(f"{', '.join(stale[:5])} changed since the review was made: nothing applied; discard it and ask again")
        tree = self.root / str(review_id) / "tree"
        for key, change, _, _ in manifest.entries:
            parts = key.split("/")
            with PinnedDirectory.open(folder, *parts[:-1], create=change != "deleted") as pinned:
                if change == "deleted":
                    pinned.unlink(parts[-1])
                else:
                    write_plain(pinned, parts[-1], (tree / key).read_bytes())
        shutil.rmtree(self.root / str(review_id), ignore_errors=True)
        return review

    def discard(self, review_id: UUID) -> MachinePlaceReview | None:
        meta = self._meta(review_id)
        review = self._review(meta, self.manifest(review_id))
        shutil.rmtree(self.root / str(review_id), ignore_errors=True)
        return review
