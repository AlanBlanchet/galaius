"""The agent fence: an agent CLI started on this PC runs inside an OS sandbox built from its levels
(`galaius.places.PlaceMap`), so a level shown for agents is enforced by the kernel, not by the
CLI's own permission mode.

What a run records is its `FenceSpec` (the levels file, where it starts, its staging copies, its
CLI); the view is rebuilt from the PC's CURRENT levels at every turn, so a narrowing reaches a run
started before it. Linux only, from parts no process inside can loosen:
- bubblewrap builds the file view: the system folders read-only, an empty home folder, then each
  folder set to `read` (read-only), `sandbox` / `write` (writable) or `write_on_review` (its
  staging copy); `hidden` and `see` folders do not exist inside. In every writable folder, git
  hooks, git config and the `STEERING` files already at its top or a repository's top stay
  read-only, and one the agent creates there is moved out into a review by the helper outside
  (checked every half second and when the turn ends). In every opened folder, credential files
  (`NEVER_GRANTABLE.secret`) read as empty.
- the CLI's own state is a private copy kept by the runner per run: its sign-in, settings and
  instructions read-only, its sessions in a runner-owned folder, `~/.claude.json` a copy never
  written back; galaius's own state (run records, keys) is absent.
- no network but loopback: HTTPS leaves only through the runner's egress proxy (`EgressProxy`),
  to the model API hosts the run names; nothing on the PC's loopback is reachable.
- Landlock scopes (Linux 6.12+) on everything: no abstract UNIX socket made outside (the X
  server's), no signal to a process outside.

Residual, stated: the CLI's sign-in file is readable inside (it needs it); the model API itself is
an exit; folders deeper than `SCAN_DEPTH` below an opened folder are not checked for credential
files or repositories; a steering file created by the agent exists in the folder for up to half a second
before it is moved out (an instruction file in a subfolder: until the turn ends); macOS and Windows have no fence here yet."""

import ctypes
import errno
import fnmatch
import functools
import json
import os
import platform
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from galaius_core import PlaceLevel

from galaius.paths import UserPaths
from galaius.pinned_directory import PinnedDirectory
from galaius.places import DEPENDENCY_FOLDERS, LEVEL_RANK, NEVER_GRANTABLE, PlaceMap, split
from galaius.upgrade.store import active_interpreter

#: Per CLI: its state folder under home, what of it the agent reads (sign-in, settings, the
#: owner's instructions: read-only), the folders it keeps sessions in (a private per-run folder:
#: a resumed turn finds its own session, never anyone else's), and files it rewrites at start (a
#: private copy, never written back).
TOOLS: dict[str, dict[str, tuple[str, ...]]] = {
    "claude": {"folder": (".claude",), "read": (".credentials.json", "settings.json", "CLAUDE.md", "agents", "rules", "skills", "commands", "hooks", "plugins"),
               "sessions": ("projects",), "copies": (".claude.json",)},
    "codex": {"folder": (".codex",), "read": ("auth.json", "config.toml", "AGENTS.md", "prompts", "rules"), "sessions": ("sessions",), "copies": ()},
}
#: The hosts each CLI reaches its model through (HTTPS only).
EGRESS: dict[str, tuple[str, ...]] = {
    "claude": ("api.anthropic.com", "console.anthropic.com", "claude.ai", "statsig.anthropic.com"),
    "codex": ("api.openai.com", "chatgpt.com", "auth.openai.com"),
}
#: Names at the top of a Write folder or a git repository that an editor, git or an agent CLI
#: obeys or runs later: never created or changed there by a fenced agent.
STEERING = frozenset({".claude", ".mcp.json", ".vscode", ".envrc", ".githooks", ".husky", ".cursor", ".idea", ".devcontainer", ".gitmodules",
                      "claude.md", "agents.md", "gemini.md", "claude.local.md"})
#: Instruction files an agent CLI or direnv reads in whichever folder it works in: read-only at
#: any depth of a writable folder, and one created there is held for review when the turn ends.
INSTRUCTIONS = frozenset({"claude.md", "claude.local.md", "agents.md", "gemini.md", ".envrc"})
#: Per CLI: the variables holding its own key (the only secrets from the environment it keeps);
#: every other variable that looks like a secret (`SECRET_SUFFIXES`) is unset inside.
#: The one variable galaius hands Codex the token of a self-hosted model it routes it to in (`agents.profiles`): its own key.
COMPAT_KEY = "GALAIUS_OPENAI_COMPAT_KEY"
KEYS: dict[str, tuple[str, ...]] = {"claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
                                    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", COMPAT_KEY)}
SECRET_SUFFIXES = ("_API_KEY", "_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASS", "_CREDENTIALS")
SCAN_DEPTH, SCAN_ENTRIES = 8, 200_000
#: Variables that point at the desktop or the user's session: gone inside (their sockets are too).
UNSET = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "SSH_AGENT_PID", "XDG_RUNTIME_DIR",
         "GNOME_KEYRING_CONTROL", "GPG_AGENT_INFO", "DOCKER_HOST")
#: System folders bound read-only when present; merged-/usr names are recreated as the links they are.
SYSTEM = ("/usr", "/etc", "/opt", "/nix", "/snap", "/sys")
MERGED = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
EGRESS_MOUNT = "/run/galaius-egress"
#: Landlock: create_ruleset / restrict_self syscall numbers (one table on every Linux architecture)
#: and the scopes (ABI 6): abstract UNIX sockets, signals.
_CREATE_RULESET, _RESTRICT_SELF, _SCOPES, _SCOPE_ABI = 444, 446, 0b11, 6
_NO_NEW_PRIVS = 38
#: Run inside the fence before the agent (`python -I -c`, no import of this package there): a
#: loopback listener piping each connection to the egress socket, then the proxy variables set
#: and the agent exec'd in this process's place.
INNER = r"""
import os, socket, sys, threading
path, argv = sys.argv[1], sys.argv[2:]
if os.path.exists(path):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    port = server.getsockname()[1]
    if os.fork() == 0:
        def pipe(source, sink):
            try:
                while data := source.recv(65536):
                    sink.sendall(data)
            except OSError:
                pass
            finally:
                for end in (source, sink):
                    try:
                        end.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        def carry(client):
            upstream = socket.socket(socket.AF_UNIX)
            try:
                upstream.connect(path)
            except OSError:
                client.close()
                return
            threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
            pipe(upstream, client)
        while True:
            client, _ = server.accept()
            threading.Thread(target=carry, args=(client,), daemon=True).start()
    server.close()
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        os.environ[name] = f"http://127.0.0.1:{port}"
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ""
os.execvp(argv[0], argv)
"""


