"""Server prompt editing when configured, local Git authoring otherwise."""

import os
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import stat
import sys
from typing import Annotated

from cyclopts import App, Parameter
import httpx

from interact import prompt_projection
from interact.agents.catalog_connection import CatalogConnection, CatalogConnectionError
from interact.prompt_projection import compile_prompt_projection, install_prompt_projection
from interact.prompt_publisher import publish_projection
from interact.prompt_secret import read_prompt_token
from interact.server_prompts import MAX_EDITOR_BYTES, PromptConflictError, ServerPrompts

prompts_app = App(name="prompts", help="Edit server prompts when configured, or author through local Git.")
_MAX_EDITOR_BYTES = MAX_EDITOR_BYTES


class PromptMode:
    """Choose the configured authority before touching any local authoring path."""

    @staticmethod
    def server() -> ServerPrompts | None:
        try:
            connection = CatalogConnection.load()
        except (OSError, ValueError) as error:
            _editor_error(str(error))
        return None if connection is None else ServerPrompts(connection=connection)

    @staticmethod
    def require_local() -> None:
        if PromptMode.server() is not None:
            _editor_error(
                "Server prompts are authoritative; saves are already server-versioned. "
                "Use prompts catalog, prompts read namespace/slug.md, then prompts write "
                "namespace/slug.md DIGEST with content on stdin, or the signed-in server prompt editor. "
                "Use prompts sync to refresh installed caches. The local recovery worktree is untouched.",
                "server_managed",
            )

    @staticmethod
    def revisions(server: ServerPrompts) -> None:
        try:
            revisions = server.catalog()
        except (OSError, ValueError, httpx.HTTPError):
            _editor_error("Cannot read current server revisions; local recovery worktree is untouched.")
        print(json.dumps({
            "ok": True, "source": "server", "history": "current heads only",
            "message": "Saves are server-versioned. Use prompts read PATH for current content; Git diff/history is not exposed by this API.",
            "revisions": [{"path": server.path(item.key), "digest": item.digest,
                           "revision": str(item.revision), "parent_digest": item.parent_digest,
                           "source_commit": item.source_commit, "created_at": item.created_at.isoformat()}
                          for item in revisions],
        }, separators=(",", ":")))

    @staticmethod
    def project(server: ServerPrompts, *, install: bool) -> Path:
        try:
            if not install:
                return prompt_projection.compile_server_prompt_projection(server.connection, _consumer_home())
            vscode_root = Path(os.environ.get(
                "INTERACT_PROMPT_VSCODE_ROOT", Path.home() / ".config" / "Code" / "User" / "prompts"
            ))
            state = _state_home() / "interact" / "prompts" / "installed.json"
            return prompt_projection.install_server_prompt_projection(
                server.connection, _consumer_home(), vscode_root, state, state.parent / "bootstrap-adoption.json",
            )
        except (OSError, ValueError, httpx.HTTPError):
            _editor_error("Server prompt projection failed; local recovery worktree is untouched.")


def _data_home() -> Path:
    configured = os.environ.get("XDG_DATA_HOME")
    return Path(configured) if configured else Path.home() / ".local" / "share"


def _repository() -> Path:
    return _data_home() / "interact" / "prompts"


def _cache_home() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    return Path(configured) if configured else Path.home() / ".cache"


def _state_home() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    return Path(configured) if configured else Path.home() / ".local" / "state"


def _consumer_home() -> Path:
    configured = os.environ.get("INTERACT_PROMPT_CONSUMER_ROOT")
    return Path(configured) if configured else Path.home()


def _projection(repository: Path) -> Path:
    commit_id = _run("rev-parse", "HEAD", repository=repository, output=False).stdout.strip()
    return _cache_home() / "interact" / "prompts" / commit_id


def _compile(repository: Path) -> Path:
    _require_clean(repository)
    projection = _projection(repository)
    if not projection.exists():
        compile_prompt_projection(repository, "HEAD", projection, _consumer_home())
    return projection


def _install(repository: Path) -> Path:
    projection = _compile(repository)
    vscode_root = Path(os.environ.get(
        "INTERACT_PROMPT_VSCODE_ROOT", Path.home() / ".config" / "Code" / "User" / "prompts"
    ))
    state = _state_home() / "interact" / "prompts" / "installed.json"
    adoption = state.parent / "bootstrap-adoption.json"
    install_prompt_projection(projection, _consumer_home(), vscode_root, state, adoption)
    return projection


