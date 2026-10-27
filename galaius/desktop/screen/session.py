"""Watching this computer's screen from somewhere else, and acting on it: the frames it sends
(`ScreenSession`), the acts it takes back (`ScreenSession.perform`), and the owner's own switch on
the machine itself (`ScreenAllowance`, `ScreenSwitch`).

Capture is **mss** on every operating system — the library the Windows and macOS desktop backends
already grab with (`PortableBackend`): GDI ``BitBlt`` on Windows, CoreGraphics on macOS,
``XGetImage`` on Linux, all in-process. Linux X11 keeps ``maim`` for a one-off screenshot (a
subprocess per picture, fine once, far too slow at ten a second), so a stream reads mss directly
and every OS runs one code path. Desktop Duplication (DXGI) would cut the Windows grab further and
report its own damage rectangles, but it needs a Windows-only package and only pays once GDI is
the limit — measured here it is not: a grab is ~9 ms against a ~100 ms frame budget.

Input goes through `host_desktop()`, so a viewer's click is the same act an agent performs
(`SendInput` on Windows, Quartz on macOS, xdotool on X11) with the same names.
"""

import asyncio
import concurrent.futures
import hashlib
import io
import itertools
import logging
import math
import statistics
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar

import mss
from galaius_core import (
    SCREEN_ACTS,
    SCREEN_ACTS_MAX,
    SCREEN_FRAME_MAX,
    SCREEN_WIDTHS,
    ScreenAct,
    ScreenAllowance,
    ScreenFrame,
    ScreenMonitor,
    ScreenPoint,
    ScreenReadiness,
    ScreenSpec,
    ScreenStats,
    Timing,
)
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from galaius.desktop.backend import DesktopBackend, DesktopUnsupportedError
from galaius.desktop.host import host_desktop
from galaius.desktop.screen.marker import ScreenMarker
from galaius.desktop.screen.switch import ScreenSwitch
from galaius.desktop.screen.trust import ScreenEnd

logger = logging.getLogger("galaius")


class FrameGrabber:
    """One mss grab of one rectangle of this computer's screen, as RGB pixels.

    mss is opened once per session and kept: its per-call setup is the costly part, and a
    ``with mss.mss()`` per frame doubled the grab on this machine."""

    def __init__(self) -> None:
        # `mss.mss()` as `PortableBackend` opens it: 10.x deprecated the name in favour of `mss.MSS`,
        # which 9.x (this package's floor) does not have — one spelling until the floor moves.
        try:
            self._mss = mss.mss()
        except Exception as error:  # mss raises its own XError / ScreenShotError per platform
            raise DesktopUnsupportedError(
                f"This computer's screen cannot be captured: {error}. On Linux the runtime needs the "
                "graphical session it should show (DISPLAY set for the service that runs it); a Wayland "
                "session cannot be captured at all yet."
            ) from error

    def close(self) -> None:
        self._mss.close()

    def monitors(self) -> list[ScreenMonitor]:
        """The physical monitors, numbered as ``screen:<index>`` names them (mss's [0] is the union
        of them all, which is what `whole` grabs)."""
        return [ScreenMonitor(index=index, name=f"monitor{index}", x=m["left"], y=m["top"], w=m["width"], h=m["height"])
                for index, m in enumerate(self._mss.monitors[1:])]

    def rect(self, monitor: int | None) -> dict[str, int]:
        """The rectangle a spec names: one monitor, or the whole desktop across all of them.
        A monitor index no longer attached falls back to the whole desktop — a screen unplugged
        mid-session never ends the session with a stack trace."""
        if monitor is not None:
            physical = self._mss.monitors[1:]
            if 0 <= monitor < len(physical):
                return dict(physical[monitor])
            logger.info("screen: monitor %d is not attached; showing the whole desktop", monitor)
        return dict(self._mss.monitors[0])

    def grab(self, rect: dict[str, int]) -> Image.Image:
        shot = self._mss.grab(rect)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


