"""Build the self-contained program of a client release, one archive per system it runs on.

    bundle.py --release <folder> --out <folder> [--target NAME ...]

`--release` holds a release as `release.py` writes it (release.json, its lock and both wheels). Each
archive is a standalone CPython (python-build-standalone, its bytes pinned below) with every locked
dependency and both wheels installed for that system, compiled, and `galaius` launchers that name the
interpreter beside them: a computer unpacks it and runs it, nothing is resolved, downloaded or built
there. `bundles.json` lists every archive with its sha256 and the release.json it was built from.
Every system-specific value lives in `TARGETS`; nothing below it names a system.
"""

import argparse
import compileall
import gzip
import hashlib
import io
import json
import os
import platform
import py_compile
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from importlib.metadata import PathDistribution
from pathlib import Path, PurePosixPath
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from galaius.upgrade.release import Release


class Interpreter(BaseModel):
    """One python-build-standalone release: every target's CPython comes from it."""

    model_config = ConfigDict(frozen=True)
    version: str = "3.13.14"
    build: str = "20260623"

    @property
    def minor(self) -> tuple[int, int]:
        major, minor, _ = self.version.split(".")
        return int(major), int(minor)

    def asset(self, triple: str) -> str:
        return f"cpython-{self.version}+{self.build}-{triple}-install_only_stripped.tar.gz"

    def url(self, triple: str) -> str:
        return f"https://github.com/astral-sh/python-build-standalone/releases/download/{self.build}/{self.asset(triple).replace('+', '%2B')}"


class Layout(BaseModel):
    """How one family of systems lays out an interpreter folder, and how a launcher there starts it."""

    model_config = ConfigDict(frozen=True)
    #: `site-packages`, relative to the folder; `{minor}` is the interpreter's `X.Y`.
    site: str
    #: Where `sysconfig` puts the interpreter's programs (galaius's `Uv.find`, its sign-in helper look there).
    scripts: str
    suffix: str
    #: The launcher format: a POSIX shell script, or distlib's Windows console launcher.
    launcher: Literal["posix", "windows"]

    def program(self, name: str) -> str:
        return f"{name}{self.suffix}"


POSIX = Layout(site="lib/python{minor}/site-packages", scripts="bin", suffix="", launcher="posix")
WINDOWS = Layout(site="Lib/site-packages", scripts="Scripts", suffix=".exe", launcher="windows")


class Target(BaseModel):
    """One system the program runs on: where its CPython comes from, which wheels fit it, its layout."""

    model_config = ConfigDict(frozen=True)
    name: str
    #: python-build-standalone's name for the system.
    triple: str
    #: uv's `--python-platform`: the wheels installed are those this system loads.
    wheels: str
    #: sha256 of `Interpreter.asset(triple)`, from the release's SHA256SUMS.
    interpreter_sha256: str
    #: `<sys.platform>-<platform.machine()>` of a computer that runs it: there the built program is started once
    #: before it is packed.
    native: str
    layout: Layout = POSIX

    @property
    def archive(self) -> str:
        return f"galaius-{self.name}.tar.gz"

    def site_packages(self, root: Path, interpreter: Interpreter) -> Path:
        return root / self.layout.site.format(minor=".".join(map(str, interpreter.minor)))

    def scripts(self, root: Path) -> Path:
        return root / self.layout.scripts


INTERPRETER = Interpreter()
TARGETS: tuple[Target, ...] = (
    Target(name="linux-x86_64", triple="x86_64-unknown-linux-gnu", wheels="x86_64-manylinux_2_28", native="linux-x86_64",
           interpreter_sha256="459ed79967acc207bef2ff5124dac35d74d5108528e37b15395d14e2922f2c92"),
    Target(name="windows-x86_64", triple="x86_64-pc-windows-msvc", wheels="x86_64-pc-windows-msvc", native="win32-AMD64", layout=WINDOWS,
           interpreter_sha256="7c3159205dda0289a47e6dd1226c4e5800b1f99a03146cdc25a41fed8ef74f5a"),
    Target(name="macos-arm64", triple="aarch64-apple-darwin", wheels="aarch64-apple-darwin", native="darwin-arm64",
           interpreter_sha256="795a5aeeb050f00aa8a2214d779bad9f1b9113edb6923317a80c042a11a087d7"),
)