def _run(
    *arguments: str,
    repository: Path | None = None,
    output: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = ["git"]
    if repository is not None:
        command.extend(("-C", str(repository)))
    command.extend(arguments)
    environment = os.environ.copy()
    for key in ("GIT_ASKPASS", "SSH_ASKPASS", "GIT_BROWSER", "BROWSER"):
        environment.pop(key, None)
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""})
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, env=environment)
    if output and result.stdout:
        print(result.stdout, end="")
    if result.returncode and check:
        message = result.stderr.strip() or f"git exited {result.returncode}"
        print(f"ERROR: {message}", file=sys.stderr)
        raise SystemExit(result.returncode)
    return result


def _require_clean(repository: Path) -> None:
    status = _run(
        "status", "--porcelain=v1", "-z", repository=repository, output=False
    ).stdout
    if status:
        print("ERROR: prompt worktree has uncommitted changes or conflicts", file=sys.stderr)
        raise SystemExit(2)


def _source_parent(value: str) -> tuple[Path, int, str]:
    if not hasattr(os, "O_NOFOLLOW") or not all(
        function in os.supports_dir_fd for function in (os.open, os.rename, os.unlink)
    ):
        raise OSError("secure prompt source access is unavailable on this platform")
    relative = PurePosixPath(value)
    repository = _repository().resolve(strict=True)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("prompt path is outside the source worktree")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    parent = os.open(repository, flags)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, flags, dir_fd=parent)
            os.close(parent)
            parent = child
    except BaseException:
        os.close(parent)
        raise
    return repository.joinpath(*relative.parts), parent, relative.parts[-1]


def _source_file(value: str) -> tuple[Path, str]:
    target, parent, name = _source_parent(value)
    descriptor: int | None = None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("prompt path is not a regular source file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(_MAX_EDITOR_BYTES + 1)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)
    if len(data) > _MAX_EDITOR_BYTES:
        raise ValueError("prompt source is too large")
    try:
        content = data.decode("utf-8")
    except UnicodeError as error:
        raise ValueError("prompt path is not a regular source file")
    return target, content


def _editor_error(message: str, code: str = "invalid") -> None:
    print(json.dumps({"ok": False, "code": code, "error": message}, separators=(",", ":")))
    raise SystemExit(2)


@prompts_app.command
def catalog() -> None:
    """Return the finite editable prompt-source catalog as JSON."""
    server = PromptMode.server()
    if server is not None:
        try:
            files = [server.path(item.key) for item in server.catalog()]
        except (OSError, ValueError, httpx.HTTPError):
            _editor_error("Cannot read server prompt catalog; no local fallback.")
        print(json.dumps({"ok": True, "files": files}, separators=(",", ":")))
        return
    repository = _repository().resolve(strict=True)
    files: list[str] = []
    for target in sorted(repository.rglob("*")):
        if target.suffix.lower() not in {".md", ".json", ".yaml", ".yml"}:
            continue
        try:
            source, _ = _source_file(target.relative_to(repository).as_posix())
        except (OSError, UnicodeError, ValueError):
            continue
        files.append(source.relative_to(repository).as_posix())
    print(json.dumps({"ok": True, "files": files}, separators=(",", ":")))


@prompts_app.command
def read(path: str) -> None:
    """Read one allowlisted UTF-8 prompt source with its CAS digest as JSON."""
    server = PromptMode.server()
    try:
        if server is not None:
            content = server.read(path).content
        else:
            _, content = _source_file(path)
    except (OSError, UnicodeError, ValueError, httpx.HTTPError) as error:
        _editor_error(str(error) if server is None else "Cannot read server prompt; check path, access and connection. No local fallback.")
    digest = hashlib.sha256(content.encode()).hexdigest()
    print(json.dumps({"ok": True, "path": path, "content": content, "digest": digest}, separators=(",", ":")))


@prompts_app.command
def create(path: str, *, name: str | None = None) -> None:
    """Create a new prompt on the configured server; bounded UTF-8 content comes from stdin.

    PATH is namespace/slug.md, not a local file. --name defaults to the slug.
    Existing keys conflict; this command never creates or edits a local Git prompt source.
    """
    server = PromptMode.server()
    if server is None:
        _editor_error("Prompt creation requires a configured server; preserve the editor buffer. No local write was made.", "server_required")
    data = sys.stdin.buffer.read(_MAX_EDITOR_BYTES + 1)
    if len(data) > _MAX_EDITOR_BYTES:
        _editor_error("prompt source is too large; preserve the editor buffer")
    try:
        saved = server.create(path, data.decode("utf-8"), name=name)
    except PromptConflictError:
        _editor_error("server prompt already exists; preserve the editor buffer and read the current prompt before editing", "conflict")
    except CatalogConnectionError as error:
        _editor_error(f"{error}; preserve the editor buffer. Read the server prompt before retrying. No local write was made.")
    except (OSError, UnicodeError, ValueError, httpx.HTTPError):
        _editor_error("Cannot confirm server creation; preserve the editor buffer. Check UTF-8 content, path, name, access and connection; read the server prompt before retrying. No local write was made.")
    print(json.dumps({"ok": True, "source": "server", "path": path, "digest": saved.digest,
                      "revision": str(saved.revision)}, separators=(",", ":")))


@prompts_app.command
def write(path: str, digest: str) -> None:
    """CAS-write one prompt source; CONTENT is bounded UTF-8 on stdin."""
    data = sys.stdin.buffer.read(_MAX_EDITOR_BYTES + 1)
    if len(data) > _MAX_EDITOR_BYTES:
        _editor_error("prompt source is too large")
    server = PromptMode.server()
    if server is not None:
        try:
            saved = server.write(path, digest, data.decode("utf-8"))
        except PromptConflictError:
            _editor_error("server prompt changed; preserve the editor buffer and reload", "conflict")
        except CatalogConnectionError as error:
            _editor_error(f"{error}; preserve the editor buffer. No local write was made.")
        except (OSError, UnicodeError, ValueError, httpx.HTTPError):
            _editor_error("Server save failed; preserve the editor buffer. Check access and connection, or save in the signed-in server prompt editor. No local write was made.")
        print(json.dumps({"ok": True, "path": path, "digest": saved.digest}, separators=(",", ":")))
        return
    parent: int | None = None
    descriptor: int | None = None
    temporary: str | None = None
    lock_name: str | None = None
    owns_lock = False
    try:
        content = data.decode("utf-8")
        target, parent, name = _source_parent(path)
        lock_name = f".{name}.interact.lock"
        try:
            descriptor = os.open(lock_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=parent)
            owns_lock = True
        except FileExistsError:
            _editor_error("prompt source has a competing editor; preserve the buffer and reload", "conflict")
        current_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(current_fd, "rb") as stream:
            current = stream.read(_MAX_EDITOR_BYTES + 1)
        if hashlib.sha256(current).hexdigest() != digest:
            _editor_error("prompt source changed; preserve the editor buffer and reload", "conflict")
        temporary = f".{name}.interact-{os.getpid()}"
        temporary_fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=parent)
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    except (OSError, UnicodeError, ValueError) as error:
        _editor_error(str(error))
    finally:
        if parent is not None and temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None and lock_name is not None and owns_lock:
            try:
                os.unlink(lock_name, dir_fd=parent)
            except FileNotFoundError:
                pass
        if parent is not None:
            os.close(parent)
    print(json.dumps({"ok": True, "path": path, "digest": hashlib.sha256(data).hexdigest()}, separators=(",", ":")))


