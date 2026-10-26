"""This computer's screen over its machine link: the requests the server relays (`ScreenDesk.answer`),
the viewer's sealed messages (`ScreenDesk.take`), and the one live session they drive (`LiveScreen`).

The link is the server's; nothing on it is the server's to decide. A session opens for an offer its
owner's passkey signed (`ScreenGate`), every frame leaves sealed for the viewer alone and every act
arrives sealed by it (`ScreenKeys`), the session ends at the minute its offer named, and the switch
and badge on this computer end it whatever the server says (`ScreenSession`). One session at a time:
a newer verified offer ends the older one (a phone that reconnected), never runs beside it.
"""

import asyncio
import concurrent.futures
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID

from galaius_core import (
    SCREEN_PLAIN_FRAME,
    SCREEN_PLAIN_STATS,
    MachineAgentAnswer,
    ScreenFrame,
    ScreenOpened,
    ScreenOpenRequest,
    ScreenReadiness,
    ScreenSealed,
    ScreenStatusRequest,
    ScreenViewerMessage,
    Timing,
    b64url,
)
from pydantic import ValidationError

from galaius.desktop.backend import DesktopUnsupportedError
from galaius.desktop.screen import FrameGrabber, ScreenSession, ScreenStreamer
from galaius.desktop.screen_switch import ScreenSwitch
from galaius.desktop.screen_trust import (
    Admitted,
    ScreenGap,
    ScreenGate,
    ScreenIdentity,
    ScreenRefused,
    ScreenTrustFile,
)

logger = logging.getLogger("galaius")

#: What the desk sends on the machine link: one JSON message (the runner's socket `send`).
Send = Callable[[dict[str, object]], Awaitable[None]]


class LiveScreen:
    """The one session being watched: its keys, its stream, the frames waiting for the viewer's
    ack, and the timer that ends it when its offer runs out."""

    #: The stream's numbers go to the viewer this often (sealed like a frame).
    STATS_SECONDS: ClassVar[float] = 2.0
    #: A frame whose ack takes this long is logged with where its time went (`_deliver`): a stall
    #: the viewer sees must be placed on this computer, the link out, or the way back.
    SLOW_ROUND_MS: ClassVar[float] = 400.0

    def __init__(self, admitted: Admitted, session: ScreenSession, send: Send, ended: Callable[["LiveScreen"], Awaitable[None]]) -> None:
        self.admitted = admitted
        self.session_view = session
        self.send_message = send
        self._ended = ended
        self.acks: dict[int, asyncio.Future[float]] = {}
        self._drawn: list[float] = []
        self._stats_at = 0.0
        self.why = ""
        self.streamer = ScreenStreamer(session, self._deliver)
        #: The one thread a view's acts run on, in the order their messages came: a worker per
        #: message let the text typed after a tap land before the tap.
        self._acting = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="galaius-screen-acts")
        self.task = asyncio.create_task(self._run())

    @property
    def session(self) -> UUID:
        return self.admitted.session

    async def _run(self) -> None:
        seconds = max((self.admitted.ends_at - datetime.now(UTC)).total_seconds(), 0.0)
        try:
            await asyncio.wait_for(self.streamer.run(), timeout=seconds)
            self.why = self.why or self.streamer.ended or "the stream ended"
        except TimeoutError:
            self.why = "The minutes this view was opened for are over: open it again."
        except asyncio.CancelledError:
            self.why = self.why or "this view was ended"
        finally:
            for waiting in self.acks.values():
                waiting.cancel()
            self.session_view.stop_acting()
            self._acting.shutdown(wait=False, cancel_futures=True)
            await self._ended(self)

    async def _sealed(self, plain: bytes) -> None:
        sealed = self.admitted.keys.seal(plain)
        try:
            await self.send_message({"type": "screen_frame", **sealed.model_dump(mode="json")})
        except Exception as error:  # the runner's socket closed under the stream: the link is gone
            raise ConnectionError(f"the link to the server dropped ({type(error).__name__})") from error

    async def _deliver(self, frame: ScreenFrame) -> None:
        """`ScreenStreamer`'s transport: the frame sealed for the viewer, returned once the viewer
        says it drew it — so the stream's measured time is the whole round, link included."""
        header = frame.model_dump_json(exclude={"jpeg"}).encode()
        waiting = self.acks[frame.sequence] = asyncio.get_running_loop().create_future()
        started = time.perf_counter()
        try:
            await self._sealed(bytes([SCREEN_PLAIN_FRAME]) + len(header).to_bytes(4, "big") + header + frame.jpeg)
            handed = time.perf_counter()
            drawn_ms = await waiting
        finally:
            self.acks.pop(frame.sequence, None)
        if (round_ms := (time.perf_counter() - started) * 1000) >= self.SLOW_ROUND_MS:
            logger.info("screen: frame %d (%d KB) came back after %.0f ms: %.0f ms handing it to the link, %.0f ms waiting for its ack "
                        "(the viewer drew it in %.0f ms; frame made %.0f ms before it left)", frame.sequence, len(frame.jpeg) // 1024, round_ms,
                        (handed - started) * 1000, (time.perf_counter() - handed) * 1000, drawn_ms,
                        (datetime.now(UTC) - frame.at).total_seconds() * 1000 - round_ms)
        if time.monotonic() - self._stats_at >= self.STATS_SECONDS:
            self._stats_at = time.monotonic()
            stats = self.session_view.stats.model_copy(update={"drawn": Timing.of(self._drawn[-ScreenSession.SAMPLES:])})
            await self._sealed(bytes([SCREEN_PLAIN_STATS]) + stats.model_dump_json().encode())

    def take(self, sealed: ScreenSealed) -> None:
        """One sealed viewer message: its ack frees the stream, its acts (control only) are taken
        off the loop, one message after another. A replay, a forgery or a malformed message is
        dropped and logged — never acted on. A gap (a message before it lost or withheld on the way)
        ends the view: what follows a missing act is never acted on out of order."""
        try:
            message = ScreenViewerMessage.model_validate_json(self.admitted.keys.open(sealed))
        except ScreenGap as gap:
            logger.warning("screen: %s; the view ends", gap)
            self.why = f"A message from the viewer was lost on the way ({gap}), so the view ended rather than act out of order."
            self.task.cancel()
            return
        except (ScreenRefused, ValidationError, ValueError) as error:
            logger.warning("screen: a viewer message was dropped: %s", error)
            return
        if message.ack is not None:
            if message.drawn_ms is not None:
                self._drawn.append(message.drawn_ms)
            for sequence, waiting in tuple(self.acks.items()):
                if sequence <= message.ack and not waiting.done():
                    waiting.set_result(message.drawn_ms or 0.0)
        if message.acts:
            done = asyncio.get_running_loop().run_in_executor(self._acting, self.session_view.perform, message.acts)
            done.add_done_callback(self._performed)

    def _performed(self, done: asyncio.Future) -> None:
        if done.cancelled():
            return
        if isinstance(error := done.exception(), PermissionError):
            logger.warning("screen: acts refused: %s", error)
        elif error is not None:
            logger.warning("screen: an act failed", exc_info=error)

    async def end(self, why: str) -> None:
        self.why = self.why or why
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task


