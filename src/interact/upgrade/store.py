"""Runtimes installed side by side, and the one pointer every long-lived process follows.

`<data>/interact/runtimes/` (Linux `~/.local/share`, Windows `%LOCALAPPDATA%`, macOS
`~/Library/Application Support`; `INTERACT_RUNTIMES` overrides) holds one virtual environment per
installed release and `current.json`: the ACTIVE runtime, the PREVIOUS one (where a failed start
rolls back to), the FLOOR (the newest release ever activated: nothing older is installed without an
explicit local `interact upgrade use`) and the builds that FAILED to start (never tried again).
The pointer is replaced atomically under the store's file lock; the folder is private to this user.
A runtime the pointer names is complete: it is only named after its import probe passed."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from interact.file_lock import exclusive
from interact.private_files import PRIVATE_FILES
from interact.server_registry import _alive
from interact.upgrade.release import BuildIdentity, ReleaseOrder

#: A worker exits with this at its quiet point when the pointer names another runtime: "start the
#: active one in my place" (EX_TEMPFAIL, unused by anything else interact exits with).
EXIT_UPGRADE = 75


class Runtime(BaseModel):
    """One installed environment: `path` is its virtual-environment root."""

    model_config = ConfigDict(frozen=True)
    path: Path
    receipt_name: ClassVar[str] = "installation.json"

    @classmethod
    def own(cls) -> "Runtime":
        """The environment this process runs from (a bootstrap install before any upgrade)."""
        return cls(path=Path(sys.prefix))

    @property
    def python(self) -> Path:
        """Its interpreter; on Windows the windowless `pythonw` when this process has no console."""
        if sys.platform == "win32":
            name = "pythonw.exe" if Path(sys.executable).name.lower() == "pythonw.exe" else "python.exe"
            return self.path / "Scripts" / name
        return self.path / "bin" / "python"

    def command(self, arguments: tuple[str, ...]) -> list[str]:
        """`interact <arguments>` in this runtime, isolated (`-I`): the working directory, user site
        and PYTHON* variables never shadow the installed package."""
        return [str(self.python), "-I", "-m", "interact", *arguments]

    def usable(self) -> bool:
        return self.python.is_file()

    def receipt(self) -> "RuntimeReceipt | None":
        try:
            return RuntimeReceipt.model_validate_json((self.path / self.receipt_name).read_bytes())
        except (OSError, ValidationError):
            return None

    def order(self) -> ReleaseOrder | None:
        receipt = self.receipt()
        return receipt.build if receipt is not None else None

    def label(self) -> str:
        receipt = self.receipt()
        return receipt.build.label() if receipt is not None and receipt.build is not None else self.path.name


#: Where a runtime's wheels came from: the signed-in server, GitHub releases, a person's own build.
ReleaseOrigin = Literal["server", "github", "local"]
#: What an upgrade event says happened.
EventKind = Literal["checked", "installed", "activated", "refused", "rolled_back", "failed", "restarted"]


class RuntimeReceipt(BaseModel):
    """What was installed into a runtime and from where; `identity` is its interact wheel's sha256."""

    model_config = ConfigDict(frozen=True)
    build: BuildIdentity | None
    identity: str
    source: ReleaseOrigin
    installed_at: datetime
    packages: dict[str, str]


class Pointer(BaseModel):
    model_config = ConfigDict(frozen=True)
    active: Path | None = None
    previous: Path | None = None
    floor: ReleaseOrder | None = None
    failed: tuple[str, ...] = ()
    generation: int = 0


class UpgradeEvent(BaseModel):
    """One line of `events.jsonl`: what the upgrade machinery did, in words a person reads."""

    model_config = ConfigDict(frozen=True)
    at: datetime
    kind: EventKind
    text: str


class LiveProcess(BaseModel):
    """A process using a runtime (`live/<pid>.json`): prune never removes what one still runs."""

    model_config = ConfigDict(frozen=True)
    pid: int
    runtime: Path
    started: float