@prompts_app.command
def clone(remote: str) -> None:
    """Clone REMOTE into the local prompt authoring worktree."""
    PromptMode.require_local()
    repository = _repository()
    if repository.exists():
        print(f"ERROR: prompt worktree already exists: {repository}", file=sys.stderr)
        raise SystemExit(2)
    repository.parent.mkdir(parents=True, exist_ok=True)
    _run("clone", "--", remote, str(repository))


@prompts_app.command
def status() -> None:
    """Show server sync state when configured, otherwise local Git state."""
    server = PromptMode.server()
    if server is not None:
        try:
            result = server.status()
        except (OSError, ValueError, httpx.HTTPError):
            _editor_error("Cannot read server prompt sync status; no local fallback.")
        print(json.dumps({"ok": True, "source": "server", "sync": result.model_dump(mode="json")}, separators=(",", ":")))
        return
    _run("status", "--short", "--branch", repository=_repository())


@prompts_app.command
def diff() -> None:
    """Show current server revisions when configured, otherwise local Git changes."""
    server = PromptMode.server()
    if server is not None:
        PromptMode.revisions(server)
        return
    _run("diff", "--no-ext-diff", "HEAD", "--", repository=_repository())


@prompts_app.command
def commit(message: Annotated[str, Parameter(name=["--message", "-m"])]) -> None:
    """Commit all authored prompt changes with MESSAGE."""
    PromptMode.require_local()
    repository = _repository()
    conflicts = _run("diff", "--name-only", "--diff-filter=U", repository=repository).stdout
    if conflicts:
        print("ERROR: resolve every conflict before committing", file=sys.stderr)
        raise SystemExit(2)
    identity = _run("var", "GIT_AUTHOR_IDENT", repository=repository, output=False, check=False)
    if identity.returncode:
        _editor_error(
            "Git author identity is unavailable; configure user.name and user.email, then commit again.",
            "git_identity",
        )
    _run("add", "-A", "--", ".", repository=repository)
    _run("commit", "-m", message, repository=repository)


