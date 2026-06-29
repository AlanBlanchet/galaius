"""Track running MCP servers so the CLI can flag a STALE one.

A long-lived ``interact mcp`` process keeps the CODE it imported at startup. After the package
advances (an editable checkout especially — source is live but a running process is not), that
server silently serves OLD code until it is reconnected. This is the trap behind "I shipped the
fix but the bug persists": the maintainer's fix is committed, yet the editor's long-lived MCP
server still runs the pre-fix code.

Each server records its pid + version under ``<debug_dir>/runtime/`` at startup; ``interact
doctor``/``status`` read these, prune dead pids, and flag any LIVE server whose version is behind
the latest — naming the pid so the user knows exactly which editor connection to reconnect.
"""

import json
import os
from pathlib import Path

from interact import installed_version


def _runtime_dir() -> Path:
    """A FIXED well-known path (``~/.interact/out/runtime``), NOT ``debug_dir``-relative. This is
    cross-process IPC — servers announce here, the CLI reads here — so every interact process must
    agree on the path regardless of an ``INTERACT_DEBUG_DIR`` override (that relocates debug OUTPUT,
    not runtime state; ``config.env`` likewise always lives at the fixed ``~/.interact``)."""
    return Path.home() / ".interact" / "out" / "runtime"


def _source_version() -> str | None:
    """The version in the source tree's pyproject.toml when interact runs from an editable checkout
    (``src/interact/`` two levels under the repo root). None for a wheel install — there the
    installed metadata IS the latest, so no drift is possible without a reinstall+restart."""
    try:
        import tomllib

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        if pyproject.exists():
            return tomllib.loads(pyproject.read_text()).get("project", {}).get("version")
    except Exception:
        pass
    return None


def latest_version() -> str:
    """The newest interact version available on this machine: the editable SOURCE version if higher
    than the installed metadata (the source advanced without a reinstall), else the installed one."""
    return _source_version() or installed_version()


def register_server() -> Path | None:
    """Record this MCP server's pid + the version it loaded. Best-effort; returns the file to remove."""
    try:
        d = _runtime_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{os.getpid()}.json"
        path.write_text(json.dumps({"pid": os.getpid(), "version": installed_version()}))
        return path
    except OSError:
        return None


def unregister_server(path: Path | None) -> None:
    if path:
        try:
            path.unlink()
        except OSError:
            pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user — still running
    return True


def stale_servers() -> list[dict]:
    """Live MCP servers whose loaded version is behind :func:`latest_version` (→ serving old code;
    reconnect them). Prunes registry files for dead pids as a side effect, so a crashed server
    doesn't linger as a false positive."""
    d = _runtime_dir()
    if not d.exists():
        return []
    latest = latest_version()
    out: list[dict] = []
    for f in d.glob("*.json"):
        try:
            info = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        pid = info.get("pid")
        if not isinstance(pid, int) or not _alive(pid):
            f.unlink(missing_ok=True)  # dead → prune
            continue
        if info.get("version") != latest:
            out.append(info)
    return out
