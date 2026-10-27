"""Watching this computer's screen from somewhere else (`galaius.desktop.screen.session`).

Owner 2026-10-23, dictated: « maybe we could access the PC from here? … we could have the screen,
you know? … Like view the PC, like a TeamViewer or a Parsec from the Data tab. »

What these pin: a click read off a frame lands on the pixel it was read from however the stream
rescaled since; an idle screen costs the link nothing; the computer's own switch and badge end a
view whatever the server says; and a slow link narrows the picture instead of queueing frames.
No desktop needed — the grabber and the backend are stand-ins; the one test that captures this
computer for real is marked `desktop`.
"""

import asyncio
import collections
import contextlib
import math
import time
from datetime import UTC, datetime, timedelta

import pytest
from galaius_core import ScreenAllowance, ScreenSpec
from PIL import Image

from galaius.desktop.screen.session import (
    FrameGrabber,
    QualityLadder,
    ScreenSession,
    ScreenStreamer,
)
from galaius.desktop.screen.switch import ScreenSwitch
from tests.support.screen import FakeBackend, FakeGrabber, FakeMarker


def _session(tmp_path, spec=None, allowance=True, grabber=None, backend=None, marker=None, control=True) -> ScreenSession:
    switch = ScreenSwitch(tmp_path / "screen.json")
    switch.write(ScreenAllowance(allowed=allowance, until=datetime.now(UTC) + timedelta(hours=1)) if allowance
                 else ScreenAllowance(why="off here"))
    return ScreenSession(spec=spec or ScreenSpec(), grabber=grabber or FakeGrabber(), backend=backend or FakeBackend(),
                         switch=switch, marker=marker or FakeMarker(), viewer="the web", control=control)


def test_an_unchanged_screen_sends_one_frame_and_then_nothing(tmp_path):
    grabber = FakeGrabber()
    session = _session(tmp_path, grabber=grabber)

    first = session.frame()
    assert first is not None and first.jpeg[:2] == b"\xff\xd8" and first.sequence == 1
    assert session.frame() is None, "an idle desktop must cost the link nothing"
    assert session.stats.unchanged == 1 and session.stats.frames == 1

    grabber.fill = (200, 30, 40)
    changed = session.frame()
    assert changed is not None and changed.sequence == 2 and session.stats.frames == 2


def test_a_pointer_that_moved_on_a_still_screen_is_sent(tmp_path):
    """The grab leaves the cursor out (the viewer draws it): a tap on a still window moved only the
    pointer, and the viewer kept the old one for up to `IDLE_SECONDS`."""
    backend = FakeBackend()
    session = _session(tmp_path, backend=backend)
    session.frame()
    backend.spot = (960, 540)
    moved = session.frame()
    assert moved is not None and (moved.pointer_x, moved.pointer_y) == (640, 360)
    assert session.frame() is None


def test_an_unchanged_screen_still_sends_after_its_idle_wait(tmp_path, monkeypatch):
    """A viewer that joined late, or lost a frame, always gets a picture within `IDLE_SECONDS`."""
    session = _session(tmp_path)
    assert session.frame() is not None
    assert session.frame() is None
    monkeypatch.setattr(time, "monotonic", lambda: time.perf_counter() + ScreenSession.IDLE_SECONDS + 1)
    again = session.frame()
    assert again is not None and again.sequence == 2


@pytest.mark.parametrize("width, origin, point, lands", [
    pytest.param(1920, (0, 0), (100, 50), (100, 50), id="native-scale"),
    pytest.param(960, (0, 0), (100, 50), (200, 100), id="halved-stream"),
    pytest.param(960, (-1920, 60), (100, 50), (-1720, 160), id="a-monitor-left-of-the-primary"),
])
def test_a_click_lands_on_the_pixel_it_was_read_from(tmp_path, width, origin, point, lands):
    """The viewer sends the pixel it SAW (its frame's own number with it); the computer maps it with
    that frame's origin and scale. No multiplication in the browser, so no drift."""
    backend = FakeBackend()
    grabber = FakeGrabber(rect={"left": origin[0], "top": origin[1], "width": 1920, "height": 1080})
    session = _session(tmp_path, spec=ScreenSpec(width=width), grabber=grabber, backend=backend)
    frame = session.frame()

    session.perform([{"type": "click", "x": point[0], "y": point[1], "frame": frame.sequence, "count": 2}])

    assert backend.done == [("click", lands[0], lands[1], "left", 2)]


