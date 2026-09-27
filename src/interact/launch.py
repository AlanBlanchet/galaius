"""Command rewriting + sizing for ``launch_app``, apart from the MCP tool module.

Each rewriter shares one job shape: inspect the command's executable, and if it matches a known
class, inject flags and return a note explaining what was added and why — the note reaches the
agent, so a launch that behaves unexpectedly explains itself. ``apply_launch_rewrites`` threads a
command through all of them.

The isolating rewriters (`_browser_isolate`, `_editor_isolate`) also PREPARE the profile they
point at — creating it and clearing a lock left by a dead process — so they touch the filesystem
rather than being pure argv transforms.
"""

import os
import re
from pathlib import Path

from interact.desktop.orphans import process_alive

_DEVICE_SIZES = {
    "phone": "412x915",
    "tablet": "820x1180",
    "desktop": "1280x800",
}
_SIZE_RE = re.compile(r"^\d{2,5}x\d{2,5}$")

# Browser executables whose default launch joins an ALREADY-RUNNING instance via a profile
# singleton (Chrome's SingletonLock, Firefox's remoting). Inside the sandbox that's fatal: the URL
# opens on the user's REAL desktop browser, the sandboxed process exits, and the agent (and user)
# is left with an empty Xephyr window. Matched on the executable basename.
_CHROMIUM_BROWSERS = ("chrome", "chromium", "brave", "edge", "vivaldi", "opera")
_FIREFOX_BROWSERS = ("firefox", "librewolf", "waterfox")


# Shell syntax raw exec can't honor: agents naturally write `cd <repo> && uv run app`, which
# exec'd verbatim fails with the cryptic `[Errno 2] No such file or directory: 'cd'`.
_SHELL_MARKERS = ("&&", "||", ";", "|", ">", "<", "$(", "`")


def needs_shell(command: str) -> bool:
    """True when a launch command uses shell syntax (`cd X && app`, pipes, redirects, command
    substitution) that must run via ``bash -c`` rather than raw exec. A quoted argument merely
    CONTAINING a marker also routes through bash — harmless, bash parses quotes identically."""
    return command.lstrip().startswith("cd ") or any(m in command for m in _SHELL_MARKERS)


def _resolve_nested_size(size: str | None, device: str | None) -> tuple[str | None, str | None]:
    """Pick the nested display size for a launch: explicit ``size`` ("WxH") wins, then a ``device``
    profile, else None → the caller keeps the configured default. Returns (size_or_None, error)."""
    if size:
        norm = size.strip().lower()
        if not _SIZE_RE.match(norm):
            return None, f"ERROR: size must be WxH (e.g. 412x915), got {size!r}"
        return norm, None
    if device:
        key = device.strip().lower()
        if key not in _DEVICE_SIZES:
            opts = ", ".join(_DEVICE_SIZES)
            return None, f"ERROR: unknown device {device!r} — use one of: {opts}, or pass size=WxH"
        return _DEVICE_SIZES[key], None
    return None, None


# A POSIX shell variable assignment (`FOO=bar`) — the one token shape a simple command may carry
# before its executable. `_argv_executable` skips them, `split_env_assignments` peels them (#117).
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _argv_executable(argv: list[str]) -> str | None:
    """The executable token in a command, skipping an ``env`` prefix and its ``VAR=value`` pairs —
    so ``env LANG=C google-chrome`` resolves to ``google-chrome``. Shared by the launch rewriters."""
    return next((t for t in argv if t != "env" and not _ENV_ASSIGNMENT_RE.match(t)), None)


def split_env_assignments(argv: list[str]) -> tuple[dict[str, str], list[str]]:
    """Peel the leading ``VAR=value`` tokens off a command into the environment they mean.

    ``FOO=bar app --x`` is shell phrasing for "run ``app --x`` with FOO set", but carries no
    shell marker, so it reached exec verbatim and ``Popen(["FOO=bar", "app"])`` died with
    ``FileNotFoundError: 'FOO=bar'`` (#117). Splitting it here keeps the command on the exec
    path, so launch rewrites still apply (a ``bash -c`` launch would bypass them). An explicit
    ``env`` prefix is left whole — the ``env`` binary applies its own assignments. Returns
    ``(env, argv)``; an assignments-only command leaves argv empty for the caller to refuse."""
    env: dict[str, str] = {}
    rest = list(argv)
    while rest and _ENV_ASSIGNMENT_RE.match(rest[0]):
        name, value = rest.pop(0).split("=", 1)
        env[name] = value
    return env, rest


