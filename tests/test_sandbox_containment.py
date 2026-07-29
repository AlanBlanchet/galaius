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

import pytest

from interact.desktop.backend import _WAYLAND_ESCAPE_VARS, sandbox_child_env


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
    env = sandbox_child_env(HOST, ":99", browser_handler=str(tmp_path / "open"))
    assert env["DISPLAY"] == ":99"


@pytest.mark.parametrize("var", sorted(_WAYLAND_ESCAPE_VARS))
def test_every_wayland_escape_var_is_scrubbed(var, tmp_path):
    """#85: any of these left set lets a toolkit auto-detect the HOST session and render there.
    Scrubbed as a CLASS — a new toolkit's variable is one entry, not a new bug."""
    env = sandbox_child_env(HOST, ":99", browser_handler=str(tmp_path / "open"))
    assert var not in env, f"{var} escaped into the sandbox child's environment"


def test_toolkits_are_pinned_to_x11(tmp_path):
    """Scrubbing alone leaves auto-detection to chance; pin the two big toolkits explicitly so a
    Qt/GTK app cannot pick anything but the sandbox's X display (#85)."""
    env = sandbox_child_env(HOST, ":99", browser_handler=str(tmp_path / "open"))
    assert env["QT_QPA_PLATFORM"] == "xcb"
    assert env["GDK_BACKEND"] == "x11"
    assert env["SDL_VIDEODRIVER"] == "x11"
    assert env["XDG_SESSION_TYPE"] == "x11"


def test_software_gl_is_forced_but_an_explicit_override_wins(tmp_path):
    assert sandbox_child_env(HOST, ":99", browser_handler="x")["LIBGL_ALWAYS_SOFTWARE"] == "1"
    kept = sandbox_child_env(
        {**HOST, "LIBGL_ALWAYS_SOFTWARE": "0"}, ":99", browser_handler="x"
    )
    assert kept["LIBGL_ALWAYS_SOFTWARE"] == "0"


def test_url_opening_is_routed_to_the_contained_handler(tmp_path):
    """#83: ``$BROWSER`` is what both ``webbrowser.open()`` and generic ``xdg-open`` consult
    first, so pointing it at the sandbox's own handler stops a URL reaching the user's real
    browser. The desktop-detection vars must ALSO be gone, or xdg-open takes its GNOME/KDE
    branch (gio/kde-open) and ignores $BROWSER entirely."""
    handler = str(tmp_path / "sandbox-open")
    env = sandbox_child_env(HOST, ":99", browser_handler=handler)
    assert env["BROWSER"] == handler
    for var in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "KDE_FULL_SESSION",
                "GNOME_DESKTOP_SESSION_ID"):
        assert var not in env, f"{var} left set — xdg-open would bypass $BROWSER"


def test_the_url_handler_records_instead_of_opening(tmp_path):
    """The handler must be a real executable that captures the URL (so the agent can SEE what the
    app tried to open) and must not launch anything on the host."""
    from interact.desktop.backend import write_sandbox_browser_handler

    log = tmp_path / "urls.log"
    handler = write_sandbox_browser_handler(tmp_path, log)
    assert os.access(handler, os.X_OK)

    import subprocess

    subprocess.run([handler, "https://example.com/issues/new"], check=True, timeout=10)
    assert "https://example.com/issues/new" in log.read_text()