def test_every_act_a_viewer_sends_is_the_act_an_agent_performs(tmp_path):
    """Same verbs, same names as `run_actions` on a desktop target: a scroll points first."""
    backend = FakeBackend()
    session = _session(tmp_path, spec=ScreenSpec(width=1920), backend=backend)  # native: this test is about verbs, not scale
    frame = session.frame()

    done = session.perform([
        {"type": "move", "x": 10, "y": 10, "frame": frame.sequence},
        {"type": "scroll", "x": 10, "y": 10, "frame": frame.sequence, "clicks": -3},
        {"type": "key_press", "key": "super"},
        {"type": "type_text", "text": "notepad"},
        {"type": "nonsense", "x": 1, "y": 1},
    ])

    assert done == 4, "the malformed act is dropped, the rest land"
    assert backend.done == [("move", 10, 10), ("move", 10, 10), ("scroll", -3, False), ("key", "super"), ("type", "notepad")]


def test_the_pointer_travels_with_the_frame_because_no_capture_holds_it(tmp_path):
    """mss / BitBlt / XGetImage all leave the cursor out: the viewer draws it from these fields."""
    session = _session(tmp_path, spec=ScreenSpec(width=960), backend=FakeBackend(pointer=(400, 200)))
    frame = session.frame()
    assert (frame.pointer_x, frame.pointer_y) == (200, 100)

    away = _session(tmp_path, backend=FakeBackend(pointer=(5000, 200))).frame()
    assert (away.pointer_x, away.pointer_y) == (None, None), "a pointer off this rectangle is not in this frame"


@pytest.mark.parametrize("switch_off, pressed, badge, said", [
    pytest.param(True, False, True, "off here", id="switched-off-on-the-machine"),
    pytest.param(False, True, True, "Stop was pressed", id="its-badge-stopped-it"),
    pytest.param(False, False, False, "could not show", id="no-badge-no-view"),
])
def test_nothing_is_watched_that_this_computer_does_not_show_and_allow(tmp_path, switch_off, pressed, badge, said):
    """The computer's own veto, which the server cannot answer for: the switch, the badge's Stop,
    and a desktop that cannot show a badge at all."""
    session = _session(tmp_path, allowance=not switch_off, marker=FakeMarker(showable=badge, pressed=pressed))

    with pytest.raises(PermissionError, match=said):
        session.frame()
    with pytest.raises(PermissionError, match=said):
        session.perform([{"type": "key_press", "key": "super"}])


def test_a_switch_turned_off_mid_session_ends_it_within_a_second(tmp_path, monkeypatch):
    session = _session(tmp_path)
    assert session.frame() is not None

    session.switch.turn_off()
    monkeypatch.setattr(time, "monotonic", lambda: time.perf_counter() + ScreenSwitch.POLL_SECONDS + 1)

    with pytest.raises(PermissionError, match="switched off"):
        session.frame()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Link:
    """A link as the ladder meets it: `floor_ms` of distance every round plus the picture's own time,
    `picture_ms` for a 1280-wide one and in proportion to its pixels. The clock moves by each round,
    paced to the 10 frames a second the viewer asks for; `rounds` returns the widths sent."""

    def __init__(self, ladder: QualityLadder, clock: _Clock, floor_ms: float, picture_ms: float, screen: int = 1920) -> None:
        self.ladder, self.clock, self.floor_ms, self.picture_ms = ladder, clock, floor_ms, picture_ms
        ladder.screen_width = screen

    def rounds(self, n: int) -> list[int]:
        sent = []
        for _ in range(n):
            width, quality = self.ladder.sent
            round_ms = self.floor_ms + self.picture_ms * (width / 1280) ** 2
            self.clock.now += max(round_ms, 100) / 1000
            self.ladder.measured(round_ms, width, quality)
            sent.append(width)
        return sent

    def during(self, seconds: float) -> list[int]:
        """The widths sent over `seconds` of this link."""
        sent, until = [], self.clock.now + seconds
        while self.clock.now < until:
            sent += self.rounds(1)
        return sent


def _rounds(ladder: QualityLadder, ms: float, n: int) -> None:
    for _ in range(n):
        ladder.measured(ms, *ladder.sent)


def _level(ladder: QualityLadder) -> tuple[int, int]:
    return (ladder.spec.width, ladder.spec.quality)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def ladder(clock) -> QualityLadder:
    ladder = QualityLadder(ScreenSpec(width=1280, quality=55, max_fps=10), clock=clock)
    ladder.screen_width = 1920
    return ladder


