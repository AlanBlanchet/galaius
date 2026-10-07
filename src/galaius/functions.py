"""Local function registry: a `@galaius.function`-decorated Python callable, or a registered
shell command, invocable from a workflow's "On my PC" machine-function node — the inbound half
of PC integration; `galaius.workflows.run` is the outbound half. Every entry is typed (each
input port and the single output port carry a `ValueType`) and version-hashed, so a workflow
node pinned against one signature fails loudly, never silently, when the local function changes
shape. Registered under `~/.config/galaius/functions.json`, 0600, same layout discipline as
`MachineConfig`."""

import hashlib
import importlib.util
import inspect
import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Literal, get_type_hints
from uuid import uuid4

from galaius_core import MachineFunctionSummary, PortSpec, ValueType
from pydantic import BaseModel, ConfigDict, Field, model_validator

from galaius.paths import UserPaths
from galaius.private_files import PRIVATE_FILES

FunctionKind = Literal["python", "shell"]
#: What a step may do on a machine: its permission, and the machine owner's ceiling over every step.
PermissionLevel = Literal["read_only", "full_access"]

#: Every distinct Python type a function signature may be typed with, mapped onto the wire's
#: `ValueType`. Anything else (a dataclass, a pydantic model, an untyped `Any`) is carried as
#: `json` — the generic escape hatch every other typed boundary in this codebase already uses.
_PYTHON_VALUE_TYPES: tuple[tuple[type, ValueType], ...] = ((bool, "boolean"), (str, "text"), (int, "number"), (float, "number"), (dict, "json"), (list, "json"))


def _value_type(annotation: object) -> ValueType:
    origin = getattr(annotation, "__origin__", None)
    base = origin if origin is not None else annotation
    for python_type, value_type in _PYTHON_VALUE_TYPES:
        if base is python_type or (isinstance(base, type) and issubclass(base, python_type)):
            return value_type
    return "json"