class ScreenDesk:
    """Every screen request and message the runner receives, answered here (`MachineRunner`)."""

    def __init__(self, machine_id: UUID, server_url: str, switch: ScreenSwitch | None = None, trust: ScreenTrustFile | None = None,
                 identity: ScreenIdentity | None = None, session: Callable[..., ScreenSession] = ScreenSession,
                 grabber: Callable[[], FrameGrabber] = FrameGrabber) -> None:
        self.switch = switch or ScreenSwitch()
        self.make_grabber = grabber
        self.gate = ScreenGate(machine_id, server_url, trust)
        self.identity = identity or ScreenIdentity()
        self.make_session = session
        self.live: LiveScreen | None = None

    async def answer(self, request: ScreenStatusRequest | ScreenOpenRequest, send: Send) -> MachineAgentAnswer:
        """The PC's answer to one relayed request; a refusal in its owner's words (`error`)."""
        try:
            if isinstance(request, ScreenOpenRequest):
                return MachineAgentAnswer(request_id=request.id, screen_opened=await self._open(request, send))
            return MachineAgentAnswer(request_id=request.id, screen=await asyncio.to_thread(self.readiness))
        except PermissionError as refusal:
            logger.warning("screen %s refused: %s", request.op, refusal)
            return MachineAgentAnswer(request_id=request.id, error=str(refusal)[:400])

    def readiness(self) -> ScreenReadiness:
        trust = self.gate.trust.read()
        unsupported, monitors = "", ()
        try:
            grabber = self.make_grabber()
        except DesktopUnsupportedError as error:
            unsupported = str(error)
        else:
            try:
                monitors = tuple(grabber.monitors())
            finally:
                grabber.close()
        live = self.live
        return ScreenReadiness(allowance=self.switch.read(), monitors=monitors, viewing=live is not None, unsupported=unsupported,
                               stats=live.session_view.stats if live is not None else None, passkeys=trust.trusted, pending=len(trust.pending),
                               identity=self.identity.public, origin=self.gate.origin, rp_id=self.gate.rp_id)

    async def _open(self, request: ScreenOpenRequest, send: Send) -> ScreenOpened:
        admitted = await asyncio.to_thread(self.gate.admit, request.offer, request.assertion, self.switch.live())
        if self.live is not None:
            await self.live.end("a newer view of this computer was opened")
        offer = admitted.offer
        session = self.make_session(spec=offer.spec, viewer=request.viewer, ends_at=admitted.ends_at, control=offer.mode == "control", switch=self.switch)
        self.live = LiveScreen(admitted, session, send, self._ended)
        logger.warning("screen %s opened for %s… (%s, until %s)", admitted.session, admitted.passkey.credential_id[:8], offer.mode, f"{admitted.ends_at:%H:%M}Z")
        return ScreenOpened(session=admitted.session, pc_key=b64url(admitted.keys.pc_key), identity=self.identity.public,
                            proof=self.identity.prove(admitted.challenge, admitted.keys.pc_key, admitted.session), mode=offer.mode,
                            ends_at=admitted.ends_at, paired=admitted.paired)

    async def _ended(self, live: LiveScreen) -> None:
        if self.live is live:
            self.live = None
        logger.warning("screen %s ended (%s): %s", live.session, live.why, live.session_view.stats.said)
        with contextlib.suppress(Exception):  # the link may be the thing that ended it
            await live.send_message({"type": "screen_ended", "session": str(live.session), "why": live.why[:400]})

    def take(self, sealed: ScreenSealed) -> None:
        """A relayed viewer message (`screen_input`); one for no live session is dropped."""
        if self.live is not None and self.live.session == sealed.session:
            self.live.take(sealed)

    def take_raw(self, message: dict[str, object]) -> None:
        """`take` for the message as the link carried it; a malformed one is dropped."""
        try:
            self.take(ScreenSealed.model_validate(message))
        except ValidationError:
            logger.warning("screen: a malformed viewer message was dropped")

    async def close(self, session: UUID | str, why: str) -> None:
        """End `session` (the viewer left, the server says so): ending never needs a passkey."""
        if self.live is not None and str(self.live.session) == str(session):
            await self.live.end(why)

    async def close_all(self, why: str) -> None:
        if self.live is not None:
            await self.live.end(why)

