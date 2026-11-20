"""Script languages this machine runs and one run of them."""

import os
import re
import shutil
import sys
from pathlib import Path
from typing import ClassVar, Literal, Self
from pydantic import BaseModel, ConfigDict, model_validator
from galaius_core import MachineCommand, ScriptFile, ScriptLanguage
from galaius.machine.connection.logs import audit
if sys.platform == "win32":
    import win32api


class ScriptRuntime(BaseModel):
    """How one Script language (`ScriptLanguage`) runs: its name as the owner reads it, the file its
    inline code is written to, the program that runs it on each system (tried in order: a name on
    PATH, or an absolute path; `{system}` is Windows' own system folder) with its options, the
    program a pooled sandbox runs it with, and what is said where nothing runs it. `SCRIPT_RUNTIMES`
    holds one per language."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    title: str
    suffix: str
    programs: dict[Literal["posix", "windows"], tuple[str, ...]] = {}
    options: dict[Literal["posix", "windows"], tuple[str, ...]] = {}
    #: The program inside a pooled (Linux gVisor) sandbox; None: not offered there.
    pooled: str | None = None
    #: Said when no program here runs it.
    missing: str
    SYSTEM: ClassVar[Literal["posix", "windows"]] = "windows" if sys.platform == "win32" else "posix"

    @property
    def here(self) -> tuple[str, ...]:
        """This system's candidates, `{system}` filled in."""
        system = win32api.GetSystemDirectory() if self.SYSTEM == "windows" else ""
        return tuple(candidate.format(system=system) for candidate in self.programs.get(self.SYSTEM, ()))

    @property
    def label(self) -> str:
        """The program as the owner reads it before approving (`python3`, `/bin/sh`, `pwsh`, `cmd.exe`)."""
        first = self.programs.get(self.SYSTEM, ())
        return " ".join((Path(first[0]).name if "{system}" in first[0] else first[0], *self.options.get(self.SYSTEM, ()))) if first else self.title

    def program(self, path: str | None) -> str | None:
        """The first candidate this computer has; None when it has none."""
        for candidate in self.here:
            found = (candidate if Path(candidate).is_file() else None) if Path(candidate).is_absolute() else self.which(candidate, path)
            if found is not None:
                return found
        return None

    @staticmethod
    def which(name: str, path: str | None) -> str | None:
        """`name` on PATH only, never in the folder the runner stands in (Windows looks there
        first: a file step could plant `pwsh.cmd` beside it)."""
        found = shutil.which(name, path=path)
        folders = {Path(folder).resolve() for folder in (path or "").split(os.pathsep) if folder}
        return None if found is None or (Path(found).resolve().parent == Path.cwd().resolve() and Path.cwd().resolve() not in folders) else found

    @staticmethod
    def languages(path: str | None) -> tuple[ScriptLanguage, ...]:
        """The Script languages this computer runs, as the runner announces them (`script:<language>`):
        Python always (the runner's own), each other where its program is found."""
        return tuple(language for language, runtime in SCRIPT_RUNTIMES.items() if language == "python" or runtime.program(path) is not None)


SCRIPT_RUNTIMES: dict[ScriptLanguage, ScriptRuntime] = {
    # A machine's Python file runs on its own Python: python3, or on Windows the `py -3` launcher.
    "python": ScriptRuntime(title="Python", suffix=".py", programs={"posix": ("python3",), "windows": ("py",)}, options={"windows": ("-3",)}, pooled="python3",
                            missing="Python is not on this machine's PATH (python3, or py on Windows): name the interpreter in the step"),
    "shell": ScriptRuntime(title="Shell", suffix=".sh", programs={"posix": ("/bin/sh",)}, pooled="/bin/sh",
                           missing="a shell script runs with /bin/sh, which this computer does not have (Windows): write the step in PowerShell or cmd"),
    # The owner approved this exact code: the execution policy (not a security boundary) never refuses it.
    "powershell": ScriptRuntime(title="PowerShell", suffix=".ps1", programs={"posix": ("pwsh",), "windows": ("pwsh", "powershell")},
                                options={system: ("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File") for system in ("posix", "windows")},
                                missing="PowerShell is not installed on this machine (pwsh): install PowerShell 7 or place the step on a Windows machine"),
    # Windows' own cmd.exe, never %COMSPEC% (the environment could name another program).
    "cmd": ScriptRuntime(title="cmd", suffix=".cmd", programs={"windows": ("{system}\\cmd.exe",)}, options={"windows": ("/d", "/c")},
                         missing="cmd scripts run on Windows only: place the step on a Windows machine, or write it in shell or PowerShell"),
}