@pytest.mark.parametrize("floor_ms, fits_while_full, full_seconds", [
    pytest.param(10, 720, 6, id="lan-10ms"),
    pytest.param(70, 480, 6, id="phone-70ms"),
    pytest.param(90, 480, 6, id="phone-under-load-90ms"),
    pytest.param(220, 480, 6, id="far-link-220ms"),
    pytest.param(70, 480, 300, id="phone-70ms-full-for-5-minutes"),
    pytest.param(90, 480, 300, id="phone-under-load-90ms-full-for-5-minutes"),
])
def test_a_full_link_narrows_and_climbs_back_to_the_top_once_it_clears(ladder, clock, floor_ms, fits_while_full, full_seconds):
    """Bytes are what make this link slow while it is full (a 1280 picture costs 400 ms on it): the
    picture narrows rung by rung to one that fits, and once the link clears it climbs back to 1280 a
    rung per judgment — 960 on the way both ways, whatever wait the refused levels were left with. A
    phone's distance used to hold it narrow: « easy » was a fixed 90 ms, which a picture 90 ms away
    never is; a far link, whose distance alone is over the budget, climbs back too; and a link full for
    longer than the floor remembers a wider picture climbs back by trying one (code-reviewer r1: it
    stayed at 480 for good)."""
    link = _Link(ladder, clock, floor_ms, picture_ms=40)
    assert link.rounds(50)[-4:] == [1280] * 4, "a clear link keeps the sharpest picture"

    link.picture_ms = 400
    down = link.during(full_seconds)
    assert ladder.spec.width == fits_while_full and 960 in down

    link.picture_ms = 40
    up = link.rounds(100)
    assert up[-1] == 1280 and 960 in up, up
    assert up.index(1280) <= 4 * ladder.settings.judged_rounds, up


@pytest.mark.parametrize("floor_ms", [70, 90], ids=["phone-70ms", "phone-under-load-90ms"])
def test_a_link_full_from_its_first_frame_settles_on_a_picture_that_fits(ladder, clock, floor_ms):
    """No clear round ever showed this link's distance: its fastest round is mostly picture. Taken as
    the distance, it held the 960 picture at ~300 ms rounds, twice the budget; the narrowest and widest
    rounds put the distance where it is, and the 480 picture that fits is where the ladder stays."""
    link = _Link(ladder, clock, floor_ms, picture_ms=400)
    link.during(30)
    held = link.during(60)
    assert sum(width == 480 for width in held) >= 0.9 * len(held), collections.Counter(held)


def test_a_screen_narrower_than_the_rungs_still_narrows_on_a_full_link(ladder, clock):
    """On a 1024-wide screen the 1280 and 960 rungs send one 1024 picture: a full link skips to the
    first rung that is really narrower instead of stopping at the one that changes nothing."""
    link = _Link(ladder, clock, 70, picture_ms=400, screen=1024)
    assert 720 in link.rounds(40)


@pytest.mark.parametrize("floor_ms", [70, 113, 220], ids=["phone-35ms-each-way", "phone-under-load", "far-link-220ms"])
def test_a_link_slow_by_distance_keeps_the_sharp_picture(ladder, clock, floor_ms):
    """Distance before a byte of picture is what makes these rounds long: narrowing buys nothing, so
    the 1280 view stays — over the budget, one narrowing a minute is tried and undone (the old ladder
    walked it down to 427 px)."""
    started = clock.now
    _Link(ladder, clock, floor_ms, picture_ms=30).rounds(200)
    assert _level(ladder) == (1280, 55)
    assert ladder.lightened <= math.ceil((clock.now - started) / ladder.settings.floor_seconds), ladder.lightened


def test_a_link_that_moved_farther_is_narrowed_once_and_given_back(ladder, clock):
    """A phone hands over from Wi-Fi (10 ms) to a far cell (220 ms): the floor still remembers 10 ms,
    so one narrowing is tried, seen to come back no faster, undone — and not tried again."""
    link = _Link(ladder, clock, 10, picture_ms=30)
    link.rounds(50)
    link.floor_ms = 220
    link.rounds(200)  # ~50 s: within the minute the learned distance holds
    assert _level(ladder) == (1280, 55) and ladder.lightened == 1


def test_one_late_frame_is_jitter_not_a_slow_link(ladder):
    for _ in range(20):
        _rounds(ladder, 60, ladder.settings.judged_rounds - 1)
        ladder.measured(900, *ladder.sent)
    assert _level(ladder) == (1280, 55) and ladder.lightened == 0


