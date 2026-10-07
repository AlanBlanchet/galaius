"""One-time move of a computer's install from the product's former name, interact, to galaius.

Run once by `install.sh` / `install.ps1` (or `galaius migrate`) on a computer that still holds
interact's folders, service and MCP registrations. Every step is idempotent and leaves what it
cannot decide alone (both folders present, a non-JSON config) for the person, said in the report.
This module and its command are deleted in the first release after the rename: no alias of the
old name survives it.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from galaius.cli.clients import ClientTarget, MCPServer, Scope
from galaius.machine_service import MACHINE_SERVICE, ServiceUnavailable, SystemdMachineService, WindowsMachineService
from galaius.paths import UserPaths

Outcome = Literal["done", "already", "absent", "conflict", "manual", "failed"]


class Step(BaseModel):
    """What one migration step did, in the person's words."""

    name: str
    outcome: Outcome
    detail: str = ""


class InstallMigration(BaseModel):
    """Moves interact's state on this computer to galaius's places, each step at most once."""

    former: ClassVar[str] = "interact"
    current: ClassVar[str] = "galaius"
    #: Folder entries an installer recreates before `galaius migrate` runs (its own uv, the server it
    #: signs in to): galaius's fresh copy wins and the former one is deleted, never a conflict.
    regenerated: ClassVar[tuple[str, ...]] = ("uv", "login-server")
    #: Shell start-up files that may export the former variables; read, never edited.
    shell_files: ClassVar[tuple[str, ...]] = (".bashrc", ".bash_profile", ".profile", ".zshrc", ".zprofile", ".config/fish/config.fish")

    home: Path = Field(default_factory=Path.home)

    def folders(self) -> list[tuple[Path, Path]]:
        """(former, current) for every folder named after the product: home dot folder, settings, data."""
        current = [self.home / f".{self.current}", UserPaths.config(), UserPaths.data()]
        return [(path.with_name(path.name.replace(self.current, self.former)), path) for path in current]

    def move_folders(self) -> list[Step]:
        """Each former folder becomes the current one; one the installer already started takes the
        former's entries it lacks, keeps its own `regenerated` ones, and leaves any other name both
        hold for the person."""
        steps = []
        for former, current in self.folders():
            if not former.exists():
                steps.append(Step(name=str(current), outcome="already" if current.exists() else "absent"))
                continue
            current.parent.mkdir(parents=True, exist_ok=True)
            if not current.exists():
                os.replace(former, current)
                steps.append(Step(name=str(current), outcome="done", detail=f"moved from {former}"))
                continue
            both = sorted(entry.name for entry in former.iterdir() if (current / entry.name).exists() and entry.name not in self.regenerated)
            for entry in former.iterdir():
                if entry.name in self.regenerated and (current / entry.name).exists():
                    shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
                elif entry.name not in both:
                    os.replace(entry, current / entry.name)
            if both:
                steps.append(Step(name=str(current), outcome="conflict", detail=f"{', '.join(both)} in both {former} and {current}; the rest moved — keep one of each, delete the other, run `galaius migrate` again"))
            else:
                former.rmdir()
                steps.append(Step(name=str(current), outcome="done", detail=f"merged from {former}"))
        return steps

    def rename_settings(self) -> Step:
        """`INTERACT_*` keys in the local settings file become `GALAIUS_*`."""
        path = self.home / f".{self.current}" / "config.env"
        if not path.exists():
            return Step(name=str(path), outcome="absent")
        before = path.read_text(encoding="utf-8")
        after = re.sub(rf"\b{self.former.upper()}_", f"{self.current.upper()}_", before)
        if after == before:
            return Step(name=str(path), outcome="already")
        path.write_text(after, encoding="utf-8")
        return Step(name=str(path), outcome="done", detail=f"{before.count(self.former.upper() + '_')} settings renamed")

    def replace_service(self) -> Step:
        """The former background connection is removed and, when it existed, galaius's installed."""
        if isinstance(MACHINE_SERVICE, SystemdMachineService):
            unit = MACHINE_SERVICE.path.with_name(MACHINE_SERVICE.name.replace(self.current, self.former))
            if not unit.exists():
                return Step(name="machine service", outcome="absent")
            subprocess.run(["systemctl", "--user", "disable", "--now", unit.name], capture_output=True)
            unit.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        elif isinstance(MACHINE_SERVICE, WindowsMachineService):
            task = MACHINE_SERVICE.task.replace(self.current.capitalize(), self.former.capitalize(), 1)
            if subprocess.run(["schtasks", "/Query", "/TN", task], capture_output=True).returncode != 0:
                return Step(name="machine service", outcome="absent")
            subprocess.run(["schtasks", "/End", "/TN", task], capture_output=True)
            subprocess.run(["schtasks", "/Delete", "/TN", task, "/F"], capture_output=True)
        else:
            return Step(name="machine service", outcome="absent")
        try:
            MACHINE_SERVICE.install()
        except ServiceUnavailable as error:
            return Step(name="machine service", outcome="failed", detail=f"former service removed; galaius's could not start: {error}")
        return Step(name="machine service", outcome="done", detail="former service removed, galaius's started")

    def replace_registrations(self) -> list[Step]:
        """Each coding client that ran the former MCP server now runs galaius's, same scope."""
        steps = []
        for client in ClientTarget.all():
            if not any(found.startswith(Scope.user.value) for found in client.registrations(self.home, self.former)):
                continue
            removed = client.forget(self.former, Scope.user, self.home)
            added = client.install(MCPServer.resolve(), Scope.user, self.home, dry_run=False)
            outcome: Outcome = "manual" if "manual" in (removed.action, added.action) else "done"
            steps.append(Step(name=f"{client.label} MCP server", outcome=outcome, detail="; ".join(filter(None, [removed.target, removed.detail, added.target, added.detail]))))
        return steps

    def remove_former_program(self) -> list[Step]:
        """The former uv tool (its `interact` command) and VS Code extension folders."""
        steps = []
        uv = shutil.which("uv")
        listed = subprocess.run([uv, "tool", "list"], capture_output=True, text=True).stdout if uv else ""
        if re.search(rf"(?m)^{self.former} ", listed):
            done = subprocess.run([uv, "tool", "uninstall", self.former], capture_output=True, text=True)
            steps.append(Step(name=f"`{self.former}` command", outcome="done" if done.returncode == 0 else "failed", detail=(done.stderr or "").strip()))
        extensions = self.home / ".vscode" / "extensions"
        for folder in sorted(extensions.glob(f"alanblanchet.{self.former}-*")) if extensions.is_dir() else []:
            # No `code --uninstall-extension`: it reloads every window's extension host. Running
            # windows keep the copy they loaded; the next window opened has only galaius's.
            shutil.rmtree(folder, ignore_errors=True)
            steps.append(Step(name="VS Code extension", outcome="done", detail=f"removed {folder.name}; open a new window to load galaius's"))
        return steps

    def shell_variables(self) -> list[Step]:
        """Lines exporting the former variables: reported for the person to edit, never rewritten."""
        steps = []
        for name in self.shell_files:
            path = self.home / name
            if path.is_file():
                lines = [str(number) for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1) if f"{self.former.upper()}_" in line]
                if lines:
                    steps.append(Step(name=str(path), outcome="manual", detail=f"lines {', '.join(lines)} set {self.former.upper()}_*: rename to {self.current.upper()}_*"))
        if any(key.startswith(f"{self.former.upper()}_") for key in os.environ):
            steps.append(Step(name="this shell", outcome="manual", detail=f"{self.former.upper()}_* variables are set here; open a new terminal after editing them"))
        return steps

    def run(self) -> list[Step]:
        return [*self.move_folders(), self.rename_settings(), self.replace_service(), *self.replace_registrations(), *self.remove_former_program(), *self.shell_variables()]


def migrate() -> None:
    """Move this computer's interact install (folders, settings, service, MCP registrations) to galaius."""
    steps = InstallMigration().run()
    for step in steps:
        print(f"{step.outcome:>8}  {step.name}" + (f" — {step.detail}" if step.detail else ""))
    if any(step.outcome in {"conflict", "failed"} for step in steps):
        sys.exit(1)