class ScriptExecution(BaseModel):
    """The program a Script step runs through here, chosen from what the owner approves (language,
    code, a file's own `interpreter`): the approval preview shows it, the runner resolves it. A file
    naming its interpreter runs through that; Python declaring its packages (PEP 723) through
    `uv run --script`, which installs them into a cached throwaway environment; other inline Python
    on the runner's own interpreter, a machine's Python file on that machine's Python; every other
    language on its `SCRIPT_RUNTIMES` program."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    program: Literal["runner_python", "python3", "uv", "shell", "powershell", "cmd", "custom"]
    #: The program a file names (a command on PATH or an absolute path); only for "custom".
    interpreter: str | None = None
    #: PEP 723 inline script metadata: `# /// script` … `# ///` (the editor writes a script's packages there).
    METADATA: ClassVar[re.Pattern] = re.compile(r"(?m)^# /// script\r?\n(?:^#(?: .*)?\r?\n)*?^# ///\r?$")  # CRLF too: a file saved on Windows
    UV: ClassVar[tuple[str, ...]] = ("run", "--quiet", "--no-project", "--script")

    @model_validator(mode="after")
    def custom_interpreter(self) -> Self:
        if (self.program == "custom") != (self.interpreter is not None):
            raise ValueError("only a custom program names its interpreter")
        return self

    @classmethod
    def select(cls, language: ScriptLanguage, source: str, file: ScriptFile | None = None) -> Self:
        if file is not None and file.interpreter is not None:
            return cls(program="custom", interpreter=file.interpreter)
        if language != "python":
            return cls(program=language)
        return cls(program="uv" if cls.METADATA.search(source) else "python3" if file is not None else "runner_python")

    @property
    def runtime(self) -> ScriptRuntime | None:
        """The table entry whose program runs it (none for the runner's own Python, uv or a named interpreter)."""
        return SCRIPT_RUNTIMES["python" if self.program == "python3" else self.program] if self.program in {"python3", "shell", "powershell", "cmd"} else None

    @property
    def options(self) -> tuple[str, ...]:
        return self.UV if self.program == "uv" else () if self.runtime is None else self.runtime.options.get(ScriptRuntime.SYSTEM, ())

    @property
    def description(self) -> str:
        if self.runtime is not None:
            return self.runtime.label
        return " ".join((self.interpreter if self.program == "custom" else {"runner_python": "galaius's own Python", "uv": "uv"}[self.program], *self.options))


def audit_script(command: MachineCommand, path: str | None = None) -> None:
    """A durable local record of every script ATTEMPT (not just successes): source digest,
    the workflow/node/run that triggered it. Threat-modeler mitigation #4 — deliberately
    machine-side, next to the running process, so the record survives even a server outage;
    pairs with the server's OWN `machine_script_approvals.approved_by_account_id` for who
    allowlisted the digest in the first place."""
    audit("script-audit.log", {
        "language": command.impl.language, "source_digest": command.impl.source_digest, "approval_digest": command.impl.approval_digest(command.config), "workflow_id": str(command.workflow.key.id),
        "workflow_revision": str(command.workflow.revision), "node_id": str(command.node_id), "run_id": str(command.run_id),
        **({"path": path} if path is not None else {}),
    })
