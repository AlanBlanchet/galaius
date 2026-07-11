"""Command rewriting + sizing for ``launch_app`` — pure helpers with no dependency on the MCP
server or its runtime state, so they live apart from the 2000-line tool module.

Two launch rewriters (`_flutter_software_render`, `_browser_isolate`) share the same job shape:
inspect the command's executable, and if it matches a known class, inject flags + return a note.
They compose as ``LAUNCH_REWRITES`` — a third rewriter is one more entry, no call-site edit.
"""

import re
from pathlib import Path

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


def _argv_executable(argv: list[str]) -> str | None:
    """The executable token in a command, skipping an ``env`` prefix and its ``VAR=value`` pairs —
    so ``env LANG=C google-chrome`` resolves to ``google-chrome``. Shared by the launch rewriters."""
    return next((t for t in argv if t != "env" and not re.match(r"^\w+=", t)), None)


def _flutter_software_render(argv: list[str]) -> tuple[list[str], str]:
    """A Flutter Linux bundle's GPU compositing — notably a `BackdropFilter`/blur (a `ConvexAppBar`
    blurred bottom bar) — renders as a solid black strip under the sandbox's software GL (llvmpipe),
    so the nav is invisible and untappable (#28). Flutter's Skia CPU rasteriser bypasses GL entirely
    and renders it correctly, so add `--enable-software-rendering` for a detected Flutter bundle.
    Idempotent; a no-op for non-Flutter commands. Returns (argv, note-for-the-result)."""
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
    """Give a known browser command a sandbox-local profile so it starts a REAL instance inside the
    sandbox instead of delegating to the user's running browser (the singleton escape above).
    The profile dir is stable per (display, browser): a relaunch reuses it and may join the
    in-sandbox instance — which is isolated, so that's correct. A caller who already picked a
    profile (--user-data-dir / --profile / -P) is left alone. Returns (argv, note-for-the-result)."""
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
    profile.mkdir(parents=True, exist_ok=True)
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


def apply_launch_rewrites(argv: list[str], display: str) -> tuple[list[str], str]:
    """Run every launch rewriter over a command, threading the argv through each and concatenating
    their notes. The one place launch_app calls to prepare a command for the sandbox."""
    note = ""
    argv, n = _flutter_software_render(argv)
    note += n
    argv, n = _browser_isolate(argv, display)
    note += n
    return argv, note
