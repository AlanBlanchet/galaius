"""The capabilities this PC installs on its owner's press (`galaius_core.MACHINE_TOOLS`, `AgentToolInstallRequest`): each
its pinned package in a venv of its own under `~/.galaius/tools/<name>/` (never the user's site-packages), installed by
`uv` in the background; the programs it calls are found on PATH or reported missing, never installed. What a capability's
agent tools run (`MachineTools.program`): nothing until it is ready, then that venv's command."""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import ClassVar

from galaius_core import MACHINE_TOOLS, MachineToolName, MachineToolState

from galaius.file_lock import FILE_LOCK

logger = logging.getLogger(__name__)


class ToolNotReady(PermissionError):
    """A capability's tool asked before its owner installed it there (or while PATH lacks a program it calls)."""


class MachineTools:
    """Every capability's folder: `venv/` (its package), `installed.json` (the version a finished install left),
    `failed.json` (why the last one failed), `install.lock` (held while one runs, by whichever process runs it)."""

    root: ClassVar[Path] = Path.home() / ".galaius" / "tools"
    #: Longest one install may take (a package and its wheels on a slow line).
    INSTALL_SECONDS: ClassVar[float] = 1200

    @classmethod
    def folder(cls, name: MachineToolName) -> Path:
        return cls.root / name

    @classmethod
    def venv_bin(cls, name: MachineToolName) -> Path:
        return cls.folder(name) / "venv" / ("Scripts" if sys.platform == "win32" else "bin")

    @classmethod
    def states(cls) -> tuple[MachineToolState, ...]:
        return tuple(cls.state(name) for name in MACHINE_TOOLS)

    @classmethod
    def state(cls, name: MachineToolName) -> MachineToolState:
        spec, folder = MACHINE_TOOLS[name], cls.folder(name)
        missing = tuple(program for program in spec.programs if shutil.which(program) is None)
        if cls._installing(name):
            return MachineToolState(name=name, phase="installing", missing=missing)
        installed = cls._read(folder / "installed.json").get("version")
        if installed == spec.version and (cls.venv_bin(name) / _command(spec.package)).is_file():
            return MachineToolState(name=name, phase="ready", version=installed, missing=missing)
        failed = cls._read(folder / "failed.json").get("detail")
        return MachineToolState(name=name, phase="failed" if failed else "absent", version=installed, missing=missing, detail=str(failed or "")[:400])

    @classmethod
    def install(cls, name: MachineToolName) -> tuple[MachineToolState, ...]:
        """Start `name`'s install unless it is under way or ready; every capability's state."""
        if cls.state(name).phase not in ("installing", "ready"):
            started = threading.Event()
            threading.Thread(target=cls._job, args=(name, started), name=f"tool-{name}", daemon=True).start()
            started.wait(10)
        return cls.states()

    @classmethod
    def program(cls, name: MachineToolName) -> Path:
        """The command `name`'s agent tools run; ToolNotReady, in one line naming where to install it, before it is."""
        state, spec = cls.state(name), MACHINE_TOOLS[name]
        if state.missing:
            raise ToolNotReady(f"{spec.label} needs {', '.join(state.missing)} on this PC's PATH: install it there (its package manager), then ask again")
        if not state.ready:
            under_way = {"installing": " (an install is under way: ask again in a minute)", "failed": f" (the last install failed: {state.detail})"}.get(state.phase, "")
            raise ToolNotReady(f"{spec.label} is not installed on this PC{under_way}: its owner installs it in Galaius, on this PC's page under "
                               f"« Capabilities » ({spec.label} → Install). Nothing was done.")
        return cls.venv_bin(name) / _command(spec.package)

    @classmethod
    def _job(cls, name: MachineToolName, started: threading.Event) -> None:
        folder = cls.folder(name)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(folder / "install.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if not FILE_LOCK.try_acquire(descriptor):
                return  # another process installs it now
            try:
                started.set()
                cls._install(name)
            finally:
                FILE_LOCK.release(descriptor)
        finally:
            os.close(descriptor)
            started.set()

    @classmethod
    def _install(cls, name: MachineToolName) -> None:
        spec, folder = MACHINE_TOOLS[name], cls.folder(name)
        (folder / "failed.json").unlink(missing_ok=True)
        try:
            uv = shutil.which("uv")
            if uv is None:
                raise RuntimeError("uv is not on this PC's PATH (https://docs.astral.sh/uv/ installs it)")
            venv = folder / "venv"
            shutil.rmtree(venv, ignore_errors=True)
            python = f"{sys.version_info.major}.{sys.version_info.minor}"
            for argv in ([uv, "venv", "--python", python, str(venv)],
                         [uv, "pip", "install", "--python", str(cls.venv_bin(name) / ("python.exe" if sys.platform == "win32" else "python")),
                          "--exclude-newer", spec.resolved_before.isoformat(), f"{spec.package}=={spec.version}"]):
                done = subprocess.run(argv, capture_output=True, text=True, timeout=cls.INSTALL_SECONDS, stdin=subprocess.DEVNULL)
                if done.returncode != 0:
                    raise RuntimeError((done.stderr or done.stdout).strip().splitlines()[-1][:300] if (done.stderr or done.stdout).strip() else f"{argv[1]} exited {done.returncode}")
            (folder / "installed.json").write_text(json.dumps({"version": spec.version}), encoding="utf-8")
            logger.info("%s installed: %s==%s", name, spec.package, spec.version)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            logger.warning("%s install failed: %s", name, error)
            (folder / "failed.json").write_text(json.dumps({"detail": str(error)[:400]}), encoding="utf-8")

    @classmethod
    def _installing(cls, name: MachineToolName) -> bool:
        path = cls.folder(name) / "install.lock"
        if not path.is_file():
            return False
        descriptor = os.open(path, os.O_RDWR)
        try:
            if FILE_LOCK.try_acquire(descriptor):
                FILE_LOCK.release(descriptor)
                return False
            return True
        finally:
            os.close(descriptor)

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}


def _command(package: str) -> str:
    """The console script a package installs under its own name."""
    return f"{package}.exe" if sys.platform == "win32" else package
