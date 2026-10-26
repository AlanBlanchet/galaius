"""This computer's screen over its machine link (`galaius.desktop.screen_desk`), played end to end
against the browser's half (`tests.support.screen.Viewer`) with the server as a dumb relay.

Owner 2026-10-23: « maybe we could access the PC from here? … view the PC, like a TeamViewer or a
Parsec from the Data tab. » Threat review (binding): frames and input sealed end to end, control its
own passkey press, a session's life at most 30 minutes, the PC's own stop beyond the server's reach.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from galaius_core import (
    SCREEN_PLAIN_FRAME,
    SCREEN_PLAIN_STATS,
    MachineRef,
    ScreenAllowance,
    ScreenOffer,
    ScreenOpenRequest,
    ScreenSealed,
    ScreenStatusRequest,
)

from galaius.desktop.screen import ScreenSession
from galaius.desktop.screen_desk import ScreenDesk
from galaius.desktop.screen_switch import ScreenSwitch
from galaius.desktop.screen_trust import ScreenIdentity, ScreenTrustFile
from tests.support.screen import (
    MACHINE,
    ORIGIN,
    FakeBackend,
    FakeGrabber,
    FakeMarker,
    SoftPasskey,
    Viewer,
)


class Relay:
    """The server between the two: it keeps what the PC sent, and can do nothing else with it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.arrived = asyncio.Event()

    async def send(self, message: dict) -> None:
        self.sent.append(json.loads(json.dumps(message)))  # what crosses the wire, not the objects
        self.arrived.set()

    async def next(self, kind: str, after: int = 0) -> tuple[int, dict]:
        for _ in range(200):
            for index, message in enumerate(self.sent[after:], after):
                if message["type"] == kind:
                    return index, message
            self.arrived.clear()
            await asyncio.wait_for(self.arrived.wait(), 2)
        raise AssertionError(f"no {kind}")


@pytest.fixture
def desk(tmp_path):
    switch = ScreenSwitch(tmp_path / "screen.json")
    switch.write(ScreenAllowance(allowed=True, until=datetime.now(UTC) + timedelta(hours=1)))
    backend = FakeBackend()
    desk = ScreenDesk(MACHINE, f"{ORIGIN}/plateform", switch=switch, trust=ScreenTrustFile(tmp_path / "keys.json"),
                      identity=ScreenIdentity(tmp_path / "id.pem"),
                      session=lambda **options: ScreenSession(grabber=FakeGrabber(), backend=backend, marker=FakeMarker(), **options),
                      grabber=FakeGrabber)
    desk.backend = backend
    return desk


def _request(kind, **fields):
    return kind(id=uuid4(), machine=MachineRef(id=MACHINE), workspace_id=uuid4(), initiator_account=uuid4(),
                expires_at=datetime.now(UTC) + timedelta(seconds=20), signature="0" * 64, **fields)


async def _open(desk, relay, phone, mode="view", new=False, minutes=10):
    viewer = Viewer()
    text = viewer.offer(mode=mode, minutes=minutes)
    answer = await desk.answer(_request(ScreenOpenRequest, offer=text, assertion=phone.assert_(text, new=new), viewer="Alan"), relay.send)
    assert answer.error is None, answer.error
    viewer.start(answer.screen_opened.pc_key, ScreenOffer.challenge(text))
    return viewer, answer.screen_opened


def _frame(viewer, message):
    plain = viewer.open(ScreenSealed.model_validate(message))
    assert plain[0] == SCREEN_PLAIN_FRAME
    size = int.from_bytes(plain[1:5], "big")
    return json.loads(plain[5:5 + size]), plain[5 + size:]


