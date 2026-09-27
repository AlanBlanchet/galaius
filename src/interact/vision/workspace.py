"""Private user-owned request workspace shared by every media transport."""

import os
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from interact.config import Config
from interact.vision.types import MediaItem

_STALE_SECONDS = 24 * 60 * 60


def _private_directory(parent: Path, name: str) -> Path:
    directory = parent / name
    try:
        info = directory.lstat()
    except FileNotFoundError:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"private media directory {name!r} is a symlink or not a directory")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise RuntimeError(f"private media directory {name!r} is owned by another user")
    if directory.resolve(strict=True).parent != parent.resolve(strict=True):
        raise RuntimeError(f"private media directory {name!r} escapes its workspace root")
    directory.chmod(0o700)
    return directory


def _workspace_root(config: Config) -> Path:
    root = config.media_workspace_root().expanduser().resolve(strict=False)
    global_temp = Path(tempfile.gettempdir()).resolve(strict=False)
    if root == global_temp or global_temp in root.parents or root == Path(root.anchor):
        raise RuntimeError(
            "media requires a safe user-owned directory outside the global temporary directory"
        )
    existing = root
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if hasattr(os, "geteuid") and existing.stat().st_uid != os.geteuid():
        raise RuntimeError("media workspace parent is owned by another user")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    root.chmod(0o700)
    return root


def _prune(root: Path) -> None:
    cutoff = time.time() - _STALE_SECONDS
    for candidate in root.iterdir():
        if not candidate.name.startswith("job-"):
            continue
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if info.st_mtime >= cutoff or (hasattr(os, "geteuid") and info.st_uid != os.geteuid()):
            continue
        if stat.S_ISLNK(info.st_mode):
            candidate.unlink(missing_ok=True)
        elif stat.S_ISDIR(info.st_mode):
            try:
                shutil.rmtree(candidate)
            except FileNotFoundError:
                continue


@dataclass(frozen=True)
class _MediaWorkspace:
    """One sensitive request directory; paths and cleanup have exactly one owner."""

    path: Path

    @classmethod
    def create(cls, config: Config) -> "_MediaWorkspace":
        jobs = _private_directory(_workspace_root(config), "media-jobs")
        _prune(jobs)
        path = jobs / f"job-{uuid.uuid4().hex}"
        path.mkdir(mode=0o700)
        return cls(path)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            shutil.rmtree(self.path)
        except OSError as cleanup_error:
            if exc is None:
                raise RuntimeError("sensitive media workspace cleanup failed") from cleanup_error

    def stage(self, item: MediaItem, name: str) -> Path:
        return item.stage(self.path / name)
