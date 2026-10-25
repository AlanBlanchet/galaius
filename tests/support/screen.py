"""What a screen-session test stands in for: the owner's passkey in software (`SoftPasskey`, the
exact bytes a browser's authenticator signs), the browser's half of the session crypto (`Viewer`),
and the desktop itself (`FakeGrabber`, `FakeBackend`, `FakeMarker`) — so the computer's side is
exercised against what a real browser sends, with no display."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from galaius_core import (
    SCREEN_KEY_INFO,
    ScreenAssertion,
    ScreenOffer,
    ScreenPasskey,
    ScreenSealed,
    b64url,
    unb64url,
)
from PIL import Image

from galaius.desktop.screen_trust import new_nonce

ORIGIN = "https://galaius.ai"
MACHINE = UUID("68b0d749-0000-4000-8000-000000000001")


class SoftPasskey:
    """A platform authenticator in software: one P-256 credential for `rp_id`."""

    def __init__(self, rp_id: str = "galaius.ai") -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = b64url(uuid4().bytes)
        self.rp_id = rp_id

    @property
    def passkey(self) -> ScreenPasskey:
        spki = self.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return ScreenPasskey(credential_id=self.credential_id, public_key=b64url(spki), algorithm=-7, label="phone")

    def assert_(self, offer_text: str, origin: str = ORIGIN, flags: int = 0x05, counter: int = 0, new: bool = False) -> ScreenAssertion:
        client = json.dumps({"type": "webauthn.get", "challenge": b64url(ScreenOffer.challenge(offer_text)), "origin": origin, "crossOrigin": False}).encode()
        data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + counter.to_bytes(4, "big")
        signature = self.key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return ScreenAssertion(credential_id=self.credential_id, authenticator_data=b64url(data), client_data_json=b64url(client),
                               signature=b64url(signature), passkey=self.passkey if new else None)


class Viewer:
    """The browser's half: an ephemeral ECDH key in the offer, then the same HKDF / AES-GCM."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())

    def offer(self, *, machine: UUID = MACHINE, origin: str = ORIGIN, minutes: float = 10, nonce: str | None = None, mode: str = "view") -> str:
        raw = self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        # The browser's own JSON.stringify: key order and spacing are whatever it emits; the PC reads THIS text.
        return json.dumps({"version": 1, "machine_id": str(machine), "nonce": nonce or new_nonce(), "mode": mode,
                           "expires_at": (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(), "viewer_key": b64url(raw), "origin": origin})

    def keys(self, pc_key: str, challenge: bytes) -> tuple[AESGCM, AESGCM]:
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64url(pc_key)))
        derive = lambda info: AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=challenge, info=info).derive(shared))
        return derive(SCREEN_KEY_INFO["pc"]), derive(SCREEN_KEY_INFO["viewer"])

    def open(self, sealed: ScreenSealed) -> bytes:
        """A sealed PC message opened as the browser does (keys from `start`)."""
        return self.from_pc.decrypt(sealed.iv("pc"), sealed.data, sealed.session.bytes)

    def seal(self, session: UUID, seq: int, plain: bytes) -> ScreenSealed:
        sealed = ScreenSealed(session=session, seq=seq, data=b"")
        return sealed.model_copy(update={"data": self.to_pc.encrypt(sealed.iv("viewer"), plain, session.bytes)})

    def start(self, pc_key: str, challenge: bytes) -> None:
        self.from_pc, self.to_pc = self.keys(pc_key, challenge)


class FakeGrabber:
    """A screen of one colour, 1920x1080 at +0+0, that changes when `fill` is set."""

    def __init__(self, fill: tuple[int, int, int] = (10, 20, 30), rect: dict[str, int] | None = None) -> None:
        self.fill = fill
        self.rect_value = rect or {"left": 0, "top": 0, "width": 1920, "height": 1080}
        self.grabs = 0
        self.closed = False

    def monitors(self):
        return []

    def rect(self, monitor):
        return dict(self.rect_value)

    def grab(self, rect):
        self.grabs += 1
        return Image.new("RGB", (rect["width"], rect["height"]), self.fill)

    def close(self):
        self.closed = True


class FakeBackend:
    """Every act a viewer can send, recorded in order."""

    def __init__(self, pointer: tuple[int, int] | None = None) -> None:
        self.done: list[tuple] = []
        self.spot = pointer

    def pointer(self):
        return self.spot

    def move(self, x, y):
        self.done.append(("move", x, y))

    def click(self, x, y, button="left", count=1):
        self.done.append(("click", x, y, button, count))

    def scroll(self, clicks, horizontal=False):
        self.done.append(("scroll", clicks, horizontal))

    def key(self, name):
        self.done.append(("key", name))

    def type_text(self, text):
        self.done.append(("type", text))


class FakeMarker:
    """The badge, as the session sees it: `showable` False is a desktop that cannot show one,
    `pressed` True is its Stop."""

    def __init__(self, showable: bool = True, pressed: bool = False) -> None:
        self.showable, self.pressed = showable, pressed
        self.shown = self.stopped = 0

    def show(self, viewer=""):
        self.shown += 1
        return self.showable

    def ended_itself(self):
        return self.pressed

    def stop(self):
        self.stopped += 1