class Uv(BaseModel):
    """The uv that installs runtimes, by absolute path: the installer's own pinned copy first, then
    one on an absolute PATH entry (never the working directory). Its environment carries no
    `UV_*` / `PIP_*` variable: an index, config or cache redirection never reaches an install."""

    model_config = ConfigDict(frozen=True)
    path: Path

    @classmethod
    def find(cls, data: Path) -> "Uv":
        name = "uv.exe" if sys.platform == "win32" else "uv"
        candidates = [data / "uv" / name, *(Path(entry) / name for entry in os.environ.get("PATH", "").split(os.pathsep) if entry and Path(entry).is_absolute())]
        found = next((candidate for candidate in candidates if candidate.is_file()), None)
        if found is None:
            raise FileNotFoundError("uv is needed to install an Interact runtime (the installer puts its own copy beside Interact's data)")
        return cls(path=found)

    @staticmethod
    def environment() -> dict[str, str]:
        return {name: value for name, value in os.environ.items() if not name.startswith(("UV_", "PIP_", "PYTHON", "VIRTUAL_ENV", "CONDA"))}

    def run(self, *arguments: str) -> None:
        completed = subprocess.run([str(self.path), *arguments], env=self.environment(), capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"uv {arguments[0]} failed: {(completed.stderr or completed.stdout).strip()[-2000:]}")


