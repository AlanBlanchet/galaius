"""Cross-platform guarantee (#24): the MCP server boots and browser automation works on Linux,
macOS and Windows; native-desktop tools degrade with ONE actionable message off Linux instead of
leaking a cryptic evdev/maim error. These are pure (no display, no browser, no key) so they run on
the whole ubuntu/macos/windows CI matrix and keep the promise honest."""

import importlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from galaius.desktop import backend as db
from galaius.mcp_relay import locked
from tests.support import browser_manager

#: Standard modules Windows does not ship. Each import of one sits under `sys.platform` or in a `try`, never at a
#: module's top level: 2026-10-23 `galaius/mcp_relay.py` imported `fcntl` there, and the owner's Windows PC could
#: not run `galaius install` at all (« ModuleNotFoundError: No module named 'fcntl' » out of `galaius.cli.app`).
UNIX_ONLY = ("fcntl", "termios", "pwd", "grp", "resource", "tty", "pty", "syslog")


def test_no_module_imports_a_unix_only_module_at_its_top_level():
    source = Path(importlib.import_module("galaius").__file__).parent
    offenders = [f"{path.relative_to(source)}:{number}: {line}"
                 for path in source.rglob("*.py")
                 for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if line.startswith(("import ", "from ")) and line.split()[1].split(".")[0] in UNIX_ONLY]
    assert offenders == [], offenders


def test_the_relay_lock_lets_one_process_at_a_time_through(tmp_path):
    """`mcp_relay.locked`, the relay's own stdlib-only lock (it runs under `python -I -S`: no pydantic, no pywin32,
    so `galaius.file_lock` cannot serve it). Here: a held lock makes the next taker wait."""
    path = str(tmp_path / "endpoint.lock")
    program = f"import sys; sys.path.insert(0, {str(Path(importlib.import_module('galaius').__file__).parents[1])!r});\nfrom galaius.mcp_relay import locked\nimport time\nwith locked({path!r}):\n    time.sleep(1.5)\n"
    holder = subprocess.Popen([sys.executable, "-c", program])
    time.sleep(0.4)
    started = time.monotonic()
    with locked(path):
        waited = time.monotonic() - started
    holder.wait(timeout=10)
    assert waited > 0.5, waited
    assert os.path.exists(path)


def test_server_module_imports_on_any_platform():
    # The import chain must not pull in a Linux-only native module at load time (evdev/uinput are
    # lazy + sys_platform-marked; atspi is try/except). If this fails, the server won't even start
    # on macOS/Windows.
    server = importlib.import_module("galaius.server")
    assert server.mcp is not None


def test_browser_manager_constructs_without_a_display():
    # Constructing a session must not require X11 / a browser launch — browser tools are the
    # cross-platform path and must be reachable everywhere.
    browser_manager()


def test_desktop_supported_is_linux_only(monkeypatch):
    monkeypatch.setattr(db.sys, "platform", "linux")
    assert db.desktop_supported() is True
    for plat in ("darwin", "win32"):
        monkeypatch.setattr(db.sys, "platform", plat)
        assert db.desktop_supported() is False


def test_server_desktop_guard_off_linux(monkeypatch):
    import galaius.server as server

    monkeypatch.setattr(db.sys, "platform", "win32")
    # window-title / nested targets stay unsupported off Linux, with an actionable message…
    msg = server._desktop_unsupported(is_screen=False)
    assert msg and msg.startswith("ERROR:") and "browser" in msg.lower() and "screen" in msg.lower()
    # …but the `screen` target IS available off Linux (PortableBackend drives the whole screen, #24).
    assert server._desktop_unsupported(is_screen=True) is None


def test_server_desktop_guard_passes_on_linux(monkeypatch):
    import galaius.server as server

    monkeypatch.setattr(db.sys, "platform", "linux")
    assert server._desktop_unsupported() is None


def test_doctor_diagnostics_are_clean_off_linux(monkeypatch, capsys):
    # A Mac/Windows colleague running `galaius doctor` must not see Linux-only advice
    # (`apt install maim`, `/dev/uinput … udev rule`) — that reads as "broken" when browser
    # automation is actually ready. Report N/A cleanly instead.
    import galaius.cli as cli

    monkeypatch.setattr("galaius.desktop.backend.desktop_supported", lambda: False)
    cli.doctor()
    out = capsys.readouterr().out
    assert "not available on" in out
    assert "/dev/uinput" not in out
    assert "apt install maim" not in out


def test_status_desktop_line_clean_off_linux(monkeypatch, capsys):
    import galaius.cli as cli

    monkeypatch.setattr("galaius.desktop.backend.desktop_supported", lambda: False)
    cli.status()
    out = capsys.readouterr().out
    assert "desktop" in out and "not available on this OS" in out