class Launcher(BaseModel):
    """A console script of galaius (its `entry_points.txt`) as a program one folder below the interpreter
    folder, which it names relative to itself: the folder can sit anywhere, under any name. Every one goes in
    the scripts folder; the person's own commands (`on_path`) also go in `commands/`, the one folder the
    installers put on PATH (the scripts folder also holds uv, which is no command of his)."""

    model_config = ConfigDict(frozen=True)
    name: str
    module: str
    function: str

    on_path: ClassVar[frozenset[str]] = frozenset({"galaius"})
    commands: ClassVar[str] = "commands"
    #: POSIX: follows a link to itself back to its folder, then to the interpreter beside it.
    posix: ClassVar[str] = ('#!/bin/sh\nhere="$(dirname "$(readlink -f "$0")")"\n'
                            'exec "$here/../bin/python3" -I -c \'import sys; sys.argv[0] = "{name}"; from {module} import {function}; sys.exit({function}())\' "$@"\n')
    #: Windows: distlib's console launcher, then its shebang (`<launcher_dir>` = the launcher's folder), then a
    #: zip whose `__main__.py` the interpreter runs, isolated like every runtime command.
    shebang: ClassVar[bytes] = b'#!"<launcher_dir>\\..\\python.exe" -I\n'

    def content(self, layout: Layout, windows_launcher: bytes | None) -> bytes:
        if layout.launcher == "posix":
            return self.posix.format(name=self.name, module=self.module, function=self.function).encode()
        main = f"import sys\nfrom {self.module} import {self.function}\nif __name__ == '__main__':\n    sys.exit({self.function}())\n"
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("__main__.py", main)
        return windows_launcher + self.shebang + payload.getvalue()

    def write(self, target: Target, root: Path, windows_launcher: bytes | None) -> None:
        content = self.content(target.layout, windows_launcher)
        for folder in (target.scripts(root), *([root / self.commands] if self.name in self.on_path else [])):
            path = folder / target.layout.program(self.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o755)

    @classmethod
    def of(cls, site: Path) -> tuple["Launcher", ...]:
        (info,) = site.glob("galaius-*.dist-info")
        found = []
        for entry in PathDistribution(info).entry_points.select(group="console_scripts"):
            module, function = entry.value.split(":")
            found.append(cls(name=entry.name, module=module, function=function))
        return tuple(found)


