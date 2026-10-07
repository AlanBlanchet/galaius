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
  (`NEVER_GRANTABLE`, `DOT_SECRETS`) read as empty.
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
import fnmatch
import functools
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from galaius_core import PlaceLevel

from galaius.places import LEVEL_RANK, NEVER_GRANTABLE, PlaceMap, split

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
KEYS: dict[str, tuple[str, ...]] = {"claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"), "codex": ("OPENAI_API_KEY", "CODEX_API_KEY")}
SECRET_SUFFIXES = ("_API_KEY", "_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASS", "_CREDENTIALS")
#: Hidden names that hold credentials: read as empty inside an opened folder.
DOT_SECRETS = frozenset({".env", ".netrc", ".npmrc", ".pypirc", ".pgpass", ".git-credentials", ".ssh", ".gnupg", ".aws", ".kube", ".docker",
                         ".password-store", ".vault-token", ".terraform.d"})
#: Folders not walked when checking an opened folder (build output, caches, dependencies).
SKIP = frozenset({"node_modules", ".venv", "venv", "__pycache__", ".cache", "target", "dist", "build", ".next", ".tox", ".mypy_cache", ".gradle", ".pytest_cache"})
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
    #: Variables unset inside besides `UNSET`: secrets that are not the run's own CLI key.
    secrets: tuple[str, ...] = ()
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
        for name in (*UNSET, *self.secrets):
            view += ["--unsetenv", name]
        view += ["--chdir", str(self.cwd)]
        inner = [sys.executable, "-I", "-c", INNER, f"{EGRESS_MOUNT}/egress.sock", *argv]
        guard = json.dumps({"reviews": str(self.reviews) if self.reviews else None, "guards": [[str(top), place, str(folder)] for top, place, folder in self.guards]})
        return [sys.executable, "-m", self.MODULE, "outer", ",".join(self.egress), guard, "--", bwrap, *view, "--", *inner]


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
                secret = name in DOT_SECRETS or name.startswith(".env.") or (not name.startswith(".") and NEVER_GRANTABLE.refusal((entry.name,), None) is not None)
                if entry.is_dir():
                    if name == ".git":
                        found.repositories.append(Path(current))
                    elif secret:
                        found.secret_folders.append(Path(entry.path))
                    elif name not in SKIP and depth < SCAN_DEPTH:
                        stack.append((Path(entry.path), depth + 1))
                elif secret and entry.is_file():
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
    #: `write_on_review` folder -> its staging copy for this run.
    staging: dict[str, Path] = Field(default_factory=dict)
    #: The CLIs whose state the run gets a private copy of (`TOOLS`).
    providers: tuple[str, ...] = ()
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
        if start_parts and LEVEL_RANK[places.reach(start_parts)] < LEVEL_RANK["read"]:
            raise PermissionError(f"no level opens {'/'.join(start_parts)} to agents; its owner sets one on this PC with `galaius machine places`")
        binds, emptied, blanked, guards = [], [], [], []
        for path, (folder, level) in sorted(places.in_force("read").items(), key=lambda item: len(split(item[0]))):
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
        links = tuple((program, program.resolve()) for program in self.programs if program.is_symlink() and not any(program.is_relative_to(folder) for folder in SYSTEM))
        for program in (*self.programs, Path(sys.prefix), Path(sys.base_prefix)):
            root = _install_root(program.resolve())
            if not any(root.is_relative_to(folder) for folder in SYSTEM) and not home.is_relative_to(root) and not any(bind.target == root for bind in binds):
                binds.append(FenceBind(source=root, target=root, writable=False))
        return Fence(home=home, cwd=self.start.resolve(), binds=tuple(binds), emptied=tuple(emptied), blanked=tuple(blanked), links=links, egress=self.egress,
                     guards=tuple(guards), reviews=self.reviews, secrets=self._foreign_secrets())

    def _foreign_secrets(self) -> tuple[str, ...]:
        """Variables of the launching environment that look like secrets and are not the key of a
        CLI this run uses (a model key of another provider, a cloud token): unset inside."""
        own = {name for provider in self.providers for name in KEYS.get(provider, ())}
        return tuple(sorted(name for name in os.environ if name.upper().endswith(SECRET_SUFFIXES) and name not in own))

    def _tool_state(self, home: Path) -> list[FenceBind]:
        """Each CLI's state as a private copy: an empty folder, its sign-in / settings / owner
        instructions read-only, its sessions in this run's own folder, its rewritten files copied."""
        binds = []
        for provider in self.providers:
            tool = TOOLS[provider]
            folder = home / tool["folder"][0]
            private = self.state / provider
            binds.append(FenceBind(source=_private(private / "home"), target=folder, writable=True))
            binds += [FenceBind(source=folder / name, target=folder / name, writable=False) for name in tool["read"] if (folder / name).exists()]
            binds += [FenceBind(source=_private(private / name), target=folder / name, writable=True) for name in tool["sessions"]]
            for name in tool["copies"]:
                copy = private / name
                if not copy.exists() and (home / name).is_file():
                    shutil.copy2(home / name, copy)
                    copy.chmod(0o600)
                if copy.exists():
                    binds.append(FenceBind(source=copy, target=home / name, writable=True))
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


def fenced(argv: list[str], fence: FenceSpec | None) -> list[str]:
    """`argv` inside the fence `fence` builds now (from the current levels), or unchanged."""
    return argv if fence is None else fence.build().command(argv)


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


def outer(hosts: tuple[str, ...], guard: SteeringGuard, argv: list[str]) -> int:
    """Outside the view: scopes, the egress proxy on a fresh socket, bwrap as a child (signals
    passed on), steering names swept while it runs and once it ended; its exit code returned."""
    scope_self()
    before = guard.candidates() | guard.nested()
    with tempfile.TemporaryDirectory(prefix="ifence-") as folder:
        threading.Thread(target=EgressProxy(hosts=hosts).serve, args=(os.path.join(folder, "egress.sock"),), daemon=True).start()
        child = subprocess.Popen([part.replace("{egress}", folder) for part in argv])
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
    trial = subprocess.run([sys.executable, "-m", Fence.MODULE, "outer", "", "{}", "--", bwrap, "--unshare-user", "--unshare-pid", "--unshare-net", "--die-with-parent",
                            "--ro-bind", "/", "/", "true"], capture_output=True, text=True, timeout=20, check=False)
    if trial.returncode != 0:
        return False, f"bubblewrap cannot start here: {(trial.stderr.strip().splitlines() or ['exit ' + str(trial.returncode)])[-1][:200]}"
    return True, ""


if __name__ == "__main__":
    mode, *rest = sys.argv[1:]
    if mode != "outer" or len(rest) < 3 or rest[2] != "--":
        raise SystemExit("usage: python -m galaius.fence outer <hosts> <guard json> -- <bwrap argv>")
    raise SystemExit(outer(tuple(host for host in rest[0].split(",") if host), SteeringGuard.model_validate_json(rest[1]), rest[3:]))
