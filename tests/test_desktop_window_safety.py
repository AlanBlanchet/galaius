"""A wheel-scroll must never mutate the window it is scrolling, and a caller must be able to
resize a sandbox window without shelling out.

- #82: a wheel event over a Qt scroll area RESIZED the whole app window (1600x1200 -> 1600x2000)
  instead of scrolling the widget — twice in one session. A bigger window also reveals content
  that was genuinely clipped at the real size, so it manufactures false layout verdicts.
- #90: the same misrouted wheel took the window down entirely; the next call reported an empty
  sandbox with no explanation, and ~10 minutes of app state was lost.
- #84 (part 1): there was no action to resize a desktop/nested window post-launch; the reporter
  fell back to ``xdotool windowsize`` outside the MCP surface.
- #88 (part 1): refs cached before a layout change silently relabel a different widget.
"""

import asyncio

import pytest

from interact.desktop import DesktopElement, DesktopWindow


class FakeBackend:
    """A nested backend stand-in that records input and lets a test script the window geometry."""

    def __init__(self, geometry=(0, 0, 800, 600)):
        self.geometry = geometry
        self.resized: list[tuple[int, int]] = []
        self.scrolls: list[tuple[int, bool]] = []
        self.moves: list[tuple[float, float]] = []
        self.alive = True

    def window_geometry(self, name):
        return self.geometry if self.alive else None

    def _window_id(self, name):
        return "123" if self.alive else None

    def focus_wid(self, wid):
        pass

    def move(self, x, y):
        self.moves.append((x, y))

    def scroll(self, clicks, horizontal=False):
        self.scrolls.append((clicks, horizontal))

    def resize_window(self, name, w, h):
        self.resized.append((w, h))
        self.geometry = (self.geometry[0], self.geometry[1], w, h)
        return True

    def last_app_output(self, limit=800):
        return "Segmentation fault"


def _win(backend):
    win = DesktopWindow(name="App", wid=123, x=0, y=0, w=800, h=600)
    win._backend = backend
    return win


def test_scroll_restores_a_window_the_wheel_resized():
    """#82: whatever made the window grow, the caller asked to scroll a WIDGET — the window's own
    geometry is restored so a later capture measures the size the caller set up."""
    be = FakeBackend()

    original_scroll = be.scroll

    def growing_scroll(clicks, horizontal=False):
        original_scroll(clicks, horizontal)
        be.geometry = (0, 0, 800, 1000)  # the misrouted wheel resized the window

    be.scroll = growing_scroll
    asyncio.run(_win(be).scroll(400, 300, "down", 3))
    assert be.geometry[2:] == (800, 600), "the window was left at the wheel-resized size"
    assert be.resized[-1] == (800, 600)


def test_scroll_leaves_an_unchanged_window_alone():
    be = FakeBackend()
    asyncio.run(_win(be).scroll(400, 300, "down", 2))
    assert be.resized == []
    assert be.scrolls == [(-2, False)]


def test_scroll_reports_a_window_that_died_under_the_wheel():
    """#90: the window vanished mid-scroll. The caller must get a named cause (and the app's own
    output), not a later mystery "the sandbox has no windows"."""
    be = FakeBackend()

    def killing_scroll(clicks, horizontal=False):
        be.alive = False

    be.scroll = killing_scroll
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_win(be).scroll(400, 300, "down", 3))
    msg = str(exc.value)
    assert "scroll" in msg.lower() and "App" in msg
    assert "Segmentation fault" in msg, "the app's own output must be surfaced as the cause"


def test_resize_sets_the_window_size():
    """#84: a first-class resize, so verifying a narrow layout needs no xdotool shell-out."""
    be = FakeBackend()
    assert asyncio.run(_win(be).resize(500, 900)) is True
    assert be.resized == [(500, 900)]


def test_resize_refuses_a_screen_target():
    screen = DesktopWindow(name="screen", wid=-1, x=0, y=0, w=1920, h=1080, screen_geometry="")
    assert asyncio.run(screen.resize(500, 900)) is False


@pytest.mark.parametrize(
    "detected_geometry, current, stale",
    [
        ((0, 0, 800, 600), (0, 0, 800, 600), False),
        ((0, 0, 800, 600), (0, 0, 500, 600), True),   # dock/splitter resize → boxes moved
        ((0, 0, 800, 600), (0, 0, 800, 900), True),
    ],
)
def test_detection_stale_flags_a_layout_change_since_detection(detected_geometry, current, stale):
    """#88: refs detected under one geometry silently relabel other widgets after a layout change.
    The resolver can now SAY so instead of clicking the wrong thing quietly."""
    wid = 4242
    DesktopElement.invalidate(wid)
    detected = [DesktopElement(index=1, x=10, y=10, w=40, h=20, role="button", name="Params")]
    DesktopElement.store(wid, detected, geometry=detected_geometry)
    win = DesktopWindow(name="App", wid=wid, x=current[0], y=current[1], w=current[2], h=current[3])
    reason = DesktopElement.detection_stale(wid, win)
    assert (reason is not None) == stale
    if stale:
        assert "detect" in reason.lower()


def test_detection_stale_is_silent_without_a_detection():
    DesktopElement.invalidate(77)
    win = DesktopWindow(name="App", wid=77, x=0, y=0, w=800, h=600)
    assert DesktopElement.detection_stale(77, win) is None
