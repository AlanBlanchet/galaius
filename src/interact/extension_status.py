"""Is the VS Code extension the user is running the one in this tree?

The MCP half of this question lives in :mod:`interact.server_registry`. This is the other half,
and the project's own notes record why it needs one: ``code <path>`` against a RUNNING editor
hands off to that instance's singleton, so the new window is served by the OLD extension host and
reinstalling at the same version never reaches it. `interact doctor` answered the question for
servers and said nothing about the extension — half the delivery gate missing.

Beyond a version mismatch, two same-version states go stale: installed compiled bytes can differ
from this tree, or a running editor can predate an otherwise-current installed build. First needs
installation; second needs a restart. Both common during development — exactly when this
diagnostic matters.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

from interact.server_registry import _process_start

#: VS Code installs an extension as `<publisher>.<name>-<version>`. Both halves already live in
#: the manifest this module reads, so they are derived rather than spelled a second time here.
def _dir_re() -> re.Pattern[str]:
    manifest = _manifest()
    publisher = (manifest.get("publisher") or "alanblanchet").lower()
    name = (manifest.get("name") or "interact").lower()
    return re.compile(rf"^{re.escape(publisher)}\.{re.escape(name)}-(\d+\.\d+\.\d+)$", re.IGNORECASE)


def _extensions_dir() -> Path:
    return Path.home() / ".vscode" / "extensions"


def _manifest() -> dict:
    pkg = Path(__file__).resolve().parents[4] / "clients" / "vscode" / "package.json"
    try:
        return json.loads(pkg.read_text())
    except (OSError, ValueError):
        return {}


def _tree_version() -> str | None:
    return _manifest().get("version")


_EDITOR_EXES = {"code", "code-insiders", "codium", "vscodium", "code-server", "electron"}


def _is_editor_cmdline(raw: bytes) -> bool:
    """Whether this command line is an editor WINDOW rather than one of its helpers.

    Two things masquerade as the main process. Electron helpers carry `--type=`, and several
    don't NUL-separate their argv at all, so the flag must be matched against the whole blob.
    Language servers are subtler: VS Code runs pylance, tsserver, copilot and friends through the
    SAME binary with ELECTRON_RUN_AS_NODE and no `--type=`, so "basename is code, no --type="
    once counted fourteen editors for one open window. Told apart by their first argument being a
    script to run.
    """
    if not raw or b"--type=" in raw:
        return False
    parts = [p for p in raw.split(b"\0") if p]
    if not parts:
        return False
    exe = Path(parts[0].split(b" ")[0].decode(errors="ignore")).name.lower()
    if exe not in _EDITOR_EXES:
        return False
    # `code /path/to/server.js --stdio` is a language server wearing the editor's binary.
    return not any(arg.endswith(b".js") for arg in parts[1:])


def _iter_proc():
    """Every /proc entry, or a FileNotFoundError where /proc does not exist."""
    return Path("/proc").iterdir()


def _editor_starts() -> list[float]:
    """Start times of every running VS Code MAIN process.

    Electron helpers must be excluded — restarted freely, say nothing about which extension host
    is loaded. Harder than it looks: some don't NUL-separate their argv at all, so the whole
    command line arrives as one element and a `--type=` check over argv[1:] sees nothing. Match
    against the raw blob instead.
    """
    out: list[float] = []
    try:
        entries = list(_iter_proc())
    except OSError:
        return []  # no /proc: macOS, Windows. Unknown, not a crash — this is a diagnostic.
    for proc in entries:
        if not proc.name.isdigit():
            continue
        try:
            raw = (proc / "cmdline").read_bytes()
        except OSError:
            continue
        if not _is_editor_cmdline(raw):
            continue
        started = _process_start(int(proc.name))
        if started is not None:
            out.append(started)
    return out


def _build_mtime(ext: Path) -> float:
    newest = 0.0
    for f in ext.rglob("*.js"):
        try:
            newest = max(newest, f.stat().st_mtime)
        except OSError:
            continue
    return newest


def _compiled_bundle_fingerprint(ext: Path) -> str | None:
    """A deterministic digest of the files VS Code can load from ``out/``.

    Source maps are excluded by ``clients/vscode/.vscodeignore``, so can't be part of an
    installed package; everything else below ``out/`` is package payload. Rejecting a missing,
    changing, unreadable, or symlinked payload is deliberate: treating an unprovable bundle as
    current would recreate the false-green this diagnostic exists to prevent, while following a
    hostile symlink could read content outside the extension.
    """
    compiled = ext / "out"
    try:
        if compiled.is_symlink() or not compiled.is_dir():
            return None
        files = []
        for path in compiled.rglob("*"):
            if path.is_symlink():
                return None
            if path.is_dir():
                continue
            if not path.is_file():
                return None
            if path.suffix != ".map":
                files.append(path)
    except OSError:
        return None
    if not files:
        return None

    digest = hashlib.sha256(b"interact-vscode-compiled-bundle-v1\0")
    files.sort(key=lambda path: path.relative_to(compiled).as_posix())
    digest.update(len(files).to_bytes(8, "big"))
    for path in files:
        relative = path.relative_to(compiled).as_posix().encode()
        content = hashlib.sha256()
        try:
            before = path.stat(follow_symlinks=False)
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    content.update(chunk)
            after = path.stat(follow_symlinks=False)
        except OSError:
            return None
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            return None
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(before.st_size.to_bytes(8, "big"))
        digest.update(content.digest())
    return digest.hexdigest()


def extension_status() -> dict | None:
    """What is stale about the installed extension, or ``None`` when nothing is.

    Returns ``{"installed", "tree", "reason", "remedy"}``. Reason is ``"version"`` for an older
    installed version or ``"code"`` for same-version drift. Remedy is ``"install"`` when the
    installed payload must be replaced and ``"restart"`` when only a running host predates it.
    This distinction is operational: restarting cannot repair different bytes on disk, while
    reinstalling an already-current bundle needlessly disrupts extension hosts.
    """
    d = _extensions_dir()
    installed: list[tuple[str, Path]] = []
    pattern = _dir_re()
    try:
        for child in d.iterdir():
            m = pattern.match(child.name)
            if m:
                installed.append((m.group(1), child))
    except OSError:
        return None
    if not installed:
        return None  # MCP-only user; not a problem to report

    installed.sort(key=lambda p: [int(n) for n in p[0].split(".")])
    version, path = installed[-1]
    tree = _tree_version()
    if tree and version != tree:
        return {"installed": version, "tree": tree, "reason": "version", "remedy": "install"}

    installed_fingerprint = _compiled_bundle_fingerprint(path)
    tree_fingerprint = _compiled_bundle_fingerprint(_extension_dir())
    if (
        installed_fingerprint is None
        or tree_fingerprint is None
        or installed_fingerprint != tree_fingerprint
    ):
        return {
            "installed": version,
            "tree": tree or version,
            "reason": "code",
            "remedy": "install",
        }

    built = _build_mtime(path)
    starts = _editor_starts()
    behind = [s for s in starts if s < built]
    if built and behind:
        # Not all of them, usually: windows opened since the rebuild are fine. Say how many are
        # not, because "your editor is stale" when five of eight are current is its own confusion.
        return {
            "installed": version,
            "tree": tree or version,
            "reason": "code",
            "remedy": "restart",
            "behind": len(behind),
            "running": len(starts),
        }
    return None


def _extension_dir() -> Path:
    """The extension SOURCE in this checkout — where the package is built from."""
    return Path(__file__).resolve().parents[4] / "clients" / "vscode"


def _run(argv: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """Run a build/install step, returning (code, output). Kept tiny and injectable so the
    delivery logic can be tested without packaging a real extension."""
    import subprocess

    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=600)
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)



def _install_vsix(vsix: Path, version: str) -> bool:
    """Unpack a packaged extension into the extensions directory, in place of `code`.

    `code --install-extension` asks VS Code to reload its extension hosts, and that reload kills
    whatever session requested it — exiting 0 through the graceful path, so it presents as an
    unexplained crash rather than a consequence. Perverse effect: the single step that DELIVERS
    the work destroys the context doing it, so it keeps being postponed and the work stays
    invisible — not hypothetical; it's how a full day of changes once sat on disk unseen.

    A .vsix is a zip whose payload lives under `extension/`. Writing those bytes into
    `<extensions>/alanblanchet.interact-<version>/` is what the editor would have done anyway,
    minus the reload signal: a NEW window loads it, running windows keep the frozen snapshot they
    were always going to keep. Replace rather than merge, so nothing survives from the version
    being overwritten — a leftover file is one the host will happily load.
    """
    target = _extensions_dir() / f"alanblanchet.interact-{version}"
    staging = target.with_name(target.name + ".incoming")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        with zipfile.ZipFile(vsix) as z:
            members = [m for m in z.namelist() if m.startswith("extension/") and not m.endswith("/")]
            if not members:
                print(f"{vsix.name} carries no extension/ payload")
                return False
            for m in members:
                dest = staging / Path(m).relative_to("extension")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    except (zipfile.BadZipFile, OSError) as e:
        shutil.rmtree(staging, ignore_errors=True)  # never leave a half-unpacked extension behind
        print(f"could not unpack {vsix.name}: {e}")
        return False

    # Swap only once the payload is complete, so a crash mid-unpack cannot leave a broken install.
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    return True


def deliver_extension() -> bool:
    """Rebuild and install the extension when the installed one is older than this tree.

    `interact doctor` has always been able to SAY the installed extension was stale and do
    nothing about it — a detector for a condition you can remedy is half a feature. A whole day's
    work once sat undelivered behind exactly that warning: the artifact on disk predated every
    change, so even a brand-new window showed the old product while every test passed.

    NOT automatic, deliberately — rebuilds and replaces what the editor loads, so it runs only
    when somebody explicitly asks. No longer shells out to `code --install-extension`: that asks
    VS Code to reload its extension hosts and kills the session that requested it, precisely why
    delivery kept being postponed. See `_install_vsix`.

    Returns True only when a new package was actually installed; a failure at any step returns
    False and says why — the failure this exists to prevent IS an unverified delivery.
    """
    status = extension_status()
    if not status:
        return False  # already current: a needless reinstall costs every window its host

    where = _extension_dir()
    code, out = _run(["npm", "run", "package"], cwd=where)
    if code != 0:
        print(f"could not package the extension: {out.strip()[-400:]}")
        return False

    vsix = sorted(where.glob("interact-*.vsix"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not vsix:
        print("packaging reported success but produced no .vsix")
        return False

    version = _tree_version()
    if not version:
        print("could not read the version from the extension manifest")
        return False
    if not _install_vsix(vsix[0], version):
        return False
    print(
        f"installed {vsix[0].name}. Running windows keep the code they started with — "
        "fully close and reopen one to pick this up (a reload is served by the same host)."
    )
    return True