class RuntimeStore(BaseModel):
    model_config = ConfigDict(frozen=True)
    root: Path
    #: Runtimes kept besides the active and previous ones (for `interact upgrade use`).
    keep: ClassVar[int] = 2

    @staticmethod
    def data_home() -> Path:
        if sys.platform == "win32":
            return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "interact"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "interact"
        return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "interact"

    @classmethod
    def default(cls) -> "RuntimeStore":
        override = os.environ.get("INTERACT_RUNTIMES")
        return cls(root=Path(override) if override else cls.data_home() / "runtimes")

    @property
    def pointer_path(self) -> Path:
        return self.root / "current.json"

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def check_path(self) -> Path:
        return self.root / "check.json"

    @property
    def live_path(self) -> Path:
        return self.root / "live"

    @contextmanager
    def locked(self) -> Iterator[None]:
        """The store's writer lock (`store.lock`, never replaced): pointer, installs, prune."""
        PRIVATE_FILES.directory(self.root)
        with exclusive(os.open(self.root / "store.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)):
            yield

    # ---- the pointer -------------------------------------------------------------------------

    def pointer(self) -> Pointer:
        try:
            return Pointer.model_validate_json(PRIVATE_FILES.read_text(self.pointer_path))
        except FileNotFoundError:
            return Pointer()

    def active(self) -> Runtime:
        """The runtime to start: the pointer's, else (none yet, or gone) this process's own."""
        active = self.pointer().active
        runtime = Runtime(path=active) if active is not None else None
        return runtime if runtime is not None and runtime.usable() else Runtime.own()

    def previous(self) -> Runtime | None:
        previous = self.pointer().previous
        return Runtime(path=previous) if previous is not None and Runtime(path=previous).usable() else None

    def signature(self) -> tuple[int, int] | None:
        """Cheap change detector for the pointer (mtime, size): read it again when this moves."""
        try:
            info = self.pointer_path.stat()
        except FileNotFoundError:
            return None
        return info.st_mtime_ns, info.st_size

    def _update(self, change: Callable[[Pointer], Pointer]) -> Pointer:
        with self.locked():
            current = self.pointer()
            updated = change(current).model_copy(update={"generation": current.generation + 1})
            PRIVATE_FILES.write_text(self.pointer_path, updated.model_dump_json(indent=2) + "\n")
            if updated.active is not None:
                self.follow(Runtime(path=updated.active))
            return updated

    @staticmethod
    def launcher() -> Path | None:
        """Where new launches start (`~/.local/bin/interact`); None on Windows (uv's own entry)."""
        return None if sys.platform == "win32" else Path(os.environ.get("XDG_BIN_HOME") or Path.home() / ".local" / "bin") / "interact"

    def follow(self, runtime: Runtime) -> None:
        """Point the launcher at `runtime` when this store manages it (a link into the store): new
        launches then start on the active runtime too, and prune never strands it. Any other
        launcher (a bootstrap install's own) is left alone: its supervisor starts the active worker."""
        launcher = self.launcher()
        if launcher is None or not launcher.is_symlink() or not Path(os.readlink(launcher)).is_relative_to(self.root) \
                or not runtime.path.is_relative_to(self.root) or not (runtime.path / "bin" / "interact").is_file():
            return
        temporary = launcher.with_name(f".{launcher.name}-{os.getpid()}")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(runtime.path / "bin" / "interact")
        os.replace(temporary, launcher)

    def activate(self, runtime: Runtime, *, explicit: bool = False) -> Pointer:
        """Point every long-lived process at `runtime`. Older than the floor only when `explicit`
        (a person's local command); the floor never goes down."""
        order = runtime.order()

        def change(current: Pointer) -> Pointer:
            if not explicit and order is not None and current.floor is not None and order < current.floor:
                raise ValueError(f"{runtime.label()} is older than {current.floor.label()}, the newest release this computer ran")
            before = current.active or Runtime.own().path
            floor = order if order is not None and (current.floor is None or current.floor < order) else current.floor
            return current.model_copy(update={"active": runtime.path, "previous": before if before != runtime.path else current.previous, "floor": floor})

        pointer = self._update(change)
        self.record("activated", f"{runtime.label()} is now the active runtime" + (" (chosen locally)" if explicit else ""))
        return pointer

    def roll_back(self, suspect: Runtime, reason: str) -> Runtime:
        """`suspect` did not start: point back to the previous runtime (else this process's own).
        Its build is only CONDEMNED (`condemn`) once the runtime rolled back to starts: a failure
        both share is this computer's, not the release's."""
        target: list[Runtime] = []

        def change(current: Pointer) -> Pointer:
            previous = Runtime(path=current.previous) if current.previous is not None and current.previous != suspect.path else None
            back = previous if previous is not None and previous.usable() else Runtime.own()
            target.append(back)
            if current.active not in (None, suspect.path):
                return current  # someone already moved the pointer on: leave their choice
            return current.model_copy(update={"active": back.path, "previous": None})

        self._update(change)
        self.record("rolled_back", f"{suspect.label()} did not start ({reason}); back on {target[0].label()}")
        return target[0]

    def condemn(self, runtime: Runtime) -> None:
        """`runtime`'s build failed where the previous one works: never installed again here."""
        receipt = runtime.receipt()
        if receipt is None:
            return
        self._update(lambda current: current if receipt.identity in current.failed else current.model_copy(update={"failed": (*current.failed, receipt.identity)}))
        self.record("failed", f"{runtime.label()} failed to start where the previous runtime works: it will not be installed again")

    def failed(self, identity: str) -> bool:
        return identity in self.pointer().failed

    # ---- installing --------------------------------------------------------------------------

    def install(self, wheels: dict[str, Path], lock: Path | None, build: BuildIdentity | None, source: ReleaseOrigin, python: str | None = None) -> Runtime:
        """A new runtime holding `wheels` (`interact`, `interact-core`), not yet active.

        Built in a private staging folder (relocatable) and renamed into place only after its import
        probe passed: a runtime under its final name is always complete. With `lock` (a signed
        release) every dependency comes from it, each checked by hash, none resolved; without (a
        person's local build) uv resolves them as `uv tool install` does."""
        PRIVATE_FILES.directory(self.root)
        uv = Uv.find(self.data_home())
        staging = Runtime(path=Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root)))
        try:
            uv.run("venv", "--quiet", "--relocatable", "--allow-existing", "--python", python or f"{sys.version_info.major}.{sys.version_info.minor}", str(staging.path))
            requirements = staging.path / "requirements.txt"
            pinned = "".join(f"{name} @ {path.resolve().as_uri()} --hash=sha256:{self.digest(path)}\n" for name, path in sorted(wheels.items()))
            if lock is not None:
                requirements.write_text(lock.read_text(encoding="utf-8") + pinned, encoding="utf-8")
                uv.run("pip", "install", "--quiet", "--python", str(staging.python), "--no-config", "--require-hashes", "--no-deps", "-r", str(requirements))
            else:
                overrides = staging.path / "overrides.txt"
                overrides.write_text(f"interact-core @ {wheels['interact-core'].resolve().as_uri()}\n", encoding="utf-8")
                uv.run("pip", "install", "--quiet", "--python", str(staging.python), "--no-config", "--overrides", str(overrides), str(wheels["interact"]))
            packages = self.probe(staging)
            if build is not None and packages.get("interact") != build.version:
                raise RuntimeError(f"the runtime imports interact {packages.get('interact')}, the release says {build.version}")
            receipt = RuntimeReceipt(build=build, identity=self.digest(wheels["interact"]), source=source, installed_at=datetime.now(UTC), packages=packages)
            (staging.path / Runtime.receipt_name).write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
            runtime = Runtime(path=self.root / f"{build.version if build is not None else 'local'}-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}")
            with self.locked():
                staging.path.rename(runtime.path)
        except BaseException:
            shutil.rmtree(staging.path, ignore_errors=True)
            raise
        self.record("installed", f"installed {runtime.label()} from {source}")
        return runtime

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def probe(runtime: Runtime) -> dict[str, str]:
        """Both packages import inside `runtime`, from `runtime` (never a stray path)."""
        script = ("import json, interact, interact_core; from importlib.metadata import version; "
                  "print(json.dumps({'interact': version('interact'), 'interact-core': version('interact-core'), "
                  "'sources': [interact.__file__, interact_core.__file__]}))")
        output = subprocess.run([str(runtime.python), "-I", "-c", script], capture_output=True, text=True, check=False, env=Uv.environment())
        if output.returncode != 0:
            raise RuntimeError(f"the new runtime does not import: {output.stderr.strip()[-2000:]}")
        loaded = json.loads(output.stdout)
        root = runtime.path.resolve()
        if any(not Path(source).resolve().is_relative_to(root) for source in loaded.pop("sources")):
            raise RuntimeError("the new runtime imports interact from outside itself")
        return loaded

    # ---- processes using runtimes ------------------------------------------------------------

    def register(self, runtime: Runtime, pid: int) -> None:
        """Record that process `pid` runs `runtime` (ignored once that process is gone)."""
        PRIVATE_FILES.write_text(self.live_path / f"{pid}.json", LiveProcess(pid=pid, runtime=runtime.path, started=time.time()).model_dump_json())

    def in_use(self) -> set[Path]:
        used = set()
        for entry in self.live_path.glob("*.json") if self.live_path.is_dir() else ():
            try:
                live = LiveProcess.model_validate_json(entry.read_bytes())
            except (OSError, ValidationError):
                continue
            if _alive(live.pid):
                used.add(live.runtime)
            else:
                entry.unlink(missing_ok=True)
        return used

    def prune(self) -> list[Path]:
        """Remove installed runtimes nothing needs: never the active, previous or a live one, and
        the `keep` newest others stay. Only runtimes this store installed (a receipt) go; so do
        leftovers of an interrupted install or prune (`.staging-*` older than a day, `.trash-*`).
        A folder that cannot go now (in use on Windows) stays for the next prune."""
        removed = []
        with self.locked():
            pointer = self.pointer()
            protected = {pointer.active, pointer.previous, *self.in_use()}
            managed = sorted((path for path in self.root.iterdir() if path.is_dir() and not path.name.startswith(".") and Runtime(path=path).receipt() is not None), key=lambda path: path.stat().st_mtime, reverse=True)
            leftovers = [path for path in self.root.glob(".trash-*")] + [path for path in self.root.glob(".staging-*") if time.time() - path.stat().st_mtime > 86400]
            for path in [path for path in managed if path not in protected][self.keep:] + leftovers:
                try:
                    trash = path if path.name.startswith(".trash-") else path.rename(path.with_name(f".trash-{path.name}"))
                    shutil.rmtree(trash)
                except OSError:
                    continue
                removed.append(path)
        return removed

    # ---- what happened, and when to look again -----------------------------------------------

    def record(self, kind: EventKind, text: str) -> None:
        PRIVATE_FILES.directory(self.root)
        with open(self.events_path, "a", encoding="utf-8") as stream:
            stream.write(UpgradeEvent(at=datetime.now(UTC), kind=kind, text=text).model_dump_json() + "\n")

    def events(self, limit: int = 20) -> list[UpgradeEvent]:
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()[-limit:]
        except FileNotFoundError:
            return []
        return [UpgradeEvent.model_validate_json(line) for line in lines if line.strip()]

    def next_check(self) -> float:
        try:
            return float(json.loads(self.check_path.read_text(encoding="utf-8"))["next_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return 0.0

    def schedule(self, seconds: float) -> None:
        PRIVATE_FILES.write_text(self.check_path, json.dumps({"next_at": time.time() + seconds}) + "\n")

    def request_check(self, min_gap: float = 60.0) -> bool:
        """Ask for a release check now (a server just restarted), at most once per `min_gap`."""
        now = time.time()
        try:
            last = self.check_path.stat().st_mtime
        except FileNotFoundError:
            last = 0.0
        if now - last < min_gap:
            return False
        self.schedule(0)
        return True
