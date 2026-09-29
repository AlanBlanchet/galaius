"""The agent fence: an agent CLI started on this PC runs inside an OS sandbox built from its levels
(`interact.places.PlaceMap`), so a level shown for agents is enforced by the kernel, not by the
CLI's own permission mode (M3).

Linux only, from two parts no other process can loosen:
- bubblewrap builds the file view: the system folders read-only, a fresh empty home folder, then
  each folder set to `read` (read-only), `sandbox` / `write` (writable) or `write_on_review` (its
  staging copy, writable, mounted at the folder's own path); `hidden` and `see` folders do not
  exist inside. A fresh /tmp and /run leave out the desktop's and the user's session sockets
  (X11, D-Bus, ssh-agent, docker), and the agent gets its own process tree (no /proc/<pid>/root
  of any process outside). Network stays: the CLI talks to its model API.
- Landlock scopes (Linux 6.12+) applied just before bubblewrap starts, inherited by everything
  inside: no connection to an abstract UNIX socket made outside (the X server's listens there),
  no signal to a process outside.

`available()` says whether this PC can build one (and why not); the runner reports `fenced` on
every agent start and refuses to start unfenced when its owner switched the fence on.

Residual, stated: the agent CLI's own state (its session files, its credentials, interact's
state) stays readable and writable inside, except the files that steer later runs
(`STEERING`, read-only); every file inside a folder a level opens is reachable, dot files
included; macOS and Windows have no fence here yet."""

import ctypes
import functools
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from interact.places import LEVEL_RANK, PlaceMap, split

#: Files and folders in a tool's state that decide what LATER runs do — the owner's own unfenced
#: sessions included (settings and hooks run commands, instructions steer the model): read-only.
STEERING = ("settings.json", "settings.local.json", "CLAUDE.md", "AGENTS.md", "hooks", "agents", "rules", "skills", "commands", "plugins",
            "config.toml", "config.env", "agents.json", "keybindings.json", "mcp.json", "agent-catalog-definitions", "agent-catalog-skills")
#: Where the agent CLIs (and interact inside them) keep their state, relative to the home folder:
#: sessions, caches, their own credentials. Writable inside, except `STEERING`; `PRIVATE_STATE`
#: (logged-in browser profiles) is emptied.
TOOL_STATE = (".claude", ".claude.json", ".codex", ".interact")
PRIVATE_STATE = ("browser-profiles",)
#: Variables that point at the desktop or the user's session: gone inside (their sockets are too).
UNSET = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "SSH_AGENT_PID", "XDG_RUNTIME_DIR",
         "GNOME_KEYRING_CONTROL", "GPG_AGENT_INFO", "DOCKER_HOST")
#: System folders bound read-only when present; merged-/usr names are recreated as the links they are.
SYSTEM = ("/usr", "/etc", "/opt", "/nix", "/snap", "/sys")
MERGED = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
#: Landlock: create_ruleset / restrict_self syscall numbers (one table on every Linux architecture)
#: and the scopes (ABI 6): abstract UNIX sockets, signals.
_CREATE_RULESET, _RESTRICT_SELF, _SCOPES, _SCOPE_ABI = 444, 446, 0b11, 6
_NO_NEW_PRIVS = 38