class FenceBind(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: Path
    target: Path
    writable: bool


class Fence(BaseModel):
    """One turn's view: `binds` over an empty `home`; `emptied` folders and `blanked` files read
    as empty; started in `cwd`; HTTPS only to `egress`."""

    model_config = ConfigDict(frozen=True)
    home: Path
    cwd: Path
    binds: tuple[FenceBind, ...]
    emptied: tuple[Path, ...] = ()
    blanked: tuple[Path, ...] = ()
    links: tuple[tuple[Path, Path], ...] = ()
    egress: tuple[str, ...] = ()
    #: (top of a writable folder or repository, its place, the place's folder): watched from outside
    #: for new `STEERING` names, held in the review store `reviews`.
    guards: tuple[tuple[Path, str, Path], ...] = ()
    reviews: Path | None = None
    #: The secret-looking variables (`SECRET_SUFFIXES`) the run keeps: its CLIs' own keys. `outer` drops every other one
    #: from the environment the fence is started with, whoever built it (a key the runner adds for one child included).
    keys: tuple[str, ...] = ()
    MODULE: ClassVar[str] = "galaius.fence"

    def command(self, argv: list[str]) -> list[str]:
        """`argv` run inside this fence: this module (outside) applies the Landlock scopes and serves
        the egress proxy, bwrap builds the view, `INNER` hands the agent its proxy."""
        bwrap = shutil.which("bwrap") or "bwrap"
        view = ["--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-net", "--unshare-cgroup-try", "--die-with-parent", "--new-session",
                "--disable-userns"]
        for folder in SYSTEM:
            view += ["--ro-bind-try", folder, folder]
        for folder in MERGED:
            view += ["--symlink", os.readlink(folder), folder] if os.path.islink(folder) else ["--ro-bind-try", folder, folder]
        view += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run", "--bind", "{egress}", EGRESS_MOUNT, "--tmpfs", str(self.home)]
        for bind in self.binds:
            view += ["--bind" if bind.writable else "--ro-bind", str(bind.source), str(bind.target)]
        for folder in self.emptied:
            view += ["--tmpfs", str(folder), "--remount-ro", str(folder)]
        for file in self.blanked:
            view += ["--ro-bind", "/dev/null", str(file)]
        for link, target in self.links:
            view += ["--symlink", str(target), str(link)]
        view += ["--setenv", "HOME", str(self.home)]
        for name in UNSET:
            view += ["--unsetenv", name]
        view += ["--chdir", str(self.cwd)]
        inner = [sys.executable, "-I", "-c", INNER, f"{EGRESS_MOUNT}/egress.sock", *argv]
        guard = json.dumps({"reviews": str(self.reviews) if self.reviews else None, "guards": [[str(top), place, str(folder)] for top, place, folder in self.guards]})
        return [sys.executable, "-m", self.MODULE, "outer", ",".join(self.egress), guard, ",".join(self.keys), "--", bwrap, *view, "--", *inner]


class Scan(BaseModel):
    """What one opened folder holds that the fence must handle (walked `SCAN_DEPTH` deep, links
    not followed): git repositories, and credential files / folders."""

    repositories: list[Path] = Field(default_factory=list)
    secret_files: list[Path] = Field(default_factory=list)
    secret_folders: list[Path] = Field(default_factory=list)
    instructions: list[Path] = Field(default_factory=list)

    @classmethod
    def of(cls, folder: Path) -> "Scan":
        found, seen, stack = cls(), 0, [(folder, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            seen += len(entries)
            if seen > SCAN_ENTRIES:
                raise PermissionError(f"{folder} holds more than {SCAN_ENTRIES} entries: too many to check before an agent sees it; open a smaller folder")
            for entry in entries:
                name = entry.name.casefold()
                if entry.is_symlink():
                    continue
                stored = NEVER_GRANTABLE.secret(entry.name)
                if entry.is_dir():
                    if name == ".git":
                        found.repositories.append(Path(current))
                    elif stored:
                        found.secret_folders.append(Path(entry.path))
                    elif name not in DEPENDENCY_FOLDERS and depth < SCAN_DEPTH:
                        stack.append((Path(entry.path), depth + 1))
                elif stored and entry.is_file():
                    found.secret_files.append(Path(entry.path))
                elif name in INSTRUCTIONS and entry.is_file():
                    found.instructions.append(Path(entry.path))
        return found


class FenceSpec(BaseModel):
    """What a run's fence is built from — recorded on the run (in the runner's own store, never
    inside a fence) and built again at each turn from the levels as they are then."""

    model_config = ConfigDict(frozen=True)
    working_directory: Path
    #: The machine file whose `places` are read at each turn; None: `levels` below (fixed).
    levels_file: Path | None = None
    levels: dict[str, PlaceLevel] = Field(default_factory=dict)
    #: The runner's own folders: never opened.
    internal: tuple[Path, ...] = ()
    start: Path
    #: The project folder put on this PC from the web (beneath `PROJECTS_ROOT`) that `start` is in:
    #: written there with no level (the hidden projects folder takes none), its siblings never bound.
    project: Path | None = None
    #: `write_on_review` folder -> its staging copy for this run.
    staging: dict[str, Path] = Field(default_factory=dict)
    #: The CLIs whose state the run gets a private copy of (`TOOLS`).
    providers: tuple[str, ...] = ()
    #: Per CLI, the folder its login lives in for this run when not its default under home: a
    #: subscription's own folder (`AgentProvider.auth_home`), its credential bound from there.
    homes: dict[str, Path] = Field(default_factory=dict)
    programs: tuple[Path, ...] = ()
    egress: tuple[str, ...] = ()
    #: Runner-owned folder holding this run's private tool state (never shared with another run).
    state: Path
    #: The review store steering files created by the agent are moved into.
    reviews: Path | None = None

    def places(self) -> PlaceMap:
        levels = self.levels
        if self.levels_file is not None:
            levels = json.loads(self.levels_file.read_text(encoding="utf-8")).get("places", {})
        return PlaceMap(working_directory=self.working_directory, levels=levels, internal=self.internal)

    def build(self) -> Fence:
        places = self.places()
        base, home = places.base, Path.home().resolve()
        try:
            start_parts = self.start.resolve().relative_to(base).parts
        except ValueError:
            raise PermissionError(f"{self.start} is outside this PC's working directory") from None
        opened = sorted(places.in_force("read").items(), key=lambda item: len(split(item[0])))
        if self.project is not None:
            facts = self.project.lstat()
            if not stat.S_ISDIR(facts.st_mode) or PinnedDirectory.link_like(facts) or self.project.resolve() != self.project or self.project not in (self.start.resolve(), *self.start.resolve().parents):
                raise PermissionError(f"{self.start} is not inside the plain project folder {self.project}")
            opened.append((self.project.relative_to(base).as_posix(), (self.project, "write")))
        elif start_parts and LEVEL_RANK[places.reach(start_parts)] < LEVEL_RANK["read"]:
            raise PermissionError(f"no level opens {'/'.join(start_parts)} to agents; its owner sets one on this PC with `galaius machine places`")
        binds, emptied, blanked, guards = [], [], [], []
        for path, (folder, level) in opened:
            parts = split(path)
            if level == "write_on_review" and path not in self.staging and start_parts[:len(parts)] == parts:
                raise PermissionError(f"{path} is write-after-review: an agent works there only in its staging copy")
            staged = level == "write_on_review" and path in self.staging
            source = self.staging[path] if staged else folder
            writable = level in {"sandbox", "write"} or staged
            scan = Scan.of(source)
            binds.append(FenceBind(source=source, target=folder, writable=writable))
            if writable and not staged:
                for top in (folder, *scan.repositories):
                    guards.append((top, path, folder))
                    binds += [FenceBind(source=top / child.name, target=top / child.name, writable=False) for child in os.scandir(top)
                              if child.name.casefold() in STEERING and not child.is_symlink()]
                    binds += [FenceBind(source=top / ".git" / name, target=top / ".git" / name, writable=False)
                              for name in ("hooks", "config", "info") if (top / ".git" / name).exists() and not (top / ".git" / name).is_symlink()]
                binds += [FenceBind(source=found, target=found, writable=False) for found in scan.instructions]
            emptied += [folder / found.relative_to(source) for found in scan.secret_folders]
            blanked += [folder / found.relative_to(source) for found in scan.secret_files]
        # A folder set below `read` inside one set at `read` or later: empty inside.
        emptied += [base.joinpath(*split(path)) for path, level in sorted(places.levels.items())
                    if LEVEL_RANK[level] < LEVEL_RANK["read"] and LEVEL_RANK[places.inherited(split(path))] >= LEVEL_RANK["read"]]
        binds += self._tool_state(home)
        reached, roots = list(self.programs), [_install_root(program.resolve()) for program in self.programs]
        for python in _interpreters():
            paths, folders = _runs_from(python)
            reached += paths
            roots += folders
        for root in roots:
            # A root gone (the runtime an upgrade pruned under a process still running from it) would stop bwrap.
            if root.is_dir() and not any(root.is_relative_to(folder) for folder in SYSTEM) and not home.is_relative_to(root) and not any(bind.target == root for bind in binds):
                binds.append(FenceBind(source=root, target=root, writable=False))
        # The links on the way to each program, re-made inside unless the view already holds their place.
        shown = (*(Path(folder) for folder in (*SYSTEM, *MERGED)), *(bind.target for bind in binds))
        links = tuple({link: target for path in reached for link, target in _links(path)
                       if not any(link.is_relative_to(folder) or folder.is_relative_to(link) for folder in shown)}.items())
        return Fence(home=home, cwd=self.start.resolve(), binds=tuple(binds), emptied=tuple(emptied), blanked=tuple(blanked), links=links, egress=self.egress,
                     guards=tuple(guards), reviews=self.reviews,
                     keys=tuple(sorted({name for provider in self.providers for name in KEYS.get(provider, ())})))

    def only(self, provider: str) -> Self:
        """This fence for one turn of `provider`'s CLI: its own keys, login and tool state (`KEYS`, `TOOLS`, `homes`), its
        own program and its model's hosts (`EGRESS`) — none of another CLI's. The spec a run records names every CLI it
        may fall through to (a quota refusal moves a start from claude to codex), so each turn narrows it here; a claude
        turn kept codex's OPENAI_API_KEY, its ~/.codex and api.openai.com, and the other way round (2026-10-26)."""
        others = {name for name in self.providers if name != provider}
        hosts = {host for name in others for host in EGRESS.get(name, ())}
        return self.model_copy(update={"providers": tuple(name for name in self.providers if name == provider),
                                       "programs": tuple(program for program in self.programs if program.name not in others),
                                       "egress": tuple(host for host in self.egress if host not in hosts),
                                       "homes": {name: folder for name, folder in self.homes.items() if name not in others}})

    def rehomed(self, homes: dict[str, Path]) -> Self:
        """This fence on other login folders (a run moved to another subscription, contract A9): the private copies a CLI
        rewrites (`TOOLS` copies, Claude's `.claude.json`: its account) of every CLI whose folder changed are dropped, so
        the next turn copies them from the new folder, never carrying the old login's over."""
        for provider in self.providers:
            if homes.get(provider) != self.homes.get(provider):
                for name in TOOLS[provider]["copies"]:
                    (self.state / provider / name).unlink(missing_ok=True)
        return self.model_copy(update={"homes": homes})

    def sessions(self, provider: str) -> tuple[Path, ...]:
        """Where a fenced run of `provider` keeps its sessions on the PC: this run's private folders,
        never its login folder's (a Codex session record, with its usage, is there)."""
        return tuple(self.state / provider / name for name in TOOLS[provider]["sessions"])

    def _tool_state(self, home: Path) -> list[FenceBind]:
        """Each CLI's state as a private copy: an empty folder, its sign-in / settings / owner
        instructions read-only, its sessions in this run's own folder, its rewritten files copied."""
        binds = []
        for provider in self.providers:
            tool = TOOLS[provider]
            # A login folder of its own (a subscription's) keeps the files Claude Code otherwise
            # keeps in home (`.claude.json`) inside it.
            folder = self.homes.get(provider) or home / tool["folder"][0]
            beside = folder if provider in self.homes else home
            private = self.state / provider
            binds.append(FenceBind(source=_private(private / "home"), target=folder, writable=True))
            binds += [FenceBind(source=folder / name, target=folder / name, writable=False) for name in tool["read"] if (folder / name).exists()]
            binds += [FenceBind(source=_private(session), target=folder / session.name, writable=True) for session in self.sessions(provider)]
            for name in tool["copies"]:
                copy = private / name
                if not copy.exists() and (beside / name).is_file():
                    shutil.copy2(beside / name, copy)
                    copy.chmod(0o600)
                if copy.exists():
                    binds.append(FenceBind(source=copy, target=beside / name, writable=True))
        return binds


def _private(folder: Path) -> Path:
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def _install_root(program: Path) -> Path:
    """The folder a program needs whole: its package (the nearest folder with a package.json,
    three levels up at most: an npm CLI), else its own folder (a single binary, a venv)."""
    folder = program if program.is_dir() else program.parent
    for candidate in (folder, *list(folder.parents)[:3]):
        if (candidate / "package.json").is_file():
            return candidate
    return folder


def _links(path: Path) -> list[tuple[Path, Path]]:
    """Every symbolic link met resolving `path`, its folders' included, as (the link at its real place, what it says): a
    view holding only the file at the end misses the links on the way (a runtime's `bin/python` -> uv's
    `cpython-3.14-…` folder link -> the interpreter), and exec there finds nothing."""
    found, resolved, pending = [], Path(path.anchor or "/"), list(path.absolute().parts[1:])
    while pending:
        name = pending.pop(0)
        if name == "..":
            resolved = resolved.parent
            continue
        candidate = resolved / name
        if not candidate.is_symlink():
            resolved = candidate
            continue
        if len(found) > 40:
            raise OSError(errno.ELOOP, "more than 40 links on the way", str(path))
        target = Path(os.readlink(candidate))
        found.append((candidate, target))
        if target.is_absolute():
            resolved = Path(target.anchor)
        pending[:0] = target.parts[1:] if target.is_absolute() else target.parts
    return found


def _interpreters() -> tuple[Path, ...]:
    """The interpreters a fenced turn starts: this one (`INNER`) and the active runtime's (its agent's hooks,
    `participant_argv`), two while a process started before an upgrade still runs."""
    return tuple(dict.fromkeys(Path(found) for found in (sys.executable, active_interpreter()) if found))


def _runs_from(python: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """What starting `python` goes through (itself, the base installation its venv's `pyvenv.cfg` names) and the
    folders it runs from (that venv, the installation its executable is in, the base one), read-only inside."""
    venv = Path(os.path.realpath(python.parent)).parent
    reached, roots = [python], [_prefix(python.resolve())]
    if (venv / "pyvenv.cfg").is_file():
        roots.append(venv)
        settings = {key.strip(): value.strip() for key, _, value in (line.partition("=") for line in (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines())}
        if base := settings.get("home"):
            reached.append(Path(base))
            roots.append(_prefix(Path(base).resolve() / python.name))
    return tuple(reached), tuple(roots)


def _prefix(executable: Path) -> Path:
    """The installation an interpreter belongs to: the folder holding its `bin`, else its own folder."""
    return executable.parent.parent if executable.parent.name == "bin" else executable.parent


def fenced(argv: list[str], fence: FenceSpec | None, provider: str) -> list[str]:
    """`argv`, a turn of `provider`'s CLI, inside the fence `fence` builds now for that CLI alone (`FenceSpec.only`,
    from the current levels), or unchanged."""
    return argv if fence is None else fence.only(provider).build().command(argv)


class EgressProxy(BaseModel):
    """The one way out of a fence: an HTTP CONNECT proxy on a UNIX socket, opening a tunnel only to
    port 443 of a named host (never an address, never plain HTTP)."""

    hosts: tuple[str, ...]

    def target(self, request_line: str) -> tuple[str, int] | None:
        method, _, rest = request_line.partition(" ")
        authority = rest.split(" ", 1)[0]
        host, _, port = authority.rpartition(":")
        if method != "CONNECT" or port != "443" or not host or host.replace(".", "").isdigit() or ":" in host:
            return None
        return (host, 443) if any(fnmatch.fnmatch(host.lower(), pattern) for pattern in self.hosts) else None

    def serve(self, path: str) -> None:
        server = socket.socket(socket.AF_UNIX)
        server.bind(path)
        server.listen(64)
        while True:
            client, _ = server.accept()
            threading.Thread(target=self._carry, args=(client,), daemon=True).start()

    def _carry(self, client: socket.socket) -> None:
        with client:
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 16384:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            target = self.target(line)
            if target is None:
                print(f"galaius fence: refused {line[:200]}", file=sys.stderr)
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            try:
                upstream = socket.create_connection(target, timeout=20)
            except OSError:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            with upstream:
                upstream.settimeout(None)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                rest = head.split(b"\r\n\r\n", 1)[1]
                if rest:
                    upstream.sendall(rest)
                forward = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
                forward.start()
                _pipe(upstream, client)
                forward.join(timeout=5)


def _pipe(source: socket.socket, sink: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            sink.sendall(data)
    except OSError:
        pass
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def landlock_abi() -> int:
    """The kernel's Landlock ABI version (0: none or not Linux)."""
    if not sys.platform.startswith("linux"):
        return 0
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    version = libc.syscall(_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    return max(int(version), 0)


def scope_self() -> None:
    """Landlock scopes on this process, inherited by all it starts: no abstract UNIX socket made
    outside, no signal to a process outside."""
    landlock_self(0, (), _SCOPES)


def landlock_self(handled: int, writable: Iterable[Path], scoped: int) -> None:
    """One Landlock domain on this process and all it starts: the filesystem rights `handled`
    allowed only beneath `writable` (a file gets its file rights), the scopes `scoped` (ABI 6+)."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    attributes = struct.pack("QQQ", handled, 0, scoped) if scoped else struct.pack("Q", handled)
    ruleset = libc.syscall(_CREATE_RULESET, ctypes.create_string_buffer(attributes, len(attributes)), ctypes.c_size_t(len(attributes)), ctypes.c_uint32(0))
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "Landlock ruleset refused")
    for path in writable:
        try:
            handle = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            continue  # absent here (no /dev/shm): nothing to open
        rule = struct.pack("=Qi", handled if stat.S_ISDIR(os.fstat(handle).st_mode) else handled & _FILE_WRITES, handle)
        added = libc.syscall(_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(_PATH_BENEATH), ctypes.create_string_buffer(rule, len(rule)), ctypes.c_uint32(0))
        os.close(handle)
        if added != 0:
            raise OSError(ctypes.get_errno(), f"Landlock refused a rule for {path}")
    if libc.prctl(_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 or libc.syscall(_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
        raise OSError(ctypes.get_errno(), "Landlock could not be applied")
    os.close(ruleset)


class SteeringGuard(BaseModel):
    """Outside the fence, while a turn runs: a `STEERING` name that appears at the top of a
    writable folder or repository (or `.git/hooks` where it was missing) is moved into a review."""

    reviews: Path | None = None
    guards: tuple[tuple[Path, str, Path], ...] = ()

    def candidates(self) -> set[Path]:
        found = set()
        for top, _, _ in self.guards:
            try:
                found |= {top / name for name in os.listdir(top) if name.casefold() in STEERING}
            except OSError:
                continue
            if (top / ".git" / "hooks").exists() or (top / ".git" / "hooks").is_symlink():
                found.add(top / ".git" / "hooks")
        return found

    def nested(self) -> set[Path]:
        """Instruction files at any depth of the guarded folders (walked once, when a turn starts and ends)."""
        found = set()
        for folder in {folder for _, _, folder in self.guards}:
            try:
                found |= set(Scan.of(folder).instructions)
            except PermissionError:
                continue
        return found

    def sweep(self, before: set[Path], deep: bool = False) -> None:
        if self.reviews is None:
            return
        # registry → fence → place_reviews → registry: the store is loaded where it is used.
        from galaius.place_reviews import PlaceReviews
        store = PlaceReviews(root=self.reviews)
        for path in sorted((self.candidates() | (self.nested() if deep else set())) - before):
            top, place, folder = next(guard for guard in self.guards if path.is_relative_to(guard[2]))
            try:
                store.hold(place, folder, path)
                print(f"galaius fence: {path.relative_to(folder)} held for the owner's review (galaius machine reviews)", file=sys.stderr)
            except OSError as error:
                print(f"galaius fence: could not hold {path}: {error}", file=sys.stderr)


def outer(hosts: tuple[str, ...], guard: SteeringGuard, keys: tuple[str, ...], argv: list[str]) -> int:
    """Outside the view: scopes, the egress proxy on a fresh socket, bwrap as a child (signals
    passed on) with no secret of this environment but `keys`, steering names swept while it runs
    and once it ended; its exit code returned."""
    scope_self()
    before = guard.candidates() | guard.nested()
    with tempfile.TemporaryDirectory(prefix="ifence-") as folder:
        threading.Thread(target=EgressProxy(hosts=hosts).serve, args=(os.path.join(folder, "egress.sock"),), daemon=True).start()
        env = {name: value for name, value in os.environ.items() if name in keys or not name.upper().endswith(SECRET_SUFFIXES)}
        child = subprocess.Popen([part.replace("{egress}", folder) for part in argv], env=env)
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, lambda received, _frame: child.send_signal(received))
        while True:
            try:
                code = child.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                guard.sweep(before)
        guard.sweep(before, deep=True)
        return code


#: What bubblewrap prints when this system refuses it a user namespace (the lines Codex 0.155.1
#: checks itself, codex-rs `sandboxing/src/bwrap.rs`).
USERNS_REFUSED = ("setting up uid map: Permission denied", "loopback: Failed RTM_NEWADDR",
                  "loopback: Failed RTM_NEWLINK", "No permissions to create a new namespace")
#: Ubuntu 24.04+: an AppArmor-unconfined process (a systemd service; not a VS Code terminal, whose
#: profile allows it) gets a user namespace without capabilities, so bubblewrap cannot build one.
_APPARMOR_USERNS = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
#: The bubblewrap programs a probe found able to build a namespace: a pass holds for the process,
#: a refusal is probed again (a fix applied as root takes effect without a restart).
_BWRAP_PASSED: set[str] = set()


def userns_fix() -> str:
    """The one-time root step letting bubblewrap build a namespace on this system."""
    try:
        apparmor = _APPARMOR_USERNS.read_text().strip() == "1"
    except OSError:
        apparmor = False
    if apparmor:
        profile = "/etc/apparmor.d/bwrap-userns-restrict"
        return ("AppArmor refuses unprivileged user namespaces here (kernel.apparmor_restrict_unprivileged_userns=1). "
                "Once, as root: sudo apt install apparmor-profiles && "
                "sudo install -m 0644 /usr/share/apparmor/extra-profiles/bwrap-userns-restrict /etc/apparmor.d/ && "
                f"sudo apparmor_parser -r {profile}")
    return "unprivileged user namespaces are disabled here; once, as root, allow them (sysctl user.max_user_namespaces)"


def userns_refusal(bwrap: str) -> str | None:
    """Why `bwrap` cannot build a user + network namespace on this PC, with the fix; None when it can.

    The fix comes FIRST and what bubblewrap said last: the web shows this reason in a field the
    contract bounds (`MachineFence.reason`, cut by `galaius_core.wire.fitted`), so the one-time root
    command must be the part that always survives."""
    if bwrap in _BWRAP_PASSED:
        return None
    try:
        trial = subprocess.run([bwrap, "--unshare-user", "--unshare-net", "--ro-bind", "/", "/", "true"],
                               capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None  # not a namespace refusal: the CLI using it reports its own failure
    said = trial.stderr.strip()
    if trial.returncode != 0 and any(line in said for line in USERNS_REFUSED):
        return f"bubblewrap cannot build its sandbox here. {userns_fix()} (it said: {said.splitlines()[-1]})"
    _BWRAP_PASSED.add(bwrap)
    return None


#: Landlock write rights (ABI 3: REFER since 2, TRUNCATE since 3). Reads stay unhandled: allowed
#: everywhere. REFER is safe to grant: Landlock refuses a rename or link that would gain rights.
_WRITE_FILE, _TRUNCATE, _REFER = 1 << 1, 1 << 14, 1 << 13
_WRITES = _WRITE_FILE | sum(1 << bit for bit in (4, 5, 6, 7, 8, 9, 10, 11, 12)) | _REFER | _TRUNCATE
_FILE_WRITES = _WRITE_FILE | _TRUNCATE
_ADD_RULE, _PATH_BENEATH, _CONFINE_ABI = 445, 1, 3
_SECCOMP_ALLOW, _SECCOMP_DENY = 0x7FFF0000, 0x00050000 | 13  # RET_ALLOW, RET_ERRNO | EACCES
_SET_SECCOMP, _SECCOMP_FILTER = 22, 2


class SyscallTable(BaseModel):
    """One seccomp audit arch: the syscalls refused outright, those whose AF_UNIX socket() is
    refused, and where x32 numbers begin (x86_64 only)."""
    model_config = ConfigDict(frozen=True)
    arch: int
    refused: frozenset[int]
    sockets: frozenset[int]
    x32: int | None = None


#: Per processor (`platform.machine()`), from the kernel's syscall tables (arch/x86/entry/syscalls,
#: include/uapi/asm-generic/unistd.h, arch/arm/tools/syscall.tbl): socket 41 / 359 / 198 / 281,
#: socketcall 102, io_uring_setup 425 (io_uring makes sockets past seccomp). Landlock never sees
#: connect() to a socket file (`/run/user/<uid>/bus` reaches `systemd-run --user`: a command
#: outside every confinement), so no confined process makes a UNIX socket at all; socketpair()
#: (pipes between its own processes) stays.
_SYSCALLS: dict[str, tuple[SyscallTable, ...]] = {
    "x86_64": (SyscallTable(arch=0xC000003E, refused=frozenset({425}), sockets=frozenset({41}), x32=0x40000000),
               SyscallTable(arch=0x40000003, refused=frozenset({102, 425}), sockets=frozenset({359}))),
    "aarch64": (SyscallTable(arch=0xC00000B7, refused=frozenset({425}), sockets=frozenset({198})),
                SyscallTable(arch=0x40000028, refused=frozenset({102, 425}), sockets=frozenset({281}))),
}
#: Inside a confinement: the MCP client end Codex starts, carrying its stdio to the server the
#: confinement's parent runs outside (`python -I -c RELAY <port>`).
RELAY = r"""
import socket, sys, threading
link = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
def up():
    while data := sys.stdin.buffer.read1(65536):
        link.sendall(data)
    link.shutdown(socket.SHUT_WR)
threading.Thread(target=up, daemon=True).start()
while data := link.recv(65536):
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
"""
#: The relay's port in the argv a confinement starts: the one argument starting with `RelayedServer.key` holds it.
RELAY_PORT = "{relay}"


def confinement_refusal() -> str | None:
    """Why this PC cannot confine a run itself (`Confinement`); None when it can."""
    if (abi := landlock_abi()) < _CONFINE_ABI:
        return f"this kernel's Landlock (ABI {abi}) cannot confine writes: Linux 6.2 or later is needed"
    if platform.machine() not in _SYSCALLS:
        return f"Galaius has no socket filter for {platform.machine()} processors"
    return None


def seccomp_self() -> None:
    """This process and all it starts make no UNIX socket and no io_uring (`_SYSCALLS`)."""
    code: list[tuple[int, str | int, str | int, int]] = []  # (opcode, jump if true, if false, k): a str jump names a label
    labels: dict[str, int] = {}
    tables = _SYSCALLS[platform.machine()]
    for index, table in enumerate(tables):
        labels[f"arch{index}"] = len(code)
        code += [(0x20, 0, 0, 4), (0x15, 0, f"arch{index + 1}", table.arch), (0x20, 0, 0, 0)]  # A = arch; else next; A = nr
        if table.x32 is not None:
            code.append((0x35, "deny", 0, table.x32))
        code += [(0x15, "deny", 0, number) for number in sorted(table.refused)]
        for number in sorted(table.sockets):
            code += [(0x15, 0, "allow", number), (0x20, 0, 0, 16), (0x15, "deny", "allow", 1)]  # args[0] low word == AF_UNIX
        code.append((0x06, 0, 0, _SECCOMP_ALLOW))
    labels[f"arch{len(tables)}"] = labels["allow"] = len(code)
    code.append((0x06, 0, 0, _SECCOMP_ALLOW))
    labels["deny"] = len(code)
    code.append((0x06, 0, 0, _SECCOMP_DENY))

    def jump(target: str | int, at: int) -> int:
        return labels[target] - at - 1 if isinstance(target, str) else target
    program = b"".join(struct.pack("HBBI", op, jump(true, at), jump(false, at), k) for at, (op, true, false, k) in enumerate(code))
    filters = ctypes.create_string_buffer(program, len(program))
    fprog = struct.pack("HxxxxxxP", len(program) // 8, ctypes.addressof(filters))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 or libc.prctl(_SET_SECCOMP, _SECCOMP_FILTER, ctypes.create_string_buffer(fprog, len(fprog)), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "the socket filter could not be applied")


class SignIn(BaseModel):
    """A CLI's sign-in file among a confinement's copies, and the keys naming its account in it."""
    model_config = ConfigDict(frozen=True)
    file: str
    account: tuple[str, ...]

    def account_of(self, content: bytes) -> str | None:
        try:
            value = json.loads(content)
            for key in self.account:
                value = value[key]
        except (ValueError, KeyError, TypeError):
            return None
        return value if isinstance(value, str) and value else None


class RelayedServer(BaseModel):
    """The MCP server a confinement runs outside (its argv, the variables it gets on top of the
    run's), its client end named by the config argument starting with `key` (`RELAY_PORT` in it);
    `switch`: the TOML table in the CLI's settings whose `enabled = false` means no server at all."""
    model_config = ConfigDict(frozen=True)
    argv: tuple[str, ...]
    env: dict[str, str] = Field(default_factory=dict)
    key: str
    switch: tuple[str, ...]


class Confinement(BaseModel):
    """A CLI run on a PC that refuses it a user namespace (no bubblewrap, so its own sandbox cannot
    start), confined by Galaius instead, without root: the CLI and every command it runs write only
    in the start folder (`write_start`), the temporary folders and a private home folder for its
    state; read everywhere; network on; no UNIX socket made (seccomp), no signal or abstract socket
    reaching a process outside (Landlock scopes, Linux 6.12+). Its MCP server (`relayed`) runs
    outside, in the confinement's parent, reached through `RELAY` on its first connection only, made
    within `RELAY_WAIT` (the CLI starts its MCP servers before any command runs).

    The private home (`home_variable`, a folder fresh at each turn): `shared` names of the CLI's
    login folder read-only, `copied` ones copied in (the sign-in written back after the turn when
    the CLI refreshed it for the same account and nothing changed it meanwhile), `sessions` the
    login folder's own (writable: the run's conversation stays where its readers look).
    Residual, stated: `.git` and editor settings in the start folder stay writable; the login
    folder's sessions too."""

    model_config = ConfigDict(frozen=True)
    write_start: bool
    home_variable: str
    default_home: str
    shared: tuple[str, ...] = ()
    copied: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()
    signin: SignIn | None = None
    relayed: RelayedServer | None = None
    #: Folders writable in every confinement besides the start folder (those present).
    TEMPORARY: ClassVar[tuple[str, ...]] = ("/tmp", "/var/tmp", "/dev/shm")
    DEVICES: ClassVar[tuple[str, ...]] = ("/dev/null", "/dev/zero", "/dev/full", "/dev/tty", "/dev/ptmx", "/dev/pts")
    RELAY_WAIT: ClassVar[float] = 30.0

    @model_validator(mode="after")
    def _signin_is_copied(self) -> Self:
        if self.signin is not None and self.signin.file not in self.copied:
            raise ValueError(f"the sign-in file {self.signin.file} must be among the copied ones")
        return self

    def command(self, argv: list[str]) -> list[str]:
        """`argv` started inside this confinement."""
        return [sys.executable, "-m", Fence.MODULE, "confine", self.model_dump_json(), "--", *argv]

    def _home(self, login: Path, private: Path) -> bytes | None:
        """Fill `private` from `login`; the sign-in file's content as copied (None: none)."""
        for name in self.shared:
            if (login / name).exists():
                (private / name).symlink_to(login / name)
        for name in self.sessions:
            (login / name).mkdir(mode=0o700, parents=True, exist_ok=True)
            (private / name).symlink_to(login / name)
        signed = None
        for name in self.copied:
            if (login / name).is_file():
                content = (login / name).read_bytes()
                (private / name).write_bytes(content)
                (private / name).chmod(0o600)
                signed = content if self.signin is not None and name == self.signin.file else signed
        return signed

    def _write_back(self, login: Path, private: Path, before: bytes) -> None:
        """The sign-in the CLI refreshed inside, back in its login folder: same account, valid, and
        the login folder's copy untouched meanwhile (another session's refresh is never overwritten)."""
        try:
            after, current = (private / self.signin.file).read_bytes(), (login / self.signin.file).read_bytes()
        except OSError:
            return
        account = self.signin.account_of(after)
        if after == before or current != before or account is None or account != self.signin.account_of(before):
            return
        handle, staged = tempfile.mkstemp(prefix=f".{self.signin.file}.", dir=login)
        with os.fdopen(handle, "wb") as staging:
            staging.write(after)
        os.replace(staged, login / self.signin.file)

    def _serves(self, private: Path) -> bool:
        """Whether the CLI will start the relayed server: not when none is relayed or any of its
        settings switch it off (its own, a project's `.<home>/config.toml` in the start folder or
        above): a listener nobody takes would wait for a command inside."""
        if self.relayed is None:
            return False
        folder = Path(self.default_home).name
        for settings in (private / "config.toml", *(above / folder / "config.toml" for above in (Path.cwd(), *Path.cwd().parents))):
            try:
                table = tomllib.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
            except (OSError, ValueError):
                continue  # the CLI refuses unreadable settings itself
            for key in self.relayed.switch:
                table = table.get(key) if isinstance(table, dict) else None
            if isinstance(table, dict) and table.get("enabled") is False:
                return False
        return True

    def run(self, argv: list[str]) -> int:
        """Outside: the private home, the relayed MCP server, the confined child (signals passed
        on), the sign-in written back; the child's exit code."""
        login = Path(os.environ.get(self.home_variable) or Path.home() / self.default_home)
        homes = UserPaths.agents() / "confined"
        homes.mkdir(mode=0o700, parents=True, exist_ok=True)
        for stale in homes.iterdir():  # a turn killed outright (its run moved to another model) leaves its own
            if not Path(f"/proc/{stale.name.split('.')[1] if stale.name.count('.') == 2 else 0}").exists():
                shutil.rmtree(stale, ignore_errors=True)
        private = Path(tempfile.mkdtemp(prefix=f"{os.environ.get('GALAIUS_RUN_ID', 'run')}.{os.getpid()}.", dir=homes))
        listener: socket.socket | None = None
        servers: list[subprocess.Popen] = []
        try:
            signed = self._home(login, private)
            if self._serves(private):
                listener = socket.create_server(("127.0.0.1", 0))
                port, key = str(listener.getsockname()[1]), self.relayed.key
                end = argv.index("--") if "--" in argv else len(argv)
                argv = [part.replace(RELAY_PORT, port) if at < end and part.startswith(key) else part for at, part in enumerate(argv)]
            env = {name: value for name, value in os.environ.items() if name not in UNSET}
            env |= {self.home_variable: str(private), "XDG_CACHE_HOME": str(private / ".cache"), "npm_config_cache": str(private / ".npm")}
            writable = [Path.cwd()] if self.write_start else []
            writable += [private, *(login / name for name in self.sessions), *map(Path, self.TEMPORARY), *map(Path, self.DEVICES)]
            # No thread runs yet: the relay's starts after the child exists.
            child = subprocess.Popen(argv, env=env, preexec_fn=lambda: _confine_child(writable))
            for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(number, lambda received, _frame: child.send_signal(received))
            if listener is not None:
                threading.Thread(target=self._serve, args=(listener, servers), daemon=True).start()
            code = child.wait()
            if signed is not None:
                self._write_back(login, private, signed)
            return code
        finally:
            if listener is not None:
                listener.close()
            for server in servers:
                server.terminate()
            shutil.rmtree(private, ignore_errors=True)

    def _serve(self, listener: socket.socket, servers: list[subprocess.Popen]) -> None:
        """The relayed server on the listener's first connection within `RELAY_WAIT`, then no more:
        a command inside never reaches it."""
        listener.settimeout(self.RELAY_WAIT)
        try:
            link, _ = listener.accept()
        except OSError:
            return
        finally:
            listener.close()
        with link:
            servers.append(subprocess.Popen(list(self.relayed.argv), stdin=link, stdout=link, stderr=subprocess.DEVNULL, env=os.environ | self.relayed.env))


def _confine_child(writable: list[Path]) -> None:
    """In the confined child, before its exec: Landlock over `writable`, then the socket filter. A
    refusal is written out whole first (Python reports only that the child's setup failed)."""
    try:
        landlock_self(_WRITES, writable, _SCOPES if landlock_abi() >= _SCOPE_ABI else 0)
        seccomp_self()
    except OSError as error:
        os.write(2, f"galaius confinement: {error}\n".encode())
        raise


@functools.cache
def available() -> tuple[bool, str]:
    """(this PC can build the fence, why not): Linux, bubblewrap installed, Landlock scopes, and
    one trial run succeeding (user namespaces may be blocked by the system's policy)."""
    if not sys.platform.startswith("linux"):
        return False, f"the agent fence needs Linux (bubblewrap and Landlock); on {sys.platform} agents are not fenced yet"
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return False, "bubblewrap (bwrap) is not installed: install it to fence agents"
    if refusal := userns_refusal(bwrap):
        return False, refusal
    if (abi := landlock_abi()) < _SCOPE_ABI:
        return False, f"this kernel's Landlock (ABI {abi}) cannot scope sockets and signals: Linux 6.12 or later is needed"
    trial = subprocess.run([sys.executable, "-m", Fence.MODULE, "outer", "", "{}", "", "--", bwrap, "--unshare-user", "--unshare-pid", "--unshare-net", "--die-with-parent",
                            "--ro-bind", "/", "/", "true"], capture_output=True, text=True, timeout=20, check=False)
    if trial.returncode != 0:
        return False, f"bubblewrap cannot start here: {(trial.stderr.strip().splitlines() or ['exit ' + str(trial.returncode)])[-1][:200]}"
    return True, ""


if __name__ == "__main__":
    mode, *rest = sys.argv[1:]
    if mode == "confine" and len(rest) >= 3 and rest[1] == "--":
        raise SystemExit(Confinement.model_validate_json(rest[0]).run(rest[2:]))
    if mode != "outer" or len(rest) < 4 or rest[3] != "--":
        raise SystemExit("usage: python -m galaius.fence outer <hosts> <guard json> <kept keys> -- <bwrap argv> | confine <confinement json> -- <argv>")
    raise SystemExit(outer(tuple(host for host in rest[0].split(",") if host), SteeringGuard.model_validate_json(rest[1]), tuple(key for key in rest[2].split(",") if key), rest[4:]))