def test_a_link_on_the_edge_of_a_level_stops_flipping(ladder, clock):
    """960 fits, 1280 does not, and by far more than its pixels say (the prediction is wrong for this
    link): each try at 1280 it refuses doubles the wait before the next one (5, 10, 20 s…), where the
    old ladder flipped 960 ↔ 1280 about once a second."""
    _rounds(ladder, 300, ladder.settings.judged_rounds)
    waits = []
    for _ in range(4):
        waited = 0.0
        while _level(ladder) != (1280, 55):
            clock.now += 0.1
            waited += 0.1
            ladder.measured(40, *ladder.sent)
        waits.append(round(waited))
        _rounds(ladder, 300, ladder.settings.judged_rounds)
        assert _level(ladder) == (960, 45)
    assert waits == sorted(waits) and waits[-1] >= 4 * waits[0], waits


def test_stop_ends_text_being_typed_at_its_next_chunk(tmp_path):
    """Stop pressed while a long paste is typed: what is left is not typed (it ran on for minutes)."""
    typed: list[str] = []
    session = _session(tmp_path)
    session.frame()

    def type_text(text: str) -> None:
        typed.append(text)
        session.stop_acting()  # Stop arrives while this first chunk is being typed

    session.backend.type_text = type_text
    with pytest.raises(PermissionError, match="ended"):
        session.perform([{"type": "type_text", "text": "x" * ScreenSession.TYPE_CHUNK * 4}])
    assert typed == ["x" * ScreenSession.TYPE_CHUNK]


def test_a_session_reports_what_it_cost(tmp_path):
    """The numbers a performance review reads, per step and overall."""
    session = _session(tmp_path)
    session.measured(42.0, session.frame())

    stats = session.readiness().stats
    assert stats.frames == 1 and stats.bytes_sent > 1000
    assert stats.grab.count == 1 and stats.encode.count == 1 and stats.send.p50 == 42.0
    assert "1280px q55" in stats.said and "frame p50 42.0 ms" in stats.said

    session.close()
    assert session.grabber.closed and session.marker.stopped == 1


@pytest.mark.desktop
def test_this_computers_own_screen_really_grabs(tmp_path):
    """The one test that captures this machine: the grabber reads its monitors and one real frame
    decodes at the width the spec asked for. The badge is a stand-in here — a test never pops a
    window on the owner's desktop."""
    import io

    grabber = FrameGrabber()
    try:
        monitors = grabber.monitors()
        assert monitors and all(monitor.w > 0 and monitor.h > 0 for monitor in monitors)
        session = ScreenSession(spec=ScreenSpec(width=720), grabber=grabber, backend=FakeBackend(),
                                switch=_session(tmp_path).switch, marker=FakeMarker())
        frame = session.frame()
        assert frame is not None and frame.w <= 720, "no wider than asked (whole-number reduction)"
        assert Image.open(io.BytesIO(frame.jpeg)).size == (frame.w, frame.h)
        assert session.stats.grab.worst < 100, f"a grab of this screen took {session.stats.grab.worst} ms"
    finally:
        grabber.close()


class FakeLink:
    """A transport: `delay_ms` per frame, and it can stop carrying them (`dead`)."""

    def __init__(self, delay_ms: float = 1.0, dead_after: int | None = None) -> None:
        self.delay_ms, self.dead_after = delay_ms, dead_after
        self.frames: list[int] = []

    async def send(self, frame) -> None:
        self.frames.append(frame.sequence)
        if self.dead_after is not None and len(self.frames) >= self.dead_after:
            await asyncio.sleep(3600)  # the link stopped carrying; the streamer's timeout ends it
        await asyncio.sleep(self.delay_ms / 1000)


@pytest.mark.parametrize("ending, link, stop", [
    pytest.param("switched off", FakeLink(), "switch", id="the-owner-switched-it-off"),
    pytest.param("Stop was pressed", FakeLink(), "badge", id="its-badge-stopped-it"),
    pytest.param("stopped carrying", FakeLink(dead_after=2), "", id="the-link-died"),
])
async def test_a_stream_ends_by_itself_and_says_why(tmp_path, monkeypatch, ending, link, stop):
    """Nobody has to cooperate for a view to end: the machine's switch, its badge's Stop, or a link
    that stopped carrying frames each end it, and the session is closed either way."""
    grabber = FakeGrabber()
    marker = FakeMarker()
    session = _session(tmp_path, grabber=grabber, marker=marker)
    monkeypatch.setattr(ScreenStreamer, "SEND_TIMEOUT_SECONDS", 0.2)
    streamer = ScreenStreamer(session, link.send)

    async def interfere() -> None:
        await asyncio.sleep(0.05)
        grabber.fill = (90, 90, 90)  # something changed, so frames keep coming
        if stop == "switch":
            session.switch.turn_off()
            monkeypatch.setattr(time, "monotonic", lambda: time.perf_counter() + ScreenSwitch.POLL_SECONDS + 1)
        elif stop == "badge":
            marker.pressed = True

    await asyncio.gather(asyncio.wait_for(streamer.run(), timeout=5), interfere())

    assert ending in str(streamer.ended)
    assert link.frames, "frames flowed before it ended"
    assert marker.stopped == 1 and grabber.closed, "a stream that ends takes its badge down"


