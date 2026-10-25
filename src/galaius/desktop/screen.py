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
import logging
import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import ClassVar

import mss
from galaius_core import (
    SCREEN_ACTS,
    SCREEN_ACTS_MAX,
    SCREEN_FRAME_MAX,
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
from pydantic import ValidationError

from galaius.desktop.backend import DesktopBackend, DesktopUnsupportedError
from galaius.desktop.host import host_desktop
from galaius.desktop.screen_marker import ScreenMarker
from galaius.desktop.screen_switch import ScreenSwitch

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
    #: Frames that must fit in half the budget before the picture sharpens again.
    EASY_FRAMES: ClassVar[int] = 5
    #: A grab this close to the asked width is sent as it is (see `narrowed`).
    NEAR_ENOUGH: ClassVar[float] = 1.2

    def __init__(self, spec: ScreenSpec | None = None, grabber: FrameGrabber | None = None,
                 backend: DesktopBackend | None = None, switch: ScreenSwitch | None = None,
                 marker: ScreenMarker | None = None, viewer: str = "", ends_at: datetime | None = None,
                 control: bool = False) -> None:
        self.spec = spec or ScreenSpec()
        self.asked = self.spec
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
        self._digest = b""
        self._sent_at = 0.0
        self._checked_switch = 0.0
        self._easy = 0
        self._grabs: list[float] = []
        self._encodes: list[float] = []
        self._sends: list[float] = []

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
        """Raise `PermissionError` unless this computer still says yes AND still shows its badge:
        the owner's switch is live, Stop was not pressed, and something on this desktop says it is
        being watched. Checked before every frame and every act, never once at the start."""
        allowance = self.allowed()
        if not allowance.live():
            raise PermissionError(allowance.why or "This computer's screen sharing is off.")
        if self.ends_at is not None and datetime.now(UTC) >= self.ends_at:
            raise PermissionError("The minutes this view was opened for are over: open it again.")
        if self.marker.ended_itself():
            raise PermissionError("Stop was pressed on this computer.")
        if not self.marker.show(self.viewer):
            raise PermissionError("This computer could not show that its screen is being watched, so it is not.")

    def frame(self) -> ScreenFrame | None:
        """The next frame, or None while the screen has not changed since the last one (and the
        idle heartbeat is not due yet): an idle desktop costs the link nothing.

        Raises `PermissionError` once the owner's switch is off, its Stop is pressed, or the badge
        cannot be shown — the transport ends the session on it, so no picture leaves after that."""
        self._may_watch()
        rect = self.grabber.rect(self.spec.monitor)
        started = time.perf_counter()
        image = self.grabber.grab(rect)
        grabbed = time.perf_counter()
        digest = self._sample_digest(image)
        idle = time.monotonic() - self._sent_at < self.IDLE_SECONDS
        if digest == self._digest and idle:
            self.stats = self.stats.model_copy(update={"unchanged": self.stats.unchanged + 1})
            return None
        narrowed = self.narrowed(image)
        scale = narrowed.width / image.width
        image = narrowed
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=self.spec.quality, optimize=False)
        encoded = time.perf_counter()
        jpeg = buffer.getvalue()
        if len(jpeg) > SCREEN_FRAME_MAX:  # a 4K desktop at the top of the ladder: narrow it and let the next frame carry it
            self._step(lighter=True)
            return None
        self.sequence += 1
        self._digest, self._sent_at = digest, time.monotonic()
        self._grabs.append((grabbed - started) * 1000)
        self._encodes.append((encoded - grabbed) * 1000)
        frame = ScreenFrame(
            sequence=self.sequence, at=datetime.now(UTC), origin_x=int(rect["left"]), origin_y=int(rect["top"]),
            w=image.width, h=image.height, scale=scale, quality=self.spec.quality, jpeg=jpeg,
            **self._pointer(rect, scale),
        )
        self.frames = {frame.sequence: frame, **{key: value for key, value in self.frames.items() if key > self.sequence - 8}}
        self.stats = self.stats.model_copy(update={
            "frames": self.stats.frames + 1, "bytes_sent": self.stats.bytes_sent + len(jpeg),
            "grab": Timing.of(self._grabs[-self.SAMPLES:]), "encode": Timing.of(self._encodes[-self.SAMPLES:]),
            "width": self.spec.width, "quality": self.spec.quality,
        })
        return frame

    def narrowed(self, image: Image.Image) -> Image.Image:
        """`image` no wider than the spec asks, by whole-number reduction (`Image.reduce`): on this
        machine a 1920-wide grab for a 1280 ceiling costs 4.1 ms this way against 19.2 ms for a
        resize to exactly 1280 — and the smaller picture is 31 KB against 47 KB. The frame carries
        the size and scale it really has, so a viewer never assumes the width it asked for.

        A grab already within `NEAR_ENOUGH` of the ceiling is sent as it is: reducing a 1366-wide
        screen for a 1280 ceiling would halve it to 683 to save a few kilobytes."""
        if image.width <= self.spec.width * self.NEAR_ENOUGH:
            return image
        return image.reduce(math.ceil(image.width / self.spec.width))

    def _pointer(self, rect: dict[str, int], scale: float) -> dict[str, int | None]:
        """Where this computer's pointer is, in the frame's own pixels — the capture APIs leave the
        cursor out, so the viewer draws it. None when the OS will not say, or it is off this
        rectangle."""
        try:
            spot = self.backend.pointer()
        except (DesktopUnsupportedError, OSError, RuntimeError):
            return {"pointer_x": None, "pointer_y": None}
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

    def measured(self, send_ms: float) -> None:
        """What the link really took for the last frame, from the transport that sent it: the only
        number that includes the network. The stream steps down the moment a frame misses its
        budget, and back up only after `EASY_FRAMES` frames fit in half of it — a link that
        recovers for one frame and fails again must not flap the picture every second."""
        self._sends.append(send_ms)
        budget = self.spec.frame_budget_ms
        if send_ms > budget:
            self._easy = 0
            self._step(lighter=True)
        elif send_ms <= budget / 2:
            self._easy += 1
            if self._easy >= self.EASY_FRAMES:
                self._easy = 0
                self._step(lighter=False)
        else:
            self._easy = 0
        self.stats = self.stats.model_copy(update={"send": Timing.of(self._sends[-self.SAMPLES:])})

    def _step(self, lighter: bool) -> None:
        """Walk the ladder one step; `stats` counts each move so a review can see the link's shape."""
        stepped = self.spec.lighter() if lighter else self.spec.sharper()
        if (stepped.width, stepped.quality) == (self.spec.width, self.spec.quality):
            return
        if not lighter and (stepped.width > self.asked.width or stepped.quality > self.asked.quality):
            return  # never sharper than the viewer asked for
        self.spec = stepped
        self.stats = self.stats.model_copy(update={
            "lightened": self.stats.lightened + (1 if lighter else 0),
            "sharpened": self.stats.sharpened + (0 if lighter else 1),
            "width": stepped.width, "quality": stepped.quality,
        })
        logger.info("screen: %s to %dpx q%d", "narrowed" if lighter else "sharpened", stepped.width, stepped.quality)

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
            self.backend.type_text(act.text)

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
    badge says stop (`PermissionError` out of the session), or when the transport closes."""

    #: No frame taken for this long: the browser is gone (a phone locked, a tab closed) and the
    #: session ends by itself — nothing waits for the server to notice.
    VIEWER_GONE_SECONDS: ClassVar[float] = 20.0
    #: How long a transport may take for one frame before the stream treats the link as gone.
    SEND_TIMEOUT_SECONDS: ClassVar[float] = 15.0

    def __init__(self, session: ScreenSession, send: Callable[[ScreenFrame], Awaitable[None]]) -> None:
        self.session = session
        self.send = send
        self.ended = ""
        #: The one thread that grabs and encodes: the event loop stays free for the link, and mss
        #: stays on the thread it opened on.
        self._grabbing = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="galaius-screen")

    async def run(self) -> ScreenStats:
        """Stream until it ends, and say why (`ended`) and what it cost."""
        taken = time.monotonic()
        loop = asyncio.get_running_loop()
        try:
            while True:
                cycle = time.perf_counter()
                frame = await loop.run_in_executor(self._grabbing, self.session.frame)
                if frame is None:
                    await asyncio.sleep(min(1.0 / self.session.spec.max_fps, self.session.IDLE_SECONDS))
                    if time.monotonic() - taken > self.VIEWER_GONE_SECONDS:
                        self.ended = "the viewer went away"
                        break
                    continue
                started = time.perf_counter()
                try:
                    await asyncio.wait_for(self.send(frame), timeout=self.SEND_TIMEOUT_SECONDS)
                except TimeoutError:
                    self.ended = "the link stopped carrying frames"
                    break
                self.session.measured((time.perf_counter() - started) * 1000)
                taken = time.monotonic()
                # The viewer asked for no more than this many frames a second, counted over the
                # WHOLE cycle (grab, encode and link): pacing from the link alone paid the grab
                # twice and held a 30 fps stream to 17.
                await asyncio.sleep(max(1.0 / self.session.spec.max_fps - (time.perf_counter() - cycle), 0))
        except PermissionError as refusal:
            self.ended = str(refusal)
        except (ConnectionError, OSError) as error:
            self.ended = f"the connection ended: {error}"
        finally:
            # The session closes on the grabbing thread (mss's handles live there), even when this
            # task is cancelled: the badge comes down whatever ended the stream.
            closing = loop.run_in_executor(self._grabbing, self.session.close)
            self._grabbing.shutdown(wait=False)
            await asyncio.shield(closing)
        logger.info("screen stream ended (%s): %s", self.ended or "asked to", self.session.stats.said)
        return self.session.stats