async def test_a_passkey_opens_a_sealed_stream_the_relay_cannot_read_and_control_clicks_where_it_was_read(desk) -> None:
    relay, phone = Relay(), SoftPasskey()
    status = await desk.answer(_request(ScreenStatusRequest), relay.send)
    assert status.screen.allowance.live() and not status.screen.passkeys and status.screen.rp_id == "galaius.ai"

    viewer, opened = await _open(desk, relay, phone, mode="control", new=True)
    assert opened.paired and opened.mode == "control" and opened.identity == status.screen.identity
    index, sealed = await relay.next("screen_frame")
    assert b"\xff\xd8" not in json.dumps(sealed).encode(), "the relay holds only ciphertext"
    header, jpeg = _frame(viewer, sealed)
    assert jpeg[:2] == b"\xff\xd8" and header["sequence"] == 1 and header["w"] == 960

    # The viewer clicks at (480, 270) of the 960-wide frame it drew: the 1920-wide screen's centre.
    act = {"type": "click", "x": 480, "y": 270, "frame": header["sequence"]}
    desk.take(viewer.seal(opened.session, 1, json.dumps({"ack": header["sequence"], "drawn_ms": 4.0, "acts": [act]}).encode()))
    for _ in range(100):
        if desk.backend.done:
            break
        await asyncio.sleep(0.01)
    assert desk.backend.done == [("click", 960, 540, "left", 1)]
    _, stats = await relay.next("screen_frame", index + 1)  # after the ack: the next frame, or the stream's numbers
    assert viewer.open(ScreenSealed.model_validate(stats))[0] in {SCREEN_PLAIN_FRAME, SCREEN_PLAIN_STATS}

    # The same sealed act again is a replay: dropped, never clicked twice.
    desk.take(viewer.seal(opened.session, 1, json.dumps({"acts": [act]}).encode()))
    await asyncio.sleep(0.05)
    assert len(desk.backend.done) == 1
    await desk.close(opened.session, "the viewer closed the page")
    _, ended = await relay.next("screen_ended")
    assert ended["session"] == str(opened.session) and desk.live is None


async def test_a_view_session_takes_no_act_and_a_newer_open_replaces_it(desk) -> None:
    relay, phone = Relay(), SoftPasskey()
    viewer, first = await _open(desk, relay, phone, new=True)
    _, sealed = await relay.next("screen_frame")
    header, _ = _frame(viewer, sealed)
    desk.take(viewer.seal(first.session, 1, json.dumps({"ack": header["sequence"], "acts": [{"type": "type_text", "text": "rm -rf ~"}]}).encode()))
    await asyncio.sleep(0.05)
    assert desk.backend.done == [], "typing needs its own control offer, its own passkey press"

    _, second = await _open(desk, relay, phone)
    _, ended = await relay.next("screen_ended")
    assert ended["session"] == str(first.session) and desk.live.session == second.session
    await desk.close_all("the link to the server dropped")


async def test_a_session_ends_when_its_signed_minutes_are_over(desk, monkeypatch) -> None:
    relay, phone = Relay(), SoftPasskey()
    _, opened = await _open(desk, relay, phone, new=True, minutes=0.02)
    _, ended = await relay.next("screen_ended")
    assert ended["session"] == str(opened.session) and "minutes" in ended["why"]


async def test_a_viewer_message_lost_on_the_way_ends_the_view_rather_than_act_out_of_order(desk) -> None:
    relay, phone = Relay(), SoftPasskey()
    viewer, opened = await _open(desk, relay, phone, mode="control", new=True)
    _, sealed = await relay.next("screen_frame")
    header, _ = _frame(viewer, sealed)
    escape = {"type": "key_press", "key": "Escape"}
    desk.take(viewer.seal(opened.session, 2, json.dumps({"ack": header["sequence"], "acts": [escape]}).encode()))  # seq 1 never arrived
    _, ended = await relay.next("screen_ended")
    assert "lost" in ended["why"] and desk.backend.done == [] and desk.live is None


async def test_acts_from_successive_messages_land_in_the_order_they_were_sent(desk, monkeypatch) -> None:
    """A tap then the text typed after it arrive as two sealed messages: the text must wait for the
    click, as on the viewer's screen — two worker threads let the typing land first."""
    import time

    relay, phone = Relay(), SoftPasskey()
    viewer, opened = await _open(desk, relay, phone, mode="control", new=True)
    _, sealed = await relay.next("screen_frame")
    header, _ = _frame(viewer, sealed)
    slow_click = desk.backend.click
    monkeypatch.setattr(desk.backend, "click", lambda *a, **k: (time.sleep(0.1), slow_click(*a, **k)))
    tap = {"type": "click", "x": 10, "y": 10, "frame": header["sequence"]}
    desk.take(viewer.seal(opened.session, 1, json.dumps({"ack": header["sequence"], "acts": [tap]}).encode()))
    desk.take(viewer.seal(opened.session, 2, json.dumps({"acts": [{"type": "type_text", "text": "ls"}]}).encode()))
    for _ in range(100):
        if len(desk.backend.done) == 2:
            break
        await asyncio.sleep(0.01)
    assert [step[0] for step in desk.backend.done] == ["click", "type"]
    await desk.close_all("the test ended")