class LadderSettings(BaseModel):
    """How `QualityLadder` judges the link: one budget, the rounds a judgment rests on, and the
    margins that keep it from flipping between two levels."""

    model_config = ConfigDict(frozen=True)
    #: The round (a frame out → its ack back) the picture is kept under: half the owner's « ≤300 ms
    #: p95 » on a mobile link, as a change nobody acted for waits up to one round for its grab and then
    #: rides the next (`ScreenStreamer` sends an act's picture beside the frame in flight).
    budget_ms: float = Field(default=150.0, gt=0)
    #: Rounds a level is judged on: all of them over the budget narrow the picture (one late frame is
    #: jitter), their median says what the level costs.
    judged_rounds: int = Field(default=4, ge=2)
    #: A narrower picture must come back at least this much faster than the wider one — predicted
    #: before narrowing, seen after it — or the link is slow by distance, not by bytes.
    gain: float = Field(default=0.85, gt=0, lt=1)
    #: A sharper picture is taken once its own time (its predicted round less the floor) fits this
    #: share of what the budget leaves past the floor: room for the quality step that rides with the
    #: width, and for the jitter a median does not show.
    headroom: float = Field(default=0.85, gt=0, le=1)
    #: The link's floor (its distance: the fastest round any picture made) is the minimum over this
    #: long — long enough to hold through a congested spell, short enough to follow a move to another
    #: network; on a far link it is also how often one narrowing is tried again.
    floor_seconds: float = Field(default=60.0, gt=0)
    #: A level the link refused right after the ladder sharpened to it waits this long before the next
    #: try, doubled on each refusal within `retry_max_seconds` of reaching it — shorter than
    #: `floor_seconds`, so the retry comes due while the floor still holds the refused level's rounds.
    retry_seconds: float = Field(default=5.0, gt=0)
    retry_max_seconds: float = Field(default=50.0, gt=0)

    @model_validator(mode="after")
    def _retries_inside_the_floor(self) -> "LadderSettings":
        if not self.retry_seconds <= self.retry_max_seconds < self.floor_seconds:
            raise ValueError("retry_seconds ≤ retry_max_seconds < floor_seconds")
        return self


@dataclass
class Refusal:
    """A level the link refused: tried again once `until` passes, or sooner once its round,
    predicted from the level below and scaled by `excess` (how far its real round outran that
    prediction when it was refused), fits — the link cleared. A level on its edge, whose real round
    outran the prediction far, waits out its retry."""

    until: float
    excess: float | None = None