class Bundler(BaseModel):
    """Every archive of one release."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    release_dir: Path
    out: Path
    interpreter: Interpreter = INTERPRETER
    uv: str = shutil.which("uv") or "uv"

    def release(self) -> Release:
        """release.json, with every file it names present and intact."""
        release = Release.model_validate_json((self.release_dir / "release.json").read_bytes())
        for item in release.files:
            if hashlib.sha256((self.release_dir / item.file).read_bytes()).hexdigest() != item.sha256:
                raise SystemExit(f"{item.file} differs from release.json")
        return release

    def fetched(self, target: Target, cache: Path) -> Path:
        path = cache / self.interpreter.asset(target.triple)
        if not path.is_file():
            with urllib.request.urlopen(self.interpreter.url(target.triple), timeout=120) as answer, path.open("wb") as file:
                shutil.copyfileobj(answer, file)
        if hashlib.sha256(path.read_bytes()).hexdigest() != target.interpreter_sha256:
            path.unlink()
            raise SystemExit(f"{path.name} is not the pinned CPython (sha256 differs)")
        return path

    def build(self, target: Target, release: Release, cache: Path) -> Path:
        work = Path(tempfile.mkdtemp(prefix=f"bundle-{target.name}-", dir=self.out))
        try:
            with tarfile.open(self.fetched(target, cache)) as archive:
                archive.extractall(work, filter="data")
            root = work / "python"
            site = target.site_packages(root, self.interpreter)
            into = ["--no-config", "--quiet", "--python-platform", target.wheels, "--python-version", ".".join(map(str, self.interpreter.minor)),
                    "--target", str(site), "--no-deps"]
            subprocess.run([self.uv, "pip", "install", *into, "--require-hashes", "-r", str(self.release_dir / release.lock.file)], check=True)
            subprocess.run([self.uv, "pip", "install", *into, *(str(self.release_dir / wheel.file) for wheel in release.wheels)], check=True)
            # `--target` puts programs in <site>/bin (<site>/Scripts when building on Windows), scripts naming
            # THIS builder's interpreter: only uv's own binary is kept, where this interpreter's programs go.
            programs = [folder for folder in (site / "bin", site / "Scripts") if folder.is_dir()]
            (uv,) = [found for folder in programs if (found := folder / target.layout.program("uv")).is_file()]
            target.scripts(root).mkdir(exist_ok=True)
            uv.replace(target.scripts(root) / uv.name)
            for folder in programs:
                shutil.rmtree(folder)
            windows_launcher = (site / "pip" / "_vendor" / "distlib" / "t64.exe").read_bytes() if target.layout.launcher == "windows" else None
            self.pruned(target, root, site)
            for launcher in Launcher.of(site):
                launcher.write(target, root, windows_launcher)
            (target.scripts(root) / ".empty").unlink(missing_ok=True)
            # The folder is never edited once unpacked: bytecode is checked by nothing but its presence.
            compileall.compile_dir(root, quiet=2, workers=0, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
            if target.native == f"{sys.platform}-{platform.machine()}":
                self.started(target, root, release, work / "home")
            return self.packed(root, self.out / target.archive)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    #: Every variable naming where a galaius keeps its data, settings and runtimes.
    homes: ClassVar[tuple[str, ...]] = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                                        "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_BIN_HOME", "GALAIUS_RUNTIMES")

    @classmethod
    def started(cls, target: Target, root: Path, release: Release, home: Path) -> None:
        """The built program started once, as a person's computer starts it (its own command, no variable of
        this builder's) but in a home of its own: a newer build started against this computer's own data would
        become ITS active runtime (a scratch build once replaced the running service of the computer building it)."""
        command = root / Launcher.commands / target.layout.program("galaius")
        clean = {name: value for name, value in os.environ.items() if not name.startswith(("PYTHON", "VIRTUAL_ENV", "UV_", "GALAIUS_"))}
        clean |= {name: str(home / name.lower()) for name in cls.homes}
        said = subprocess.run([str(command), "--version"], env=clean, capture_output=True, text=True, timeout=120, check=False)
        if said.returncode != 0 or said.stdout.strip() != release.version:
            raise SystemExit(f"the built program does not start: {command} --version said {(said.stdout + said.stderr).strip()[-500:]!r}")

    #: What nothing in the program runs: build headers and manuals, the interpreter's own installers and
    #: editor, pip (uv installs upgrades), and the test suites packages ship.
    unused_folders: ClassVar[tuple[str, ...]] = ("include", "share")
    unused_stdlib: ClassVar[tuple[str, ...]] = ("idlelib", "turtledemo", "ensurepip")
    unused_programs: ClassVar[tuple[str, ...]] = ("idle*", "pip*", "pydoc*", "*-config")

    def pruned(self, target: Target, root: Path, site: Path) -> None:
        stdlib = site.parent
        doomed = [root / name for name in self.unused_folders] + [stdlib / name for name in self.unused_stdlib] + [site / "pip"]
        doomed += [found for pattern in self.unused_programs for found in target.scripts(root).glob(pattern)]
        doomed += [found for pattern in ("*/**/tests", "*/**/test") for found in site.glob(pattern) if found.is_dir()]
        doomed += list(site.glob("pip-*.dist-info"))
        for path in doomed:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

    @staticmethod
    def packed(root: Path, path: Path) -> Path:
        """The folder's content at the archive's top, in one order, owned by nobody and timeless: the same
        release builds the same bytes."""
        with path.open("wb") as file, gzip.GzipFile(fileobj=file, mode="wb", compresslevel=6, mtime=0) as packed, \
                tarfile.open(fileobj=packed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for member in sorted(root.rglob("*")):
                info = archive.gettarinfo(member, arcname=str(PurePosixPath(*member.relative_to(root).parts)))
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                if info.isreg():
                    with member.open("rb") as content:
                        archive.addfile(info, content)
                else:
                    archive.addfile(info)
        return path

    def run(self, targets: tuple[Target, ...]) -> dict:
        if sys.version_info[:2] != self.interpreter.minor:
            raise SystemExit(f"run this with Python {'.'.join(map(str, self.interpreter.minor))}: its bytecode is what every archive carries")
        release = self.release()
        self.out.mkdir(parents=True, exist_ok=True)
        cache = self.out / ".cache"
        cache.mkdir(exist_ok=True)
        listed = []
        for target in targets:
            path = self.build(target, release, cache)
            listed.append({"target": target.name, "file": path.name, "size": path.stat().st_size,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        shutil.rmtree(cache)
        document = {"release_sha256": hashlib.sha256((self.release_dir / "release.json").read_bytes()).hexdigest(),
                    "version": release.version, "commit": release.commit, "bundles": listed}
        (self.out / "bundles.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--target", action="append", choices=[target.name for target in TARGETS])
    arguments = parser.parse_args()
    chosen = tuple(target for target in TARGETS if arguments.target is None or target.name in arguments.target)
    print(json.dumps(Bundler(release_dir=arguments.release, out=arguments.out).run(chosen), indent=2))


if __name__ == "__main__":
    main()
