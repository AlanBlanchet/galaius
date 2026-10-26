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


def test_the_cli_imports_on_this_platform():
    """The import chain the owner's `galaius install` runs. On the Windows CI job this fails on any Unix-only import
    wherever it sits, which the scan below cannot see (`import os, fcntl`, a module outside its list)."""
    assert importlib.import_module("galaius.cli.app") is not None


def test_the_relay_imports_where_fcntl_does_not_exist():
    """Windows, as the owner's PC meets it: `fcntl` absent and `sys.platform` win32. Checked here because a Windows
    runner needs GitHub Actions minutes, which his account refused on 2026-10-23 (payment / spending limit)."""
    program = (
        "import galaius, shutil, socket, subprocess, json, queue, stat, threading, time, contextlib\n"  # the standard
        "import sys\n"                                            # library loads its own fcntl users first (shutil)
        "sys.platform = 'win32'\n"
        "class Absent:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'fcntl':\n"
        "            raise ModuleNotFoundError(\"No module named 'fcntl'\")\n"
        "        return None\n"
        "sys.meta_path.insert(0, Absent())\n"
        "sys.modules.pop('fcntl', None)\n"
        "import galaius.mcp_relay as relay\n"
        "print('relay imported', relay.HELLO)\n"
    )
    done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
    assert done.returncode == 0 and "relay imported" in done.stdout, done.stderr[-1500:]


def test_no_module_imports_a_unix_only_module_at_its_top_level():
    source = Path(importlib.import_module("galaius").__file__).parent
    offenders = [f"{path.relative_to(source)}:{number}: {line}"
                 for path in source.rglob("*.py")
                 for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if line.startswith(("import ", "from ")) and line.split()[1].split(".")[0] in UNIX_ONLY]
    assert offenders == [], offenders


#: Ways to start a program that never pass through `subprocess.Popen`, so `galaius.windowless.hide_child_consoles`
#: cannot keep their console hidden on Windows (owner 2026-10-25: a console window kept opening on a Windows PC running
#: galaius). Each site allowed keeps its reason: never run on Windows, or no console program started.
BYPASSING = {"os.system", "os.popen", "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe", "os.spawnv", "os.spawnve",
             "os.spawnvp", "os.spawnvpe", "os.execl", "os.execle", "os.execlp", "os.execlpe", "os.execv", "os.execve",
             "os.execvp", "os.execvpe", "os.startfile", "asyncio.create_subprocess_shell"}
BYPASS_ALLOWED = {
    ("mcp_endpoint.py", "os.execv"): "the shared relay, turned off on win32 a few lines above",
    ("upgrade/handoff.py", "os.execv"): "POSIX only: Windows hands over through a child (`continue_in`, `replace`)",
    ("sign_in_browser.py", "os.startfile"): "opens the person's browser, a windowed program, no console",
    ("feedback.py", "os.startfile"): "opens the person's browser, a windowed program, no console",
}
#: Spawn flags that give a child a console of its own (`CREATE_NEW_CONSOLE`) or none to share with what IT starts
#: (`DETACHED_PROCESS`: each console program it runs then opens a window): `hide_child_consoles` keeps either as asked.
CONSOLE_FLAGS = {"CREATE_NEW_CONSOLE", "DETACHED_PROCESS"}


def _calls_and_names(path):
    """Each call `module.name(...)` of `path` by the module's real name (`import os as _os` reads as `os`), each name
    imported straight from a module (`from os import startfile`, called by itself), and each console flag it reads."""
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules, names = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update({alias.asname or alias.name: alias.name for alias in node.names})
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.update({alias.asname or alias.name: f"{node.module}.{alias.name}" for alias in node.names})
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            yield node.lineno, f"{modules.get(node.func.value.id, node.func.value.id)}.{node.func.attr}", node
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names:
            yield node.lineno, names[node.func.id], node
        elif isinstance(node, ast.Attribute) and node.attr in CONSOLE_FLAGS:
            yield node.lineno, node.attr, node
        elif isinstance(node, ast.Name) and node.id in CONSOLE_FLAGS and names.get(node.id, "").startswith("subprocess."):
            yield node.lineno, node.id, node


def test_every_program_galaius_starts_goes_through_popen_where_windows_hides_its_console():
    """Owner 2026-10-25 (dictated): « elle aura un CMD qui va s'ouvrir … C'est vraiment un red flag de fou. » Every spawn
    passes through `subprocess.Popen`, where a galaius process with no console window gives each child none either
    (`galaius.windowless`, pinned on Windows by tests/test_processes_windows.py). A call that bypasses it, `shell=True`
    (cmd.exe in between), or a flag asking for a console of the child's own is named here with its file and line."""
    import ast
    source = Path(importlib.import_module("galaius").__file__).parent
    offenders = []
    for path in sorted(source.rglob("*.py")):
        where = path.relative_to(source).as_posix()
        if where == "windowless.py":  # the one place that reads those flags, to keep a console a caller asked for
            continue
        for line, name, node in _calls_and_names(path):
            if name in BYPASSING and (where, name) not in BYPASS_ALLOWED:
                offenders.append(f"{where}:{line}: {name}")
            elif name in CONSOLE_FLAGS:
                offenders.append(f"{where}:{line}: subprocess.{name}")
            elif isinstance(node, ast.Call) and any(keyword.arg == "shell" and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
                                                    for keyword in node.keywords):
                offenders.append(f"{where}:{line}: {name}(shell=…)")
    assert offenders == [], offenders
    present = {(path.relative_to(source).as_posix(), name) for path in source.rglob("*.py") for _line, name, _node in _calls_and_names(path)}
    assert set(BYPASS_ALLOWED) <= present, f"allowed but gone, take them out: {sorted(set(BYPASS_ALLOWED) - present)}"


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