@prompts_app.command
def log() -> None:
    """Show current server revision metadata, or local Git history when unconfigured."""
    server = PromptMode.server()
    if server is not None:
        PromptMode.revisions(server)
        return
    _run(
        "log", "--date=iso-strict", "--decorate", "--graph",
        "--pretty=format:%H%x09%aI%x09%P%x09%s", repository=_repository(),
    )


@prompts_app.command
def pull() -> None:
    """Merge from the configured remote without discarding local work."""
    PromptMode.require_local()
    repository = _repository()
    _require_clean(repository)
    _run("pull", "--no-rebase", "--no-edit", repository=repository)


@prompts_app.command
def push() -> None:
    """Push local prompt commits using ordinary Git conflict protection."""
    PromptMode.require_local()
    repository = _repository()
    upstream = _run(
        "rev-parse", "--abbrev-ref", "@{upstream}", repository=repository,
        output=False, check=False,
    )
    if upstream.returncode:
        _run("push", "--set-upstream", "origin", "HEAD", repository=repository)
        return
    _run("push", repository=repository)


@prompts_app.command
def resolve(*paths: str) -> None:
    """Stage only named conflicted prompt source PATHS after manual resolution."""
    PromptMode.require_local()
    repository = _repository()
    if not paths:
        print("ERROR: name at least one conflicted source path", file=sys.stderr)
        raise SystemExit(2)
    conflicted = set(
        _run(
            "diff", "--name-only", "--diff-filter=U", "-z",
            repository=repository, output=False,
        )
        .stdout.rstrip("\0").split("\0")
    )
    for value in paths:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value not in conflicted:
            print(f"ERROR: not an allowlisted conflicted source path: {value}", file=sys.stderr)
            raise SystemExit(2)
        if (repository / path).is_symlink():
            print(f"ERROR: conflicted source must not be a symlink: {value}", file=sys.stderr)
            raise SystemExit(2)
    _run("add", "--", *paths, repository=repository)


@prompts_app.command
def compile() -> None:
    """Compile server prompts when configured, otherwise the clean local Git HEAD."""
    server = PromptMode.server()
    print(_compile(_repository()) if server is None else PromptMode.project(server, install=False))


@prompts_app.command
def install() -> None:
    """Atomically install the exact compiled projection into managed consumers."""
    server = PromptMode.server()
    print(_install(_repository()) if server is None else PromptMode.project(server, install=True))


@prompts_app.command
def scope(name: str, project: Path = Path(".")) -> None:
    """Link one installed domain scope (its agents + skills) into a project's .claude/."""
    held = _consumer_home() / ".claude" / "scopes" / name
    if not held.is_dir():
        print(f"ERROR: scope {name!r} is not installed under {held}", file=sys.stderr)
        raise SystemExit(2)
    root = project.resolve() / ".claude"
    linked = 0
    for source in sorted(held.rglob("*.md")):
        relative = source.relative_to(held)
        target = root / relative
        if target.exists() or target.is_symlink():
            if target.is_symlink() and target.resolve() == source.resolve():
                continue
            print(f"ERROR: {target} exists and is not this scope's link", file=sys.stderr)
            raise SystemExit(2)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source)
        linked += 1
    print(f"scope {name}: {linked} link(s) into {root}")


@prompts_app.command
def publish(endpoint: str, token_file: Path) -> None:
    """Publish the clean, pushed exact HEAD to an authenticated prompt service."""
    PromptMode.require_local()
    repository = _repository()
    _require_clean(repository)
    head = _run("rev-parse", "HEAD", repository=repository, output=False).stdout.strip()
    upstream = _run(
        "rev-parse", "@{upstream}", repository=repository, output=False
    ).stdout.strip()
    if head != upstream:
        print("ERROR: prompt HEAD is not exactly pushed to its upstream", file=sys.stderr)
        raise SystemExit(2)
    token = read_prompt_token(token_file)
    publish_projection(_compile(repository), endpoint, token)
    print(head)


@prompts_app.command
def sync() -> None:
    """Refresh installed server caches when configured, otherwise pull and install local Git."""
    server = PromptMode.server()
    if server is not None:
        print(PromptMode.project(server, install=True))
        return
    repository = _repository()
    _require_clean(repository)
    _run("pull", "--no-rebase", "--no-edit", repository=repository)
    print(_install(repository))