class QualityLadder:
    """The width and quality frames are made at, walked from the rounds the link really takes (a frame
    out → its ack back).

    A round is the link's distance (its `floor`, read from the fastest rounds seen lately) plus the
    picture's own time, which scales with its pixels: that is how the ladder predicts another level's
    round from the current one (`predicted`). It narrows once `judged_rounds` rounds in a row are over
    the budget and a narrower picture is predicted to come back faster (`LadderSettings.gain`), and
    sharpens once the sharper picture is predicted to fit (`LadderSettings.headroom`) or would not be
    narrowed again — so a phone 90 ms away climbs back to its sharpest level as soon as the bytes
    fit, where a fixed « fast enough » held it narrow. A narrowing that came back no faster is undone
    and teaches the ladder the link's new distance; a level refused right after sharpening to it
    waits out a doubling retry, so a link on the edge of one level does not flip, unless the link
    clears first (`Refusal`). Never sharper than the viewer asked (`asked`)."""

    #: A screen this close above a rung is sent as it is, or by whole-number reduction when that lands
    #: this close above it: the geometric middle between the two nearest rungs (√1.2 ≈ 1.095), so two
    #: rungs never send one picture and every rung is reachable both ways.
    NEAR_ENOUGH: ClassVar[float] = min(wider / narrower for narrower, wider in itertools.pairwise(SCREEN_WIDTHS)) ** 0.5

    def __init__(self, asked: ScreenSpec, settings: LadderSettings | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings or LadderSettings()
        self.clock = clock
        self.asked = asked
        #: Every level from what the viewer asked down to the lightest, one `ScreenSpec.lighter` step
        #: apart: walked by index, so a rung is the same level on the way down and on the way up.
        self.levels = [asked]
        while (lighter := self.levels[-1].lighter()).level != self.levels[-1].level:
            self.levels.append(lighter)
        self._at = 0
        self.lightened = self.sharpened = 0
        #: The width of the last grab, set by whoever grabs: what every level is sized from (the asked
        #: width until the first grab says).
        self.screen_width = asked.width
        #: The last rounds at the current level, what a level is judged on.
        self._at_level: deque[float] = deque(maxlen=self.settings.judged_rounds)
        #: Every round of the last `floor_seconds`: when it ended, what it took, the width it carried.
        self._recent: deque[tuple[float, float, int]] = deque()
        self._stepped_at = self.clock()
        self._sharpened_last = False
        #: The wider level a narrowing left (its index) and its last round, until the narrower one is judged:
        #: the last, not the median, as a link filling up mixes rounds from before it filled into the median.
        self._narrowed_from: tuple[int, float] | None = None
        #: Levels the link refused, by index in `levels`; the wait the next refusal gets.
        self._refused: dict[int, Refusal] = {}
        self._retry = self.settings.retry_seconds

    @property
    def spec(self) -> ScreenSpec:
        """The level frames are made at now."""
        return self.levels[self._at]

    @property
    def floor(self) -> tuple[float, float] | None:
        """Where the link's distance lies, from the rounds of the last `floor_seconds`: no higher than
        the fastest round of all, no lower than where the fastest rounds of the narrowest and the
        widest picture put a picture of no pixels (a round is distance plus a time per pixel). The two
        meet while the link holds steady; they part when it changed between the rounds — a link full
        from its first frame, whose fastest round is still mostly picture (taken as the distance, it
        held the ladder at twice its budget), or one that cleared since its widest picture went out.
        Each move reads the end that favours it, as its outcome corrects it (`_far`, `_narrow`).

        None while every round carried the current width: from one picture size distance and bytes
        cannot be told apart, so either move is a trial — a narrowing counts the whole round as
        picture, a sharpening counts it as distance."""
        width = self.sizing(self.screen_width)[0]
        if all(sent == width for _, _, sent in self._recent):
            return None
        fastest: dict[int, float] = {}
        for _, round_ms, sent in self._recent:
            fastest[sent] = min(fastest.get(sent, round_ms), round_ms)
        highest, narrow, wide = min(fastest.values()), min(fastest), max(fastest)
        per_pixel = (fastest[wide] - fastest[narrow]) / (wide ** 2 - narrow ** 2) if wide > narrow else 0.0
        return max(min(highest, fastest[narrow] - per_pixel * narrow ** 2), 0.0), highest

    def sizing(self, screen_width: int, spec: ScreenSpec | None = None) -> tuple[int, int | None]:
        """The width `spec` (default: the current level) sends from a `screen_width`-wide grab, and the whole number
        that reduces the grab to it (1: sent as it is), or None when only a resize lands on its rung.

        Whole-number reduction is ~1-2 ms a frame against ~7-18 ms for a resize (measured 2026-10-27,
        Pillow 12.2, a 1280- and a 1920-wide grab), so it is taken whenever it lands within
        `NEAR_ENOUGH` of the rung; a resize only where it would not — 960 on a 1280 screen, which
        reduction sent as 640, the same picture as 720. Never wider than the screen itself."""
        rung = (spec or self.spec).width
        factor = max(screen_width // rung, 1)
        reduced = math.ceil(screen_width / factor)
        if reduced <= rung * self.NEAR_ENOUGH:
            return reduced, factor
        return rung, None

    def predicted(self, spec: ScreenSpec, round_ms: float, floor: float) -> float:
        """The round `spec` would take, from `round_ms` at the current level, on a link `floor` away:
        the same distance, and the picture's own time in proportion to its pixels. The quality step
        that rides with the width is left to `LadderSettings.headroom` and to the refusal of a level
        that proves slower."""
        return floor + (round_ms - floor) * (self.sizing(self.screen_width, spec)[0] / self.sizing(self.screen_width)[0]) ** 2

    @property
    def sent(self) -> tuple[int, int]:
        """The width and quality a frame of the current level has, from the last grab."""
        return self.sizing(self.screen_width)[0], self.spec.quality

    def measured(self, round_ms: float, width: int, quality: int) -> None:
        """One round the link took for a frame `width` wide at `quality`; may move a level. A frame
        made at a level the ladder has left since (two can be out at once) teaches the floor only."""
        now = self.clock()
        self._recent.append((now, round_ms, width))
        while self._recent[0][0] < now - self.settings.floor_seconds:
            self._recent.popleft()
        if (width, quality) != self.sent:
            return
        self._at_level.append(round_ms)
        if len(self._at_level) < self.settings.judged_rounds:
            return
        typical = statistics.median(self._at_level)
        if self._narrowed_from is not None:
            wider, wider_ms = self._narrowed_from
            self._narrowed_from = None
            if typical > wider_ms * self.settings.gain:
                self._far(wider, wider_ms, typical)
                return
            if refusal := self._refused.get(wider):
                refusal.excess = wider_ms / self.predicted(self.levels[wider], typical, self._distance())
        if min(self._at_level) > self.settings.budget_ms and self._narrow(typical, now):
            return
        if self._at > 0 and (excess := self._excess(self._at - 1, now)) is not None and self._pays(self.levels[self._at - 1], typical, excess):
            self._move_to(self._at - 1)

    def _excess(self, at: int, now: float) -> float | None:
        """How far level `at`'s rounds outrun their prediction: 1 unless it is refused (`Refusal`),
        None while its refusal holds whatever the link does."""
        refusal = self._refused.get(at)
        return 1.0 if refusal is None or refusal.until <= now else refusal.excess

    def _distance(self) -> float:
        """The high end of `floor` (0 unknown): what a sharpening reads."""
        return floors[1] if (floors := self.floor) else 0.0

    def _pays(self, sharper: ScreenSpec, typical: float, excess: float) -> bool:
        """Whether `sharper` is worth its bytes, its predicted round scaled by `excess` (`_excess`):
        its picture fits what the budget leaves past the floor; or the link's distance alone is over
        the budget and narrowing back from it would not pay (`_narrow`'s own test), so a far link
        still climbs back to the sharpest picture; or the floor is unknown (`floor`) — only after a
        level held longer than the floor remembers, so a trial at most once per `floor_seconds` on a
        link still full."""
        if (floors := self.floor) is None:
            return True
        floor = floors[1]
        round_ms = self.predicted(sharper, typical, floor) * excess
        if floor >= self.settings.budget_ms:
            return typical > round_ms * self.settings.gain
        return round_ms - floor <= self.settings.headroom * (self.settings.budget_ms - floor)

    def _narrow(self, typical: float, now: float) -> bool:
        """Every judged round over the budget: to the next level that really sends a narrower picture
        (a screen narrower than a rung sends one picture for several), if that is predicted to help;
        False when none is lighter, or the link is far, not full, and a narrower picture buys nothing."""
        width = self.sizing(self.screen_width)[0]
        narrower = next((at for at in range(self._at + 1, len(self.levels))
                         if self.sizing(self.screen_width, self.levels[at])[0] < width), None)
        floor = floors[0] if (floors := self.floor) else 0.0
        if narrower is None or self.predicted(self.levels[narrower], typical, floor) > typical * self.settings.gain:
            return False
        again = now - self._stepped_at < self.settings.retry_max_seconds and self._sharpened_last
        self._retry = min(self._retry * 2, self.settings.retry_max_seconds) if again else self.settings.retry_seconds
        self._refused[self._at] = Refusal(now + self._retry)
        self._narrowed_from = (self._at, self._at_level[-1])
        self._move_to(narrower)
        return True

    def _far(self, wider: int, wider_ms: float, typical: float) -> None:
        """A narrower picture came back no faster: the link moved farther than the floor knew. The
        wider picture returns, and the floor restarts from these rounds, so the next judgment
        predicts with the link's new distance instead of narrowing again."""
        logger.info("screen: %dpx came back no faster than %dpx (%.0f ms against %.0f): the link is far, not full",
                    self.sizing(self.screen_width)[0], self.sizing(self.screen_width, self.levels[wider])[0], typical, wider_ms)
        now, width = self.clock(), self.sizing(self.screen_width)[0]
        self._recent = deque((now, round_ms, width) for round_ms in self._at_level)
        self._refused.pop(wider, None)
        self._move_to(wider)

    def lighten(self) -> bool:
        """One step narrower now (a frame too big to send); False at the lightest."""
        if self._at == len(self.levels) - 1:
            return False
        self._move_to(self._at + 1)
        return True

    def _move_to(self, at: int) -> None:
        lighter = at > self._at
        self._at = at
        self._at_level.clear()
        self._stepped_at, self._sharpened_last = self.clock(), not lighter
        self.lightened += 1 if lighter else 0
        self.sharpened += 0 if lighter else 1
        logger.info("screen: %s to %dpx q%d", "narrowed" if lighter else "sharpened", self.sizing(self.screen_width)[0], self.spec.quality)


class ScreenSession:
    """One live view of this computer: a frame when there is a new one to send, the acts a viewer
    sends back, and what both cost (`ScreenStats`).

    The session is PULLED, never timed from inside: its transport asks for a frame when the link
    has carried the last one, so a slow link lowers the rate by itself instead of queueing frames
    nobody sees. `measured` then walks the quality ladder from the time the link really took."""

    #: Rows sampled for the "has anything changed" digest: every 16th row is ~6% of the pixels and
    #: under a millisecond, where hashing the whole frame costs as much as encoding it.
    DIGEST_ROWS: ClassVar[int] = 16
    #: An unchanged screen still sends a frame this often, so a viewer that joined late, or lost
    #: one, always has a picture.
    IDLE_SECONDS: ClassVar[float] = 2.0
    #: How many timings each step keeps for its percentiles (~30 s at 10 fps).
    SAMPLES: ClassVar[int] = 300
    #: Typed text goes out this many characters at a time, the view's end checked between them: a
    #: long paste (8,000 characters at 20 ms each) kept typing for minutes after Stop.
    TYPE_CHUNK: ClassVar[int] = 16
    #: Why a view ends at the minutes its passkey signed for, whichever side of it notices first.
    MINUTES_OVER: ClassVar[str] = "The minutes this view was opened for are over: open it again."

    def __init__(self, spec: ScreenSpec | None = None, grabber: FrameGrabber | None = None,
                 backend: DesktopBackend | None = None, switch: ScreenSwitch | None = None,
                 marker: ScreenMarker | None = None, viewer: str = "", ends_at: datetime | None = None,
                 control: bool = False) -> None:
        self.ladder = QualityLadder(spec or ScreenSpec())
        self._grabber = grabber
        self._backend = backend
        #: When the offer that opened this session stops covering it (None: a local session, no offer).
        self.ends_at = ends_at
        #: Whether the viewer's acts are taken: a control session is its own offer, its own passkey press.
        self.control = control
        self.switch = switch or ScreenSwitch()
        self.marker = marker or ScreenMarker(self.switch)
        self.viewer = viewer
        self.stats = ScreenStats(width=self.spec.width, quality=self.spec.quality)
        self.sequence = 0
        self.frames: dict[int, ScreenFrame] = {}
        self._allowance: ScreenAllowance | None = None
        #: What the last frame sent showed: its sampled pixels and where the pointer was.
        self._seen: tuple[bytes, tuple[int, int] | None] = (b"", None)
        self._sent_at = 0.0
        self._checked_switch = 0.0
        self._grabs: deque[float] = deque(maxlen=self.SAMPLES)
        self._encodes: deque[float] = deque(maxlen=self.SAMPLES)
        self._sends: deque[float] = deque(maxlen=self.SAMPLES)
        #: Set when the view ends: an act already under way (text being typed) stops at its next chunk.
        self._stopped = threading.Event()

    @property
    def spec(self) -> ScreenSpec:
        """What the next frame is made at: the ladder's current step (`QualityLadder`)."""
        return self.ladder.spec

    @property
    def grabber(self) -> FrameGrabber:
        """Opened on first use, in the thread that then grabs every frame: mss keeps its display
        connection (X11) and device contexts (Windows) per thread."""
        if self._grabber is None:
            self._grabber = FrameGrabber()
        return self._grabber

    @property
    def backend(self) -> DesktopBackend:
        """This computer's desktop, for the acts a viewer sends (capture goes through mss)."""
        if self._backend is None:
            self._backend = host_desktop()
        return self._backend

    def allowed(self) -> ScreenAllowance:
        """The owner's switch, re-read at most once a second: `off` typed on the machine stops the
        session within a frame or two, and the server cannot answer for it."""
        now = time.monotonic()
        if self._allowance is None or now - self._checked_switch >= ScreenSwitch.POLL_SECONDS:
            self._allowance = self.switch.read()
            self._checked_switch = now
        return self._allowance

    def _may_watch(self) -> None:
        """Raise `ScreenEnd` unless this computer still says yes AND still shows its badge:
        the owner's switch is live, Stop was not pressed, and something on this desktop says it is
        being watched. Checked before every frame and every act, never once at the start."""
        allowance = self.allowed()
        if not allowance.live():
            raise ScreenEnd(allowance.reason or "sharing_off", allowance.why or ScreenSwitch.OFF)
        if self.ends_at is not None and datetime.now(UTC) >= self.ends_at:
            raise ScreenEnd("minutes_over", self.MINUTES_OVER)
        if self.marker.ended_itself():
            raise ScreenEnd("stopped_here", "Stop was pressed on this computer.")
        if not self.marker.show(self.viewer):
            raise ScreenEnd("no_badge", "This computer could not show that its screen is being watched, so it is not.")

    def frame(self) -> ScreenFrame | None:
        """The next frame, or None while the screen has not changed since the last one (and the
        idle heartbeat is not due yet): an idle desktop costs the link nothing.

        Raises `ScreenEnd` once the owner's switch is off, its Stop is pressed, or the badge
        cannot be shown — the transport ends the session on it, so no picture leaves after that."""
        self._may_watch()
        rect = self.grabber.rect(self.spec.monitor)
        started = time.perf_counter()
        image = self.grabber.grab(rect)
        grabbed = time.perf_counter()
        # The pointer is not in the grab (the viewer draws it): a tap that only moved it is a change too.
        seen = (self._sample_digest(image), self._spot())
        idle = time.monotonic() - self._sent_at < self.IDLE_SECONDS
        if seen == self._seen and idle:
            self.stats = self.stats.model_copy(update={"unchanged": self.stats.unchanged + 1})
            return None
        self.ladder.screen_width = image.width
        narrowed = self.narrowed(image)
        scale = narrowed.width / image.width
        image = narrowed
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=self.spec.quality, optimize=False)
        encoded = time.perf_counter()
        jpeg = buffer.getvalue()
        if len(jpeg) > SCREEN_FRAME_MAX:  # a 4K desktop at the top of the ladder: narrow it and let the next frame carry it
            self.ladder.lighten()
            self._count_steps()
            return None
        self.sequence += 1
        self._seen, self._sent_at = seen, time.monotonic()
        self._grabs.append((grabbed - started) * 1000)
        self._encodes.append((encoded - grabbed) * 1000)
        frame = ScreenFrame(
            sequence=self.sequence, at=datetime.now(UTC), origin_x=int(rect["left"]), origin_y=int(rect["top"]),
            w=image.width, h=image.height, scale=scale, quality=self.spec.quality, jpeg=jpeg,
            **self._pointer(seen[1], rect, scale),
        )
        self.frames = {frame.sequence: frame, **{key: value for key, value in self.frames.items() if key > self.sequence - 8}}
        self.stats = self.stats.model_copy(update={
            "frames": self.stats.frames + 1, "bytes_sent": self.stats.bytes_sent + len(jpeg),
            "grab": Timing.of(list(self._grabs)), "encode": Timing.of(list(self._encodes)),
            "width": self.spec.width, "quality": self.spec.quality,
        })
        return frame

    def narrowed(self, image: Image.Image) -> Image.Image:
        """`image` at the width the ladder's level sends from it (`QualityLadder.sizing`): reduced by a
        whole number where that lands on the rung, else resized to it. The frame carries the size and
        scale it really has, so a viewer never assumes the width it asked for."""
        width, factor = self.ladder.sizing(image.width)
        if factor is not None:
            return image if factor == 1 else image.reduce(factor)
        # Bilinear with a reducing gap: Pillow reduces by whole numbers first, then filters the rest.
        return image.resize((width, round(image.height * width / image.width)), Image.Resampling.BILINEAR, reducing_gap=2.0)

    def _spot(self) -> tuple[int, int] | None:
        """Where this computer's pointer is on its desktop; None when the OS will not say."""
        try:
            return self.backend.pointer()
        except (DesktopUnsupportedError, OSError, RuntimeError):
            return None

    @staticmethod
    def _pointer(spot: tuple[int, int] | None, rect: dict[str, int], scale: float) -> dict[str, int | None]:
        """The pointer at desktop `spot`, in the frame's own pixels — the capture APIs leave the cursor
        out, so the viewer draws it. None when the OS would not say, or it is off this rectangle."""
        if spot is None:
            return {"pointer_x": None, "pointer_y": None}
        x, y = round((spot[0] - rect["left"]) * scale), round((spot[1] - rect["top"]) * scale)
        inside = 0 <= x <= round(rect["width"] * scale) and 0 <= y <= round(rect["height"] * scale)
        return {"pointer_x": x, "pointer_y": y} if inside else {"pointer_x": None, "pointer_y": None}

    def _sample_digest(self, image: Image.Image) -> bytes:
        """A digest of every `DIGEST_ROWS`-th row: what tells an idle desktop from a changed one
        without paying for the whole picture."""
        pixels = image.tobytes()
        stride = image.width * 3
        return hashlib.blake2b(b"".join(pixels[row * stride:(row + 1) * stride]
                                       for row in range(0, image.height, self.DIGEST_ROWS)), digest_size=16).digest()

    def measured(self, send_ms: float, frame: ScreenFrame) -> None:
        """What the link really took for `frame` (out → its ack back), from the transport that sent
        it: the only number that includes the network. The ladder decides the next step."""
        self._sends.append(send_ms)
        self.ladder.measured(send_ms, frame.w, frame.quality)
        self.stats = self.stats.model_copy(update={"send": Timing.of(list(self._sends))})
        self._count_steps()

    def _count_steps(self) -> None:
        """`stats` as the ladder now stands, so a review can see the link's shape."""
        self.stats = self.stats.model_copy(update={"lightened": self.ladder.lightened, "sharpened": self.ladder.sharpened,
                                                   "width": self.spec.width, "quality": self.spec.quality})

    def perform(self, acts: list[object]) -> int:
        """Take what a viewer sent — moves, clicks, scrolls, keys, text, in the galaius action
        names — on this computer, in order. Returns how many landed.

        Coordinates arrive in the pixels of the frame the viewer read (`ScreenPoint.frame`), and
        are mapped here with THAT frame's own origin and scale: a viewer never multiplies, so a
        stream that narrowed between the picture and the click still lands on the same pixel."""
        self._may_watch()
        if not self.control:
            raise PermissionError("This is a view: controlling the computer is its own offer, its own passkey press.")
        done = 0
        for raw in acts[:SCREEN_ACTS_MAX]:
            self._still_acting()
            try:
                act = SCREEN_ACTS.validate_python(raw)
            except ValidationError:
                logger.warning("screen: an act was refused as malformed")
                continue
            self._take(act)
            done += 1
        return done

    def _take(self, act: ScreenAct) -> None:
        if isinstance(act, ScreenPoint):
            x, y = self._mapped(act)
            if act.type == "move":
                self.backend.move(x, y)
            elif act.type == "click":
                self.backend.click(x, y, button=act.button, count=act.count)
            else:
                self.backend.move(x, y)
                self.backend.scroll(act.clicks, horizontal=act.horizontal)
        elif act.type == "key_press":
            self.backend.key(act.key)
        else:
            for start in range(0, len(act.text), self.TYPE_CHUNK):
                self._still_acting()
                self.backend.type_text(act.text[start:start + self.TYPE_CHUNK])

    def _still_acting(self) -> None:
        if self._stopped.is_set():
            raise PermissionError("This view ended: what was left of its acts is not taken.")

    def stop_acting(self) -> None:
        """The view is over: an act under way stops at its next chunk, and none after it starts.
        Called from the stream's own thread, never the acting one."""
        self._stopped.set()

    def _mapped(self, act: ScreenPoint) -> tuple[int, int]:
        """`act`'s point in this computer's input units, read through the frame it was taken from —
        the latest frame when that one has already been forgotten (8 frames back)."""
        frame = self.frames.get(act.frame) or (self.frames[max(self.frames)] if self.frames else None)
        if frame is None:
            raise LookupError("no frame has been sent yet, so a point on it cannot be placed")
        return frame.input_point(act.x, act.y)

    def readiness(self) -> ScreenReadiness:
        """What this computer says about being watched — what the web shows before any frame."""
        return ScreenReadiness(allowance=self.allowed(), monitors=tuple(self.grabber.monitors()),
                               viewing=self.stats.frames > 0, stats=self.stats)

    def close(self) -> None:
        self.stop_acting()
        self.marker.stop()
        if self._grabber is not None:
            self._grabber.close()
        logger.info("screen session ended: %s", self.stats.said)


class ScreenStreamer:
    """What turns a session into a stream: one frame in flight at a time, the link's own pace, and
    an end that needs nobody's cooperation.

    `send` is the transport (it returns once the other side has the frame, which is what makes the
    measured time include the link); it belongs to whoever owns the connection, so this driver
    carries no transport and decides nothing about who may watch. The stream ends when the viewer
    stops asking (`VIEWER_GONE_SECONDS` with no frame taken), when the machine's own switch or
    badge says stop (`ScreenEnd` out of the session), or when the transport closes.

    An act the viewer sent (`acted`) is what he waits to see: the screen is grabbed every
    `ACT_POLL_SECONDS` until its change goes out, and that frame leaves beside the one in flight,
    past the frame rate's pace — a tap waited up to a whole round for the frame ahead of it (134 ms
    at 1280 px on a phone link, perf-critic r1), on top of its own."""

    #: No frame taken for this long: the browser is gone (a phone locked, a tab closed) and the
    #: session ends by itself — nothing waits for the server to notice.
    VIEWER_GONE_SECONDS: ClassVar[float] = 20.0
    #: How long a transport may take for one frame before the stream treats the link as gone.
    SEND_TIMEOUT_SECONDS: ClassVar[float] = 15.0
    #: Grabbing and encoding one frame this long is logged: a stall the viewer sees must be placed
    #: on this computer or on the link (the transport logs its own slow rounds).
    SLOW_FRAME_MS: ClassVar[float] = 200.0
    #: Frames out at once: one, and one more for an act's picture.
    IN_FLIGHT: ClassVar[int] = 2
    #: After an act, the screen is grabbed this often until it changes: one refresh of a 60 Hz display.
    ACT_POLL_SECONDS: ClassVar[float] = 1 / 60
    #: ...for this long at most: an act that changed nothing (a move over a still window) stops it.
    ACT_WATCH_SECONDS: ClassVar[float] = 0.5

    def __init__(self, session: ScreenSession, send: Callable[[ScreenFrame], Awaitable[None]]) -> None:
        self.session = session
        self.send = send
        #: Why it ended (its sentence and its typed reason, `ScreenEnd`); None: asked to.
        self.ended: ScreenEnd | None = None
        #: The one thread that grabs and encodes — and walks the ladder, so a session is only ever
        #: touched from it: the event loop stays free for the link, and mss stays on the thread it
        #: opened on.
        self._grabbing = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="galaius-screen")
        #: When the last act landed and when the newest frame sent was grabbed (`time.monotonic`).
        self._acted_at = self._grabbed_at = 0.0
        self._woken = asyncio.Event()
        #: When the viewer last took a frame (its ack came back).
        self._taken = 0.0

    def acted(self) -> None:
        """An act the viewer sent landed on this computer (called on the stream's loop)."""
        self._acted_at = time.monotonic()
        self._woken.set()

    @property
    def _act_waiting(self) -> bool:
        """An act landed after the newest frame sent was grabbed, lately enough to still watch for it."""
        return self._acted_at > self._grabbed_at and time.monotonic() - self._acted_at < self.ACT_WATCH_SECONDS

    async def run(self) -> ScreenStats:
        """Stream until it ends, and say why (`ended`) and what it cost."""
        slow, slow_said = 0, 0.0
        loop = asyncio.get_running_loop()
        self._taken = time.monotonic()
        in_flight: set[asyncio.Task[None]] = set()
        # The viewer asked for no more than this many frames a second, counted over the WHOLE cycle
        # (grab, encode and link): pacing from the link alone paid the grab twice and held a 30 fps
        # stream to 17. `polled`: the next grab after one that found nothing new.
        paced = polled = 0.0
        try:
            while True:
                self._settle(in_flight)
                while (due := self._due(in_flight, paced, polled)) is None or due > time.perf_counter():
                    await self._wait(in_flight, None if due is None else due - time.perf_counter())
                    self._settle(in_flight)
                cycle, grabbed_at = time.perf_counter(), time.monotonic()
                frame = await loop.run_in_executor(self._grabbing, self.session.frame)
                if (made_ms := (time.perf_counter() - cycle) * 1000) >= self.SLOW_FRAME_MS:
                    slow += 1
                    if time.monotonic() - slow_said >= 1.0:  # one line a second, however many were slow
                        logger.info("screen: %d frame(s) took ≥%.0f ms on this computer, the last %.0f ms", slow, self.SLOW_FRAME_MS, made_ms)
                        slow, slow_said = 0, time.monotonic()
                if frame is None:
                    polled = time.perf_counter() + (self.ACT_POLL_SECONDS if self._act_waiting
                                                    else min(1.0 / self.session.spec.max_fps, self.session.IDLE_SECONDS))
                    if not in_flight and time.monotonic() - self._taken > self.VIEWER_GONE_SECONDS:
                        self.ended = ScreenEnd("viewer_gone", "the viewer went away")
                        break
                    continue
                self._grabbed_at = grabbed_at
                in_flight.add(asyncio.create_task(self._round(frame, loop)))
                paced = cycle + 1.0 / self.session.spec.max_fps
        except ScreenEnd as end:
            self.ended = end
        except ConnectionError:  # the error's own text stays in the log: the page is told only what ended it
            logger.warning("screen: the connection ended under the view", exc_info=True)
            self.ended = ScreenEnd("link_lost", "the connection to the viewer ended")
        except OSError:  # this computer's display or capture failed under the view
            logger.exception("screen: capture failed under the view")
            self.ended = ScreenEnd("failed", "this computer failed under the view")
        finally:
            for task in in_flight:
                task.cancel()
            # The session closes on the grabbing thread (mss's handles live there), even when this
            # task is cancelled: the badge comes down whatever ended the stream.
            closing = loop.run_in_executor(self._grabbing, self.session.close)
            self._grabbing.shutdown(wait=False)
            await asyncio.shield(closing)
        logger.info("screen stream ended (%s): %s", self.ended or "asked to", self.session.stats.said)
        return self.session.stats

    async def _round(self, frame: ScreenFrame, loop: asyncio.AbstractEventLoop) -> None:
        """One frame out and back; what it took walks the ladder, on the grabbing thread."""
        started = time.perf_counter()
        try:
            await asyncio.wait_for(self.send(frame), timeout=self.SEND_TIMEOUT_SECONDS)
        except TimeoutError:
            raise ScreenEnd("link_lost", "the link stopped carrying frames") from None
        await loop.run_in_executor(self._grabbing, self.session.measured, (time.perf_counter() - started) * 1000, frame)
        self._taken = time.monotonic()

    def _due(self, in_flight: set[asyncio.Task[None]], paced: float, polled: float) -> float | None:
        """When the next grab may start (`time.perf_counter`), or None while it waits for a round or an
        act: an act's picture once a poll is due, past the pace and beside one frame in flight; any
        other once nothing is in flight and both are due."""
        if self._act_waiting and len(in_flight) < self.IN_FLIGHT:
            return polled
        return None if in_flight else max(paced, polled)

    @staticmethod
    def _settle(in_flight: set[asyncio.Task[None]]) -> None:
        """Rounds that ended leave `in_flight`; one that failed raises what ended it."""
        for task in [task for task in in_flight if task.done()]:
            in_flight.discard(task)
            task.result()

    async def _wait(self, in_flight: set[asyncio.Task[None]], seconds: float | None) -> None:
        """Until a round ends, an act lands, or `seconds` pass (None: no limit)."""
        self._woken.clear()
        woken = asyncio.ensure_future(self._woken.wait())
        try:
            await asyncio.wait({*in_flight, woken}, timeout=None if seconds is None else max(seconds, 0.0),
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            woken.cancel()
