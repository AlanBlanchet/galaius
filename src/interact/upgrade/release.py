"""A client release as its publisher signed it: what it is, where it sits in time.

A release is ONE signed document (`release.json`, its ed25519 signature beside it as
`release.json.sig`) naming every file an install needs by sha256: the `interact` and
`interact-core` wheels and the hashed dependency lock. Order between two releases is
(version, released_at) as signed; `released_at` is the commit time of the client source, so a
rebuilt older commit never reads as newer. Verification and fetching live in `source` (they pull
httpx and cryptography, which a supervisor never needs)."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from importlib.metadata import PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path
from typing import ClassVar, Literal
from zipfile import ZipFile

from pydantic import BaseModel, ConfigDict, Field, field_validator

from interact.versioning import parse


class ReleaseRefused(Exception):
    """A release (or a file of it) that must not be installed; the words say why."""


class ReleaseFile(BaseModel):
    """One file of a release: a bare file name (never a path) and the sha256 of its bytes."""

    model_config = ConfigDict(frozen=True)
    name: str
    version: str = ""
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

    @classmethod
    def of(cls, wheel: Path) -> "ReleaseFile":
        with ZipFile(wheel) as archive:
            records = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(records) != 1:
                raise ReleaseRefused(f"{wheel.name} holds {len(records)} package metadata records, not one")
            metadata = BytesParser().parsebytes(archive.read(records[0]))
        return cls(name=metadata["Name"], version=metadata["Version"], sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(), file=wheel.name)

    def check(self, content: bytes) -> bytes:
        if hashlib.sha256(content).hexdigest() != self.sha256:
            raise ReleaseRefused(f"{self.file} is not the file the release signed (sha256 differs)")
        return content


class ReleaseOrder(BaseModel):
    """Where a release sits in time: semver first, then the signed commit time."""

    model_config = ConfigDict(frozen=True)
    version: str
    released_at: datetime

    @field_validator("released_at")
    @classmethod
    def _utc(cls, moment: datetime) -> datetime:
        if moment.tzinfo is None:
            raise ValueError("a release time carries its timezone")
        return moment.astimezone(UTC)

    @property
    def key(self) -> tuple[tuple[int, int, int], datetime]:
        return parse(self.version), self.released_at

    def __lt__(self, other: "ReleaseOrder") -> bool:
        return self.key < other.key

    def __le__(self, other: "ReleaseOrder") -> bool:
        return self.key <= other.key

    def label(self) -> str:
        return f"{self.version} ({self.released_at:%Y-%m-%d %H:%M} UTC)"


class BuildIdentity(ReleaseOrder):
    """One build: its order and the commit it was built from. An installed wheel carries its own
    (`interact/data/build.json`, written by the publisher before building); a plain checkout none."""

    commit: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    path: ClassVar[str] = "build.json"

    @classmethod
    def installed(cls) -> "BuildIdentity | None":
        entry = files("interact") / "data" / cls.path
        return cls.model_validate_json(entry.read_bytes()) if entry.is_file() else None

    @staticmethod
    def direct_url() -> str:
        """How this install was made (PEP 610 `direct_url.json`; empty from an index or source tree)."""
        try:
            return distribution("interact").read_text("direct_url.json") or ""
        except PackageNotFoundError:
            return ""

    @classmethod
    def installed_commit(cls) -> str | None:
        """The commit this install was built from: its stamped build, else the GitHub archive or
        git commit an installer installed it from (the public install scripts)."""
        if (build := cls.installed()) is not None:
            return build.commit
        try:
            made = json.loads(cls.direct_url() or "{}")
        except ValueError:
            return None
        if commit := made.get("vcs_info", {}).get("commit_id"):
            return commit
        archive = re.search(r"/archive/([0-9a-f]{40})\.(?:zip|tar\.gz)$", made.get("url", ""))
        return archive.group(1) if archive else None

    def label(self) -> str:
        return f"{self.version} {self.commit[:7]} ({self.released_at:%Y-%m-%d %H:%M} UTC)"

    @classmethod
    def of(cls, release: "BuildIdentity") -> "BuildIdentity":
        return cls(version=release.version, released_at=release.released_at, commit=release.commit)


class Release(BuildIdentity):
    """The signed document: one build and every file installing it takes. `wheels` holds exactly
    the `interact` and `interact-core` wheels."""

    schema_version: Literal[1] = 1
    expires_at: datetime
    wheels: tuple[ReleaseFile, ...]
    lock: ReleaseFile
    packages: ClassVar[tuple[str, ...]] = ("interact", "interact-core")

    @field_validator("wheels")
    @classmethod
    def _both_packages(cls, wheels: tuple[ReleaseFile, ...]) -> tuple[ReleaseFile, ...]:
        if sorted(wheel.name for wheel in wheels) != sorted(cls.packages):
            raise ValueError(f"a release carries exactly the wheels of {', '.join(cls.packages)}")
        return wheels

    @classmethod
    def described(cls, wheels: tuple[Path, ...], lock: Path, commit: str, released_at: datetime, lifetime: timedelta) -> "Release":
        """The document for these built files (names and versions read from each wheel)."""
        described = tuple(ReleaseFile.of(path) for path in wheels)
        version = next(wheel.version for wheel in described if wheel.name == "interact")
        return cls(version=version, released_at=released_at, commit=commit, expires_at=released_at + lifetime, wheels=described,
                   lock=ReleaseFile(name="requirements.lock", sha256=hashlib.sha256(lock.read_bytes()).hexdigest(), file=lock.name))

    @property
    def order(self) -> ReleaseOrder:
        return ReleaseOrder(version=self.version, released_at=self.released_at)

    def document(self) -> bytes:
        """The exact bytes that are signed and served as `release.json`."""
        return (self.model_dump_json(indent=2) + "\n").encode()

    @property
    def identity(self) -> str:
        """What a failed build is remembered by: its interact wheel's sha256."""
        return self.wheel("interact").sha256

    @property
    def files(self) -> tuple[ReleaseFile, ...]:
        return (*self.wheels, self.lock)

    def wheel(self, name: str) -> ReleaseFile:
        return next(wheel for wheel in self.wheels if wheel.name == name)

    def expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at
