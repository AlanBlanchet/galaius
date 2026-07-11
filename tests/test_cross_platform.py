"""Cross-platform guarantee (#24): the MCP server boots and browser automation works on Linux,
macOS and Windows; native-desktop tools degrade with ONE actionable message off Linux instead of
leaking a cryptic evdev/maim error. These are pure (no display, no browser, no key) so they run on
the whole ubuntu/macos/windows CI matrix and keep the promise honest."""

import importlib

import pytest

from interact.desktop import backend as db
from interact.browser import BrowserManager
from interact.config import Config


def test_server_module_imports_on_any_platform():
    # The import chain must not pull in a Linux-only native module at load time (evdev/uinput are
    # lazy + sys_platform-marked; atspi is try/except). If this fails, the server won't even start
    # on macOS/Windows.
    server = importlib.import_module("interact.server")
    assert server.mcp is not None


def test_browser_manager_constructs_without_a_display():
    # Constructing a session must not require X11 / a browser launch — browser tools are the
    # cross-platform path and must be reachable everywhere.
    BrowserManager(Config())


def test_desktop_supported_is_linux_only(monkeypatch):
    monkeypatch.setattr(db.sys, "platform", "linux")
    assert db.desktop_supported() is True
    for plat in ("darwin", "win32"):
        monkeypatch.setattr(db.sys, "platform", plat)
        assert db.desktop_supported() is False


@pytest.mark.parametrize("plat", ["darwin", "win32"])
def test_select_backend_off_linux(monkeypatch, plat):
    monkeypatch.setattr(db.sys, "platform", plat)
    # `local` → the cross-platform PortableBackend (pynput/mss), so macOS/Windows can drive the
    # screen. (Construction is mocked to avoid touching a real display in the headless unit job.)
    monkeypatch.setattr(db, "PortableBackend", lambda: "PORTABLE")
    assert db.select_desktop_backend(Config(desktop_target="local")) == "PORTABLE"
    # `nested` needs an X server (Xephyr/Xvfb) → still unsupported off Linux, with an actionable msg.
    with pytest.raises(db.DesktopUnsupportedError) as exc:
        db.select_desktop_backend(Config(desktop_target="nested"))
    msg = str(exc.value).lower()
    assert "browser" in msg and "issues/24" in msg


def test_server_desktop_guard_off_linux(monkeypatch):
    import interact.server as server

    monkeypatch.setattr(db.sys, "platform", "win32")
    # window-title / nested targets stay unsupported off Linux, with an actionable message…
    msg = server._desktop_unsupported(is_screen=False)
    assert msg and msg.startswith("ERROR:") and "browser" in msg.lower() and "screen" in msg.lower()
    # …but the `screen` target IS available off Linux (PortableBackend drives the whole screen, #24).
    assert server._desktop_unsupported(is_screen=True) is None


def test_server_desktop_guard_passes_on_linux(monkeypatch):
    import interact.server as server

    monkeypatch.setattr(db.sys, "platform", "linux")
    assert server._desktop_unsupported() is None


def test_doctor_diagnostics_are_clean_off_linux(monkeypatch, capsys):
    # A Mac/Windows colleague running `interact doctor` must not see Linux-only advice
    # (`apt install maim`, `/dev/uinput … udev rule`) — that reads as "broken" when browser
    # automation is actually ready. Report N/A cleanly instead.
    import interact.cli as cli

    monkeypatch.setattr("interact.desktop.backend.desktop_supported", lambda: False)
    cli.doctor()
    out = capsys.readouterr().out
    assert "not available on" in out
    assert "/dev/uinput" not in out
    assert "apt install maim" not in out


def test_status_desktop_line_clean_off_linux(monkeypatch, capsys):
    import interact.cli as cli

    monkeypatch.setattr("interact.desktop.backend.desktop_supported", lambda: False)
    cli.status()
    out = capsys.readouterr().out
    assert "desktop" in out and "not available on this OS" in out