class FenceBind(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: Path
    target: Path
    writable: bool


class Fence(BaseModel):
    """One agent's file view: `binds` over an empty `home`, `hidden` folders emptied inside a
    bound parent, started in `cwd`. Recorded on the agent's run so every later turn gets the same."""

    model_config = ConfigDict(frozen=True)
    home: Path
    cwd: Path
    binds: tuple[FenceBind, ...]
    hidden: tuple[Path, ...] = ()
    #: (link, what it points at): a program started by a link path (`~/.local/bin/claude`) keeps it.
    links: tuple[tuple[Path, Path], ...] = ()
    MODULE: ClassVar[str] = "interact.fence"

    @classmethod
    def build(cls, places: PlaceMap, *, start: Path, staging: dict[str, Path], tool_state: tuple[Path, ...], programs: tuple[Path, ...] = ()) -> "Fence":
        """The view the levels give an agent starting in `start`: every folder in force at `read`
        or later; `staging` maps each `write_on_review` folder to its staging copy (required)."""
        base = places.base
        try:
            start_parts = start.resolve().relative_to(base).parts
        except ValueError:
            raise PermissionError(f"{start} is outside this PC's working directory") from None
        if start_parts and LEVEL_RANK[places.reach(start_parts)] < LEVEL_RANK["read"]:
            raise PermissionError(f"no level opens {'/'.join(start_parts) or 'the working directory'} to agents; its owner sets one with `interact machine places`")
        home = Path.home().resolve()
        binds: list[FenceBind] = []
        for path, (folder, level) in sorted(places.in_force("read").items(), key=lambda item: len(split(item[0]))):
            if level == "write_on_review" and path in staging:
                binds.append(FenceBind(source=staging[path], target=folder, writable=True))
            elif level == "write_on_review" and start_parts[:len(split(path))] == split(path):
                raise PermissionError(f"{path} is write-after-review: an agent works there only in its staging copy")
            elif level == "write_on_review":
                binds.append(FenceBind(source=folder, target=folder, writable=False))  # read here; its writes need a review of their own
            else:
                binds.append(FenceBind(source=folder, target=folder, writable=level != "read"))
        # A folder set below `read` inside one set at `read` or later: empty inside.
        hidden = tuple(base.joinpath(*split(path)) for path, level in sorted(places.levels.items())
                       if LEVEL_RANK[level] < LEVEL_RANK["read"] and LEVEL_RANK[places.inherited(split(path))] >= LEVEL_RANK["read"])
        for state in tool_state:
            if state.exists():
                binds.append(FenceBind(source=state, target=state, writable=True))
                binds += [FenceBind(source=state / name, target=state / name, writable=False) for name in STEERING if state.is_dir() and (state / name).exists()]
                hidden += tuple(state / name for name in PRIVATE_STATE if state.is_dir() and (state / name).is_dir())
        links = tuple((program, program.resolve()) for program in programs if program.is_symlink() and not any(program.is_relative_to(folder) for folder in SYSTEM))
        for program in (*programs, Path(sys.prefix), Path(sys.base_prefix)):
            root = cls._install_root(program.resolve())
            if not any(root.is_relative_to(folder) for folder in SYSTEM) and not home.is_relative_to(root) and not any(bind.target == root for bind in binds):
                binds.append(FenceBind(source=root, target=root, writable=False))
        return cls(home=home, cwd=start.resolve(), binds=tuple(binds), hidden=hidden, links=links)

    @staticmethod
    def _install_root(program: Path) -> Path:
        """The folder a program needs whole: its package (the nearest folder with a package.json,
        three levels up at most: an npm CLI), else its own folder (a single binary, a venv)."""
        folder = program if program.is_dir() else program.parent
        for candidate in (folder, *list(folder.parents)[:3]):
            if (candidate / "package.json").is_file():
                return candidate
        return folder

    def command(self, argv: list[str]) -> list[str]:
        """`argv` run inside this fence: this module applies the Landlock scopes, then execs bwrap."""
        bwrap = shutil.which("bwrap") or "bwrap"
        view = ["--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try", "--die-with-parent", "--new-session", "--disable-userns"]
        for folder in SYSTEM:
            view += ["--ro-bind-try", folder, folder]
        for folder in MERGED:
            view += ["--symlink", os.readlink(folder), folder] if os.path.islink(folder) else ["--ro-bind-try", folder, folder]
        view += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run"]
        resolver = Path("/etc/resolv.conf").resolve()
        if resolver.is_relative_to("/run"):
            view += ["--ro-bind-try", str(resolver.parent), str(resolver.parent)]
        view += ["--tmpfs", str(self.home)]
        for bind in self.binds:
            view += ["--bind" if bind.writable else "--ro-bind", str(bind.source), str(bind.target)]
        for folder in self.hidden:
            view += ["--tmpfs", str(folder)]
        for link, target in self.links:
            view += ["--symlink", str(target), str(link)]
        view += ["--setenv", "HOME", str(self.home)]
        for name in UNSET:
            view += ["--unsetenv", name]
        view += ["--chdir", str(self.cwd)]
        return [sys.executable, "-m", self.MODULE, "--", bwrap, *view, "--", *argv]


def fenced(argv: list[str], fence: Fence | None) -> list[str]:
    """`argv` inside `fence`, or unchanged when the run has none."""
    return argv if fence is None else fence.command(argv)


def landlock_abi() -> int:
    """The kernel's Landlock ABI version (0: none or not Linux)."""
    if not sys.platform.startswith("linux"):
        return 0
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    version = libc.syscall(_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    return max(int(version), 0)


def scope_and_exec(argv: list[str]) -> None:
    """Landlock scopes on this process (inherited by all it starts), then `argv` in its place."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    attributes = struct.pack("QQQ", 0, 0, _SCOPES)  # handled fs, handled net, scoped
    buffer = ctypes.create_string_buffer(attributes, len(attributes))
    ruleset = libc.syscall(_CREATE_RULESET, buffer, ctypes.c_size_t(len(attributes)), ctypes.c_uint32(0))
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "Landlock ruleset refused")
    if libc.prctl(_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 or libc.syscall(_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
        raise OSError(ctypes.get_errno(), "Landlock scopes could not be applied")
    os.close(ruleset)
    os.execvp(argv[0], argv)


@functools.cache
def available() -> tuple[bool, str]:
    """(this PC can build the fence, why not): Linux, bubblewrap installed, Landlock scopes, and
    one trial run succeeding (user namespaces may be blocked by the system's policy)."""
    if not sys.platform.startswith("linux"):
        return False, f"the agent fence needs Linux (bubblewrap and Landlock); on {sys.platform} agents are not fenced yet"
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return False, "bubblewrap (bwrap) is not installed: install it to fence agents"
    if (abi := landlock_abi()) < _SCOPE_ABI:
        return False, f"this kernel's Landlock (ABI {abi}) cannot scope sockets and signals: Linux 6.12 or later is needed"
    trial = subprocess.run([sys.executable, "-m", Fence.MODULE, "--", bwrap, "--unshare-user", "--unshare-pid", "--die-with-parent", "--ro-bind", "/", "/", "true"],
                           capture_output=True, text=True, timeout=20, check=False)
    if trial.returncode != 0:
        return False, f"bubblewrap cannot start here: {(trial.stderr.strip().splitlines() or ['exit ' + str(trial.returncode)])[-1][:200]}"
    return True, ""


if __name__ == "__main__":
    arguments = sys.argv[1:]
    scope_and_exec(arguments[1:] if arguments[:1] == ["--"] else arguments)
