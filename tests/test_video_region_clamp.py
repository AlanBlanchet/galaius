"""A recording region must never exceed the nested screen.

Found by dogfooding the README demo generator: `gnome-calculator` refuses to shrink below its
minimum height, so in a 520x460 sandbox its window is 520x622 — taller than the display it lives
on. `capture_video` handed those raw window dimensions to x11grab, which cannot grab past the
screen and exits non-zero, so recording the window failed OUTRIGHT with a CalledProcessError
instead of returning the visible part.

Any app with a minimum size larger than the sandbox hits this, which also covers the ordinary case
of a window positioned partly off-screen.
"""

import pytest

from interact.desktop.nested import NestedBackend


@pytest.fixture
def backend(monkeypatch):
    be = NestedBackend.__new__(NestedBackend)
    be.screen_w, be.screen_h = 520, 460
    be.env = {"DISPLAY": ":120"}
    be._video_sessions = {}
    monkeypatch.setattr(NestedBackend, "force_repaint", lambda self, name: True)
    monkeypatch.setattr(NestedBackend, "audio_monitor", lambda self: None)
    monkeypatch.setattr(NestedBackend, "_reap", lambda self: None)
    return be


@pytest.mark.parametrize(
    "geometry, expected",
    [
        ((0, 0, 520, 622), (0, 0, 520, 460)),      # taller than the screen (the real case)
        ((0, 0, 800, 300), (0, 0, 520, 300)),      # wider than the screen
        ((-40, -20, 520, 460), (0, 0, 480, 440)),  # positioned partly off the top-left
        ((100, 100, 520, 460), (100, 100, 420, 360)),  # runs off the bottom-right
        ((10, 20, 300, 200), (10, 20, 300, 200)),  # already inside → untouched
    ],
)
def test_grab_region_is_clamped_to_the_screen(backend, geometry, expected):
    assert backend._grab_region(geometry) == expected


def test_a_window_bigger_than_the_screen_still_records(backend, monkeypatch):
    """The regression: an oversized window must produce a clip of what IS on screen, not raise."""
    captured: dict[str, list[str]] = {}

    monkeypatch.setattr(NestedBackend, "window_geometry", lambda self, name: (0, 0, 520, 622))

    def fake_run(args, **kwargs):
        captured["args"] = args
        out = args[-1]
        with open(out, "wb") as fh:
            fh.write(b"\x00" * 32)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr("interact.desktop.nested.subprocess.run", fake_run)
    data = backend.capture_video("Calculator", duration=1.0, fps=10)
    assert data == b"\x00" * 32
    assert "520x460" in captured["args"], f"grab region not clamped: {captured['args']}"
