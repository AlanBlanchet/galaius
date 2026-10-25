"""The badge this computer shows while it is watched, and its own stop (`galaius.desktop.screen_marker`).

Threat review 2026-10-23 (binding): « the runner draws an always-on-top window … If that window
disappears, streaming stops. Kill: a local hotkey stops the session. » These pin that the badge
really comes up before anything streams (a badge that cannot be drawn means no stream, never a
silent one), and that Ctrl+Alt+Shift+S pressed on the computer ends the session and switches
sharing off — the server is never asked.
"""

import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest
from galaius_core import ScreenAllowance

from galaius.desktop.screen_marker import ScreenMarker
from galaius.desktop.screen_switch import ScreenSwitch


@pytest.fixture
def switch(tmp_path) -> ScreenSwitch:
    switch = ScreenSwitch(tmp_path / "screen.json")
    switch.write(ScreenAllowance(allowed=True, until=datetime.now(UTC) + timedelta(hours=1)))
    return switch


def test_a_badge_that_cannot_be_drawn_means_no_view(switch, tmp_path) -> None:
    broken = tmp_path / "python"
    broken.write_text(f"#!{sys.executable}\nimport sys; sys.exit(2)\n")
    broken.chmod(0o755)
    marker = ScreenMarker(switch, interpreters=(str(broken),))
    assert marker.show("Alan") is False
    assert switch.live(), "a badge that never came up is no Stop press"


@pytest.mark.skipif(not (shutil.which("Xvfb") and shutil.which("xdotool")), reason="needs Xvfb and xdotool")
def test_the_hotkey_on_this_computer_ends_the_view_and_switches_sharing_off(switch, monkeypatch) -> None:
    server = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "1280x800x24"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        display = ":" + server.stdout.readline().strip()  # the free display Xvfb took, once it is ready
        monkeypatch.setenv("DISPLAY", display)
        marker = ScreenMarker(switch)
        assert marker.show("Alan"), "the badge must come up before a frame leaves"
        assert not marker.ended_itself()
        subprocess.run(["xdotool", "key", "ctrl+alt+shift+s"], env={**os.environ, "DISPLAY": display}, check=True)
        for _ in range(50):
            if marker.ended_itself():
                break
            time.sleep(0.1)
        assert marker.ended_itself() and not switch.live()
        assert "Stop" in switch.read().why
    finally:
        server.terminate()
        server.wait(5)