async def test_a_tap_s_picture_leaves_beside_the_frame_in_flight(tmp_path):
    """A tap's change goes out once the screen shows it, without waiting for the frame already out
    to come back (up to a whole round, 134 ms at 1280 px on a phone link): it waited behind it and
    behind the frame rate's pace. A frame nobody acted for still waits its turn."""
    grabber = FakeGrabber()
    session = _session(tmp_path, grabber=grabber)
    link = FakeLink(delay_ms=2000)  # the first frame's round outlasts the test

    async def sent(count: int) -> list[int]:
        for _ in range(100):
            if len(link.frames) >= count:
                break
            await asyncio.sleep(0.01)
        return link.frames

    streamer = ScreenStreamer(session, link.send)
    stream = asyncio.create_task(streamer.run())
    try:
        assert await sent(1) == [1]
        streamer.acted()
        await asyncio.sleep(0.1)
        assert link.frames == [1], "a tap that changed nothing sends nothing"
        grabber.fill = (90, 90, 90)  # the tapped window repaints a moment after the tap
        assert await sent(2) == [1, 2], "its picture left while the first was still out"
        grabber.fill = (120, 120, 120)  # a change nobody acted for
        await asyncio.sleep(0.3)
        assert link.frames == [1, 2], "it waits for a round to come back"
    finally:
        stream.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stream


async def test_a_viewer_that_went_away_ends_the_stream_without_the_server(tmp_path, monkeypatch):
    """A locked phone or a closed tab stops taking frames; the PC stops sending within
    `VIEWER_GONE_SECONDS` instead of grabbing its own screen for nobody."""
    monkeypatch.setattr(ScreenStreamer, "VIEWER_GONE_SECONDS", 0.1)
    session = _session(tmp_path)  # an idle screen: one frame, then nothing changes
    link = FakeLink()
    streamer = ScreenStreamer(session, link.send)

    stats = await asyncio.wait_for(streamer.run(), timeout=5)

    assert str(streamer.ended) == "the viewer went away" and streamer.ended.refusal == "viewer_gone"
    assert link.frames == [1] and stats.unchanged >= 1


@pytest.mark.parametrize("native, asked, sent, scale", [
    pytest.param(1920, 1280, 1280, 2 / 3, id="a-1080p-screen-resized-to-the-rung"),
    pytest.param(1920, 960, 960, 0.5, id="a-1080p-screen-halved-onto-960"),
    pytest.param(2560, 1280, 1280, 0.5, id="a-1440p-screen-hits-it-exactly"),
    pytest.param(3840, 1280, 1280, 1 / 3, id="a-4k-screen-thirds"),
    pytest.param(1366, 1280, 1366, 1.0, id="already-near-enough-is-sent-as-it-is"),
    pytest.param(1280, 1920, 1280, 1.0, id="never-stretched-past-its-own-pixels"),
    pytest.param(1280, 960, 960, 0.75, id="960-on-a-1280-screen-not-640"),
    pytest.param(1280, 720, 720, 0.5625, id="720-on-a-1280-screen-its-own-picture"),
    pytest.param(1280, 480, 480, 0.375, id="480-on-a-1280-screen-not-427"),
])
def test_every_rung_sends_its_own_width_and_the_frame_says_the_size_it_really_is(tmp_path, native, asked, sent, scale):
    """Each rung of the ladder is its own picture, so each is reachable both ways: whole-number
    reduction where it lands on the rung (~1-2 ms), a resize where it would not (reduction sent 960
    and 720 on a 1280 screen as one 640 picture). Nothing downstream assumes the width it asked for —
    the frame carries its own size and scale, and a click maps through those."""
    grabber = FakeGrabber(rect={"left": 0, "top": 0, "width": native, "height": round(native * 9 / 16)})
    session = _session(tmp_path, spec=ScreenSpec(width=asked), grabber=grabber)

    frame = session.frame()

    assert frame.w == sent and frame.scale == pytest.approx(scale, abs=0.01)
    assert frame.input_point(frame.w - 1, 0)[0] == pytest.approx(native - 1 / scale, abs=2), "its own edge maps to the screen's"