def _flutter_software_render(argv: list[str]) -> tuple[list[str], str]:
    """A Flutter Linux bundle's GPU compositing — notably a `BackdropFilter`/blur (a
    `ConvexAppBar` blurred bottom bar) — renders as a solid black strip under the sandbox's
    software GL (llvmpipe), so the nav is invisible and untappable (#28). Flutter's Skia CPU
    rasteriser bypasses GL entirely and renders it correctly, so add
    `--enable-software-rendering` for a detected Flutter bundle. Idempotent; no-op for
    non-Flutter commands. Returns (argv, note-for-the-result)."""
    if "--enable-software-rendering" in argv:
        return argv, ""
    exe = _argv_executable(argv)
    if not exe:
        return argv, ""
    try:
        bundle = Path(exe).resolve().parent
    except (OSError, RuntimeError):
        return argv, ""
    is_flutter = (bundle / "data" / "flutter_assets").is_dir() or (
        bundle / "lib" / "libflutter_linux_gtk.so"
    ).exists()
    if not is_flutter:
        return argv, ""
    return (
        [*argv, "--enable-software-rendering"],
        " (added --enable-software-rendering: a Flutter bundle's blur renders black under the "
        "sandbox's software GL, so its Skia CPU rasteriser is used instead)",
    )


def _browser_isolate(argv: list[str], display: str) -> tuple[list[str], str]:
    """Give a known browser command a sandbox-local profile so it starts a REAL instance inside
    the sandbox instead of delegating to the user's running browser (the singleton escape
    above). Profile dir is stable per (display, browser): a relaunch reuses it and may join the
    in-sandbox instance — isolated, so that's correct. A caller who already picked a profile
    (--user-data-dir / --profile / -P) is left alone. Returns (argv, note-for-the-result)."""
    exe = _argv_executable(argv)
    if not exe:
        return argv, ""
    base = Path(exe).name.lower()
    is_chromium = any(b in base for b in _CHROMIUM_BROWSERS)
    is_firefox = any(b in base for b in _FIREFOX_BROWSERS)
    if not (is_chromium or is_firefox):
        return argv, ""
    if any(a.startswith("--user-data-dir") or a in ("--profile", "-P", "--no-remote") for a in argv):
        return argv, ""  # caller chose its own isolation
    profile = (
        Path.home() / ".interact" / "out" / "sandbox-profiles" / f"{display.lstrip(':')}-{base}"
    )
    _prepare_profile(profile)
    exe_i = argv.index(exe)
    if is_chromium:
        inject = [f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check"]
    else:
        inject = ["--no-remote", "--profile", str(profile)]
    note = (
        " (added an isolated profile: without it the browser just signals the user's RUNNING "
        "instance — the page opens on the real desktop and nothing appears in the sandbox)"
    )
    return [*argv[: exe_i + 1], *inject, *argv[exe_i + 1:]], note


# Electron editors with a SINGLETON: launching one while an instance is already running hands
# the request to that instance, opening a window on the USER'S desktop. The sandbox then sits
# empty with no error — the same escape `_browser_isolate` closes for browsers, just as
# invisible. Matched on the executable basename.
_EDITORS = ("code", "code-insiders", "codium", "vscodium", "cursor", "windsurf")


#: Singleton locks an editor profile can carry. `code.lock` holds a pid as text; Chromium's
#: `SingletonLock` is a symlink named ``<host>-<pid>``.
_PROFILE_LOCKS = ("code.lock", "SingletonLock")


def _lock_owner(lock: Path) -> int | None:
    """The pid a lock claims, or None when it does not name one we can read."""
    try:
        raw = os.readlink(lock) if lock.is_symlink() else lock.read_text()
    except OSError:
        return None
    try:
        return int(raw.strip().rsplit("-", 1)[-1])
    except ValueError:
        return None


def sandbox_profiles(display: str) -> list[Path]:
    """Every profile this display's launches may have created — what teardown must release."""
    root = Path.home() / ".interact" / "out" / "sandbox-profiles"
    number = display.lstrip(":")
    try:
        return [p for p in root.iterdir()
                if p.is_dir() and (p.name == f"editor-{number}" or p.name.startswith(f"{number}-"))]
    except OSError:
        return []


def _prepare_profile(profile: Path) -> None:
    """Make a sandbox profile usable before an app is pointed at it.

    Every isolated launch goes through here — browser and editor alike — so a fix to one can't
    silently miss the other; clearing the lock only for editors left Chromium (what
    `SingletonLock` is actually named after) still broken.
    """
    profile.mkdir(parents=True, exist_ok=True)
    _clear_stale_locks(profile)


def _clear_stale_locks(profile: Path) -> None:
    """Remove a singleton lock whose owning process is gone.

    The profile is keyed per DISPLAY and OUTLIVES it: tearing the sandbox down takes the
    editor's processes but leaves the lock file naming a pid that no longer exists. The next
    launch then finds a lock it can't join and exits without ever mapping a window — the
    sandbox just looks empty, nothing in any log says why.

    Only a lock we can PROVE is dead is removed; an unreadable or unparseable one is left alone.
    """
    for name in _PROFILE_LOCKS:
        lock = profile / name
        if not (lock.is_symlink() or lock.exists()):
            continue
        pid = _lock_owner(lock)
        if pid is None or process_alive(pid):
            continue
        try:
            lock.unlink()
        except OSError:
            pass  # a lock we cannot remove is the editor's problem to report, not ours to crash on


def _editor_isolate(argv: list[str], display: str) -> tuple[list[str], str]:
    """Make an Electron editor start a REAL instance inside the sandbox, and render there.

    Three flags, each closing a distinct failure:

    * ``--user-data-dir`` — its own profile, so it cannot join the running instance and open on
      the host desktop. Keyed per DISPLAY so two sandboxes never fight over one profile lock.
    * ``--disable-gpu`` — a nested X display has no usable hardware GL, so an Electron app that
      tries it paints a black window.
    * ``--no-sandbox`` — Electron's own sandbox needs user namespaces that a nested/containerised
      session often lacks; without this it refuses to start at all.

    Idempotent, and a no-op for anything that is not one of these editors.
    """
    exe = _argv_executable(argv)
    if not exe or Path(exe).name.lower() not in _EDITORS:
        return argv, ""
    if any(a.startswith("--user-data-dir") for a in argv):
        return argv, ""  # already isolated — never stack a second profile
    # Same home as the browser profiles, so all sandbox state lives in one place a user
    # can inspect or delete.
    profile = Path.home() / ".interact" / "out" / "sandbox-profiles" / f"editor-{display.lstrip(':')}"
    _prepare_profile(profile)
    # A fresh profile means FIRST-RUN state: the welcome walkthrough, the workspace-trust modal,
    # release notes, an extension's sign-in prompt. Each is a modal that swallows the very
    # keystrokes an agent sends next, so the editor looks unresponsive for reasons that have
    # nothing to do with the task. Suppress them so the sandbox opens ready to drive.
    return (
        [*argv,
         f"--user-data-dir={profile}",
         "--disable-gpu",
         "--no-sandbox",
         "--skip-welcome",
         "--skip-release-notes",
         "--disable-workspace-trust",
         "--disable-telemetry",
         "--disable-updates"],
        f" (isolated the editor into its own profile at {profile}: launching it otherwise hands "
        "the window to your already-running instance on the real desktop, and the sandbox stays "
        "empty)",
    )


def apply_launch_rewrites(argv: list[str], display: str) -> tuple[list[str], str]:
    """Run every launch rewriter over a command, threading the argv through each and concatenating
    their notes. The one place launch_app calls to prepare a command for the sandbox."""
    note = ""
    argv, n = _flutter_software_render(argv)
    note += n
    argv, n = _browser_isolate(argv, display)
    note += n
    argv, n = _editor_isolate(argv, display)
    note += n
    return argv, note
