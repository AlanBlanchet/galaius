"""The sandbox must actually CONTAIN what it launches.

Two escapes were reported from real sessions, both silent:

- #85: a Qt/GTK app inherited the HOST's ``WAYLAND_DISPLAY``, so it connected to the user's real
  Wayland compositor instead of the sandbox's Xephyr — the process ran fine and NO window ever
  appeared in the sandbox, indistinguishable from a slow start.
- #83: a sandboxed app's ``webbrowser.open()`` / ``xdg-open`` navigated the user's REAL, already-
  open Chrome window away from what they were reading.

Both are fixed in the env the sandbox hands its children, so both are tested on the pure
env-builder — no X server needed.
"""

import os
import shutil
import subprocess

import pytest

from interact.desktop.backend import (
    _URL_OPENER_NAMES,
    _WAYLAND_ESCAPE_VARS,
    sandbox_child_env,
    write_sandbox_url_shims,
)


HOST = {
    "PATH": "/usr/bin",
    "HOME": "/home/someone",
    "DISPLAY": ":0",
    "WAYLAND_DISPLAY": "wayland-0",
    "GDK_BACKEND": "wayland",
    "QT_QPA_PLATFORM": "wayland",
    "XDG_SESSION_TYPE": "wayland",
    "XDG_CURRENT_DESKTOP": "GNOME",
    "DESKTOP_SESSION": "gnome",
    "KDE_FULL_SESSION": "true",
    "GNOME_DESKTOP_SESSION_ID": "this-is-deprecated",
    "MOZ_ENABLE_WAYLAND": "1",
    "SDL_VIDEODRIVER": "wayland",
    "BROWSER": "google-chrome",
}


def test_the_sandbox_display_replaces_the_hosts(tmp_path):
    env = sandbox_child_env(HOST, ":99", shim_dir=str(tmp_path))
    assert env["DISPLAY"] == ":99"


@pytest.mark.parametrize("var", sorted(_WAYLAND_ESCAPE_VARS))
def test_every_wayland_escape_var_is_scrubbed(var, tmp_path):
    """#85: any of these left set lets a toolkit auto-detect the HOST session and render there.
    Scrubbed as a CLASS — a new toolkit's variable is one entry, not a new bug."""
    env = sandbox_child_env(HOST, ":99", shim_dir=str(tmp_path))
    assert var not in env, f"{var} escaped into the sandbox child's environment"


def test_toolkits_are_pinned_to_x11(tmp_path):
    """Scrubbing alone leaves auto-detection to chance; pin the two big toolkits explicitly so a
    Qt/GTK app cannot pick anything but the sandbox's X display (#85)."""
    env = sandbox_child_env(HOST, ":99", shim_dir=str(tmp_path))
    assert env["QT_QPA_PLATFORM"] == "xcb"
    assert env["GDK_BACKEND"] == "x11"
    assert env["SDL_VIDEODRIVER"] == "x11"
    assert env["XDG_SESSION_TYPE"] == "x11"


def test_software_gl_is_forced_but_an_explicit_override_wins(tmp_path):
    assert sandbox_child_env(HOST, ":99", shim_dir="/shims")["LIBGL_ALWAYS_SOFTWARE"] == "1"
    kept = sandbox_child_env(
        {**HOST, "LIBGL_ALWAYS_SOFTWARE": "0"}, ":99", shim_dir="/shims"
    )
    assert kept["LIBGL_ALWAYS_SOFTWARE"] == "0"


def test_url_opening_is_routed_to_the_contained_handler(tmp_path):
    """#83: ``$BROWSER`` is what both ``webbrowser.open()`` and generic ``xdg-open`` consult
    first, so pointing it at the sandbox's own handler stops a URL reaching the user's real
    browser. The desktop-detection vars must ALSO be gone, or xdg-open takes its GNOME/KDE
    branch (gio/kde-open) and ignores $BROWSER entirely."""
    env = sandbox_child_env(HOST, ":99", shim_dir=str(tmp_path))
    assert env["BROWSER"] == f"{tmp_path}/sandbox-open"
    # PATH interception is the load-bearing half — see the live xdg-open test below.
    assert env["PATH"].split(":")[0] == str(tmp_path)
    for var in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "KDE_FULL_SESSION",
                "GNOME_DESKTOP_SESSION_ID"):
        assert var not in env, f"{var} left set — xdg-open would bypass $BROWSER"


@pytest.mark.parametrize("opener", _URL_OPENER_NAMES)
def test_every_url_opener_records_instead_of_opening(tmp_path, opener):
    """Each entry point must be a real executable that captures the URL (so the agent can SEE what
    the app tried to open) and launches nothing on the host. Shimmed as a CLASS because a toolkit
    reaches the desktop through whichever of these it happens to call."""
    log = tmp_path / "urls.log"
    shim_dir = write_sandbox_url_shims(tmp_path / "bin", log)
    shim = os.path.join(shim_dir, opener)
    assert os.access(shim, os.X_OK)
    subprocess.run([shim, f"https://example.com/{opener}"], check=True, timeout=10)
    assert f"https://example.com/{opener}" in log.read_text()


def test_gio_shim_only_swallows_open_and_forwards_the_rest(tmp_path):
    """``gio`` is general-purpose, so only its ``open`` subcommand is a URL escape — swallowing the
    whole binary would break unrelated app behaviour."""
    log = tmp_path / "urls.log"
    shim_dir = write_sandbox_url_shims(tmp_path / "bin", log)
    subprocess.run([os.path.join(shim_dir, "gio"), "open", "https://example.com/gio"],
                   check=True, timeout=10)
    assert "https://example.com/gio" in log.read_text()

    if shutil.which("gio"):  # a non-open subcommand must reach the real binary
        done = subprocess.run([os.path.join(shim_dir, "gio"), "help"],
                              capture_output=True, text=True, timeout=10)
        assert done.returncode == 0 and "open" in done.stdout.lower()


@pytest.mark.skipif(not shutil.which("xdg-open"), reason="needs xdg-open")
def test_a_real_xdg_open_cannot_reach_the_host_browser(tmp_path):
    """The regression test for the fix that ``$BROWSER`` alone did NOT provide.

    Verified live: ``xdg-open`` resolves the ``x-scheme-handler/https`` desktop association BEFORE
    consulting ``$BROWSER``, so with only ``$BROWSER`` set it still printed "Opening in existing
    browser session" and drove the host's Chrome — exactly #83. Containment therefore has to
    intercept the ``xdg-open`` binary on PATH, which is what this asserts against the real tool."""
    log = tmp_path / "urls.log"
    shim_dir = write_sandbox_url_shims(tmp_path / "bin", log)
    env = sandbox_child_env({**os.environ, "PATH": os.environ["PATH"]}, ":99", shim_dir=shim_dir)
    subprocess.run(["xdg-open", "https://example.com/must-not-escape"],
                   env=env, capture_output=True, timeout=20)
    assert "https://example.com/must-not-escape" in log.read_text(), (
        "xdg-open escaped the sandbox and reached the host's browser"
    )