class FunctionMeta(BaseModel):
    """Marker `@galaius.function` leaves on the decorated callable, read back by `discover_python`."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    description: str = Field(min_length=1, max_length=240)
    permission: PermissionLevel = "read_only"


def function(target: Callable | None = None, *, description: str | None = None, permission: PermissionLevel = "read_only") -> Callable:
    """Marks a typed function as callable from a workflow on this machine — bare
    (`@galaius.function`) or parameterized (`@galaius.function(description=..., permission="full_access")`).
    Without an explicit `description`, the callable's own docstring first line is used; every
    parameter and the return value need a type hint (`galaius functions add` fails loudly
    otherwise, at registration time rather than at a later, harder-to-place workflow run)."""
    def decorate(callable_target: Callable) -> Callable:
        doc = (callable_target.__doc__ or "").strip().splitlines()
        callable_target.__galaius_function__ = FunctionMeta(description=description or (doc[0].strip() if doc else callable_target.__name__), permission=permission)
        return callable_target
    return decorate(target) if callable(target) else decorate


class FunctionEntry(BaseModel):
    """One registered function: either a `python` callable (`path` + `attribute` locate it) or a
    `shell` command (`command` is its argv template, `{name}` placeholders typed as text ports).
    `version` is a content hash of name + description + ports — the same shape `MachineRunner`
    advertises to the server as `MachineFunctionSummary` and re-checks on every call."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, max_length=240)
    kind: FunctionKind
    permission: PermissionLevel
    ports: tuple[PortSpec, ...] = Field(min_length=1, max_length=32)
    version: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: str | None = None
    attribute: str | None = None
    command: tuple[str, ...] = ()

    @model_validator(mode="after")
    def coherent_source(self):
        if self.kind == "python" and (not self.path or not self.attribute or self.command):
            raise ValueError("a python function needs a source path and attribute, and no shell command")
        if self.kind == "shell" and (self.path or self.attribute or not self.command):
            raise ValueError("a shell function needs a command template, and no python source")
        if len({port.name for port in self.ports}) != len(self.ports):
            raise ValueError("function ports must have unique names")
        if sum(1 for port in self.ports if port.direction == "output") != 1:
            raise ValueError("a function must declare exactly one output port")
        return self

    def digest(self) -> str:
        payload = json.dumps(
            {"name": self.name, "description": self.description, "ports": [port.model_dump(mode="json") for port in self.ports]},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def summary(self) -> MachineFunctionSummary:
        return MachineFunctionSummary(name=self.name, description=self.description, version=self.version, permission=self.permission, ports=self.ports)

    def input_ports(self) -> dict[str, PortSpec]:
        return {port.name: port for port in self.ports if port.direction == "input"}


class FunctionRegistry:
    """`~/.config/galaius/functions.json`, private (`PRIVATE_FILES`) like `MachineRunner`'s config,
    written atomically: a partial write never leaves a corrupt or world-readable file."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or self.default_config_path()

    @staticmethod
    def default_config_path() -> Path:
        return UserPaths.config() / "functions.json"

    def load(self) -> tuple[FunctionEntry, ...]:
        if not self.config_path.exists():
            return ()
        payload = json.loads(PRIVATE_FILES.read_text(self.config_path))
        return tuple(FunctionEntry.model_validate(item) for item in payload)

    def save(self, entries: tuple[FunctionEntry, ...]) -> None:
        PRIVATE_FILES.write_text(self.config_path, json.dumps([entry.model_dump(mode="json") for entry in entries], separators=(",", ":")) + "\n")

    def add(self, entry: FunctionEntry) -> FunctionEntry:
        """A python entry's `path` still names wherever `discover_python` read it FROM (often a
        scratch or temp file) — registering copies that source into the registry's own storage
        first, so the function keeps working after the original file is gone (temp cleanup, a
        reboot clearing /tmp). Idempotent re-add of the same (name, version) reuses the same
        stored copy rather than duplicating it."""
        if entry.kind == "python":
            stored_source = self.config_path.parent / "sources" / f"{entry.name}-{entry.version}.py"
            if Path(entry.path).resolve() != stored_source.resolve():
                PRIVATE_FILES.directory(stored_source.parent)
                shutil.copyfile(entry.path, stored_source)
                PRIVATE_FILES.restrict(stored_source)
            entry = entry.model_copy(update={"path": str(stored_source)})
        entries = {item.name: item for item in self.load()}
        entries[entry.name] = entry
        self.save(tuple(entries.values()))
        return entry

    def remove(self, name: str) -> bool:
        entries = {item.name: item for item in self.load()}
        removed = entries.pop(name, None)
        if removed is None:
            return False
        self.save(tuple(entries.values()))
        if removed.kind == "python" and removed.path is not None:
            stored_source = self.config_path.parent / "sources" / f"{removed.name}-{removed.version}.py"
            if Path(removed.path).resolve() == stored_source.resolve():
                stored_source.unlink(missing_ok=True)
        return True

    def get(self, name: str) -> FunctionEntry:
        entry = next((item for item in self.load() if item.name == name), None)
        if entry is None:
            raise KeyError(f"no registered function named {name!r}; run `galaius functions add` first")
        return entry


def discover_python(path: Path) -> tuple[FunctionEntry, ...]:
    """Imports `path` in an isolated module namespace and returns one `FunctionEntry` per
    `@galaius.function`-decorated callable it defines, typed from its own annotations."""
    resolved = Path(path).resolve(strict=True)
    module = _load_module(resolved)
    entries = tuple(
        _entry_from_callable(value, attribute, value.__galaius_function__, resolved)
        for attribute, value in vars(module).items()
        if inspect.isfunction(value) and hasattr(value, "__galaius_function__")
    )
    if not entries:
        raise ValueError(f"{resolved} defines no @galaius.function callable")
    return entries


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"galaius_function_{path.stem}_{uuid4().hex}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {path} as a Python module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry_from_callable(target: Callable, attribute: str, meta: FunctionMeta, path: Path) -> FunctionEntry:
    signature = inspect.signature(target)
    hints = get_type_hints(target, include_extras=True)
    if "return" not in hints or hints["return"] is type(None):
        raise ValueError(f"{target.__name__} needs a non-None typed return value")
    ports = []
    for name, parameter in signature.parameters.items():
        if name not in hints:
            raise ValueError(f"{target.__name__} parameter {name!r} needs a type hint")
        ports.append(PortSpec(name=name, direction="input", value_type=_value_type(hints[name]), required=parameter.default is inspect.Parameter.empty))
    ports.append(PortSpec(name="result", direction="output", value_type=_value_type(hints["return"])))
    entry = FunctionEntry(name=target.__name__, description=meta.description, kind="python", permission=meta.permission, ports=tuple(ports), version="0" * 64, path=str(path), attribute=attribute)
    return entry.model_copy(update={"version": entry.digest()})


_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def discover_shell(name: str, description: str, command: tuple[str, ...], permission: PermissionLevel = "full_access") -> FunctionEntry:
    """A shell command form: `{arg}` tokens anywhere across `command` become typed text input
    ports, filled by `str.format` at call time — never `shell=True`, so an argument value can
    never break out of its own argv slot no matter what characters it holds."""
    placeholders = dict.fromkeys(match for token in command for match in _PLACEHOLDER.findall(token))
    ports = tuple(PortSpec(name=placeholder, direction="input", value_type="text") for placeholder in placeholders) + (PortSpec(name="result", direction="output", value_type="text"),)
    entry = FunctionEntry(name=name, description=description, kind="shell", permission=permission, ports=ports, version="0" * 64, command=command)
    return entry.model_copy(update={"version": entry.digest()})


def invoke(entry: FunctionEntry, arguments: dict[str, object], *, working_directory: Path, timeout: float = 120) -> object:
    """Runs `entry` with `arguments` bound to its declared input ports; the machine channel and
    the `functions test` CLI both call this. A python function runs under `timeout` in a worker
    thread (it cannot be killed early, only abandoned — same limit `subprocess.run(timeout=)`
    faces for a non-cooperating process); a shell function is a real bounded subprocess."""
    input_ports = entry.input_ports()
    if set(arguments) - set(input_ports):
        raise ValueError(f"{entry.name} received arguments outside its declared inputs: {sorted(set(arguments) - set(input_ports))}")
    missing = [name for name, port in input_ports.items() if port.required and name not in arguments]
    if missing:
        raise ValueError(f"{entry.name} is missing required arguments: {missing}")
    if entry.kind == "shell":
        return _invoke_shell(entry, arguments, working_directory, timeout)
    return _invoke_python(entry, arguments, timeout)


def _invoke_shell(entry: FunctionEntry, arguments: dict[str, object], working_directory: Path, timeout: float) -> str:
    import subprocess

    mapping = {key: str(value) for key, value in arguments.items()}
    try:
        argv = [token.format(**mapping) for token in entry.command]
    except (KeyError, IndexError) as error:
        raise ValueError(f"{entry.name}: unresolved command placeholder {error}") from error
    completed = subprocess.run(argv, cwd=working_directory, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or f"{entry.name} exited {completed.returncode}").strip()[-2000:])
    return completed.stdout.rstrip("\n")


def _invoke_python(entry: FunctionEntry, arguments: dict[str, object], timeout: float) -> object:
    module = _load_module(Path(entry.path))
    target = getattr(module, entry.attribute)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(target, **arguments).result(timeout=timeout)
