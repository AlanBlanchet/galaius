"""What this machine is and offers: the environment programs started here get, the agent CLIs it can run,
its CPU / RAM / disk and its GPUs. Plain functions, so a test patches the one it needs (`inventory.mps_accelerator`)."""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from galaius_core import MachineAccelerator, MachineRuntime
from galaius.agents import providers as agent_providers
from galaius.agents.catalog.profiles import ALLOWED_ENV
from galaius.workflows.functions import FunctionRegistry
from galaius.machine.connection.config import MachineConfig

if sys.platform == "win32":
    import win32api


def safe_environment() -> dict[str, str]:
    return {**{key: value for key, value in os.environ.items() if key in ALLOWED_ENV}, **system_environment()}


def system_environment() -> dict[str, str]:
    """What any program needs to start here and nothing else (no secret among them), PATH reaching
    every agent program's own folder; the programs installed from the web run with only this."""
    fixed = {"HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
             # Where a program opens the person's browser (a sign-in) on a Linux desktop.
             "DISPLAY", "WAYLAND_DISPLAY", "BROWSER",
             # Windows: what any program needs to start there (Python needs SYSTEMROOT, cmd COMSPEC), no secret among them.
             "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "USERNAME", "USERDOMAIN", "APPDATA", "LOCALAPPDATA",
             "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS", "PSMODULEPATH"}
    system = {key: value for key, value in os.environ.items() if key in fixed or key.startswith("LC_")}
    return {**system, "PATH": agent_providers.AgentProvider.search_path(system.get("PATH", os.defpath))}


def runtimes(config: MachineConfig) -> list[dict[str, str]]:
    """The agent CLIs the server may place agent steps on: none while agent steps are off here."""
    if not config.run_agents:
        return []
    return [MachineRuntime(provider=provider.name).model_dump(mode="json", exclude_none=True) for provider in agent_providers.program_providers() if provider.available()]


def resources(working_directory: Path | None = None) -> dict[str, int]:
    """CPU count, total RAM and free disk on the machine's own working directory — the other
    half of a placement fit check next to `accelerators`. POSIX `sysconf`, Windows
    `GlobalMemoryStatusEx` (no `psutil` dependency, no torch import); `cpu_count() or 1`
    matches the stdlib's own documented fallback."""
    ram_mb = 0
    try:
        total = win32api.GlobalMemoryStatusEx()["TotalPhys"] if sys.platform == "win32" else os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        ram_mb = total // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        disk_free_gb = shutil.disk_usage(working_directory or Path.home()).free // (1024 ** 3)
    except OSError:
        disk_free_gb = 0
    return {"cpu_count": os.cpu_count() or 1, "ram_mb": ram_mb, "disk_free_gb": disk_free_gb}


def accelerators() -> list[dict[str, object]]:
    """GPUs on this machine, so a `model` step (detection, segmentation) defaults here.

    Torch-free: `nvidia-smi` is queried directly and Apple's unified GPU is inferred from the
    platform, never by importing torch just to ask "is there a GPU" and dragging its multi-GB
    wheel onto a machine that runs no vision node. `none` is reported explicitly (not an empty
    list) so a runner that checked and found nothing is distinguishable from an older runner
    that never reported accelerators at all."""
    found = cuda_accelerators() or mps_accelerator()
    accelerators = found or (MachineAccelerator(kind="none", name="none", memory_mb=0),)
    return [item.model_dump(mode="json") for item in accelerators]


def cuda_accelerators() -> tuple[MachineAccelerator, ...]:
    found = shutil.which("nvidia-smi")
    if found is None:
        return ()
    try:
        completed = subprocess.run(
            [found, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if completed.returncode != 0:
        return ()
    accelerators = []
    for line in completed.stdout.splitlines():
        name, _, memory = line.partition(",")
        name, memory = name.strip(), memory.strip()
        if name and memory.isdigit():
            accelerators.append(MachineAccelerator(kind="cuda", name=name, memory_mb=int(memory)))
    return tuple(accelerators)


def mps_accelerator() -> tuple[MachineAccelerator, ...]:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return ()
    return (MachineAccelerator(kind="mps", name="Apple GPU", memory_mb=0),)


def functions() -> list[dict[str, object]]:
    """The functions this machine's owner offers (`FunctionRegistry`): none while its file cannot be read."""
    try:
        entries = FunctionRegistry().load()
    except PermissionError:
        return []
    return [entry.summary().model_dump(mode="json") for entry in entries]
