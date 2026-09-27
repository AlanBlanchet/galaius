"""Desktop / nested-sandbox / launch scaffolding — reused across the cluster."""

from __future__ import annotations

from interact.desktop import DesktopBackend, DesktopWindow, NestedBackend


def desktop_window(
    name: str = "test",
    wid: int = 123,
    *,
    x: int = 0,
    y: int = 0,
    w: int = 800,
    h: int = 600,
    backend=None,
) -> DesktopWindow:
    """A ``DesktopWindow`` with the usual defaults; ``backend`` binds a nested/recording backend
    the same way ``DesktopWindow._backend`` does inside the code. Three files (input, window-safety,
    coord-transform) carried this byte-identical."""
    win = DesktopWindow(name=name, wid=wid, w=w, h=h, x=x, y=y)
    if backend is not None:
        win._backend = backend
    return win


def bare_nested_backend(display: str | None = ":88", size: tuple[int, int] = (400, 400)) -> NestedBackend:
    """A ``NestedBackend`` with no X server — only the state a unit test writes to.

    Every callsite writing to ``env`` / ``_procs`` / ``_logs`` / ``_repaint_useless`` /
    ``_repaint_attempts`` / ``screen_w`` / ``screen_h`` shared this shape; centralising it
    stops it drifting per file. Callers still set the fields they care about (``_xserver``,
    ``_commands``, ``display``) after the call."""
    nb = NestedBackend.__new__(NestedBackend)
    nb.env = {"DISPLAY": display} if display else {}
    nb.screen_w, nb.screen_h = size
    nb._procs = []
    nb._logs = {}
    nb._repaint_useless = set()
    nb._repaint_attempts = {}
    return nb


class RecordingBackend(DesktopBackend):
    """A ``DesktopBackend`` that records primitive calls instead of touching a display —
    the shared shape behind ``test_desktop_scenario.RecordingBackend`` and
    ``test_cdp_bridge._RecordingBackend``."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def capture(self) -> bytes:
        return b""

    def move(self, x: float, y: float) -> None:
        self.calls.append(("move", x, y))

    def mouse_down(self, button: str = "left") -> None:
        self.calls.append(("down", button))

    def mouse_up(self, button: str = "left") -> None:
        self.calls.append(("up", button))
