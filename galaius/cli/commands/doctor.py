"""`galaius doctor`: diagnose the environment."""

import os
import platform as _pf
import shutil
from importlib.util import find_spec
from galaius.cli.command_bootstrap import UserConfig
from galaius.desktop import backend as desktop_backend
from galaius.models.registry import Model
from galaius.models.capability import ModelCapability
from galaius.runtime import config
from galaius.cli.health.extension import print_extension_status
from galaius.cli.health.media import print_media_transport
from galaius.cli.health.models import print_resolved_models
from galaius.cli.health.ollama import print_ollama
from galaius.cli.health.sandboxes import print_sandboxes
from galaius.cli.health.servers import print_stale_servers


def doctor(*, fix: bool = False) -> None:
    """Diagnose the environment: command, providers, Playwright, desktop capture.

    Parameters
    ----------
    fix
        Deliver this tree to what you're actually running: restart any stale `galaius mcp`
        server (it respawns on current code from your editor), repackage + reinstall the VS Code
        extension when the installed one is older than this tree. One-step cure for "I shipped
        the fix but the bug persists" — a real failure mode here: a day's work once sat
        undelivered behind a warning this command could only print.

        Installing an extension makes VS Code reload its extension hosts, interrupting sessions
        in your editor windows — why it never happens without --fix.
    """


    print("galaius doctor\n")
    print_stale_servers(fix=fix)
    print_extension_status(fix=fix)
    print(f"  command       : {shutil.which('galaius') or 'NOT on PATH'}")
    print(f"  config file   : {UserConfig.PATH} ({'present' if UserConfig.PATH.exists() else 'absent'})")

    if find_spec("playwright") is not None:
        print(f"  playwright    : installed (browser: {os.environ.get('GALAIUS_BROWSER_TYPE', 'chromium')})")
    else:
        print("  playwright    : MISSING — run `uv run playwright install`")


    print_media_transport(config)

    # Desktop diagnostics are Linux/X11-specific (maim, /dev/uinput, Xephyr) — on macOS/Windows
    # they'd print misleading "MISSING (apt install …)" advice, so report cleanly that desktop
    # automation is N/A and browser is the ready path.
    if not desktop_backend.desktop_supported():

        print(f"  desktop       : not available on {_pf.system()} — desktop automation is "
              "Linux/X11 only (issues/24); browser automation works fully here.")
    else:
        session = os.environ.get("XDG_SESSION_TYPE", "?")
        display = os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")
        maim = shutil.which("maim")
        print(f"  desktop       : session={session} display={display or 'none'} maim={maim or 'MISSING (apt install maim)'}")
        print_sandboxes(display)
        if session == "wayland":
            print("                  note: browser + the nested sandbox + uinput input work on "
                  "Wayland; local (non-nested) window capture/enumeration is X11-only for now "
                  "→ prefer `desktop.target=nested`.")
        print(f"  desktop target: {config.desktop_target}  (set: galaius config set desktop.target local|nested)")
        if config.desktop_target == "nested":
            server = "Xvfb" if config.nested_headless else "Xephyr"
            pkg = "xvfb" if config.nested_headless else "xserver-xephyr"
            server_path = shutil.which(server) or f"MISSING (apt install {pkg})"
            xdotool = shutil.which("xdotool") or "MISSING (apt install xdotool)"
            mode = "headless/background" if config.nested_headless else "visible"
            print(f"  nested deps   : {server}={server_path} ({mode}) xdotool={xdotool} display=:{config.nested_display}")
        uinput = "writable" if os.access("/dev/uinput", os.W_OK) else "NOT writable — add a udev rule + join `input` group (no root)"
        print(f"  input driver  : /dev/uinput {uinput}  (absolute pointer, works on X11 + Wayland)")

    Model.load_registry()
    available = Model.available_providers()
    grounding = Model.available_by_capability(ModelCapability.GUI_GROUNDING)
    print(
        f"  API/local     : {', '.join(available) or 'none (subscription visuals need no API key)'}"
    )
    print(f"  grounding     : {len(grounding)} model(s) ready")
    print_ollama()
    print("  selection     : (what each tool resolves to — answers 'why is my default X?')")
    print_resolved_models(indent="    ")
