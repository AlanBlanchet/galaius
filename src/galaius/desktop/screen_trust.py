"""Who may watch this computer, decided ON it: the owner's passkeys it pinned (`ScreenTrustFile`),
the signed offer it checks itself (`ScreenGate.admit`), its own identity key (`ScreenIdentity`), and
the end-to-end keys of one session (`ScreenKeys`).

The server relays every byte and decides none of them (`galaius_core.screen`): a request the server
signs with this machine's key only says the server sent it. A session opens when the WebAuthn
assertion over the offer verifies against a passkey THIS computer trusts — the first one offered
while its owner's own switch is on (`ScreenSwitch`), any later one only once `galaius screen trust`
is run here. A stolen web session, an API key, the agent account or the server itself cannot press
his passkey, so none of them can open a view, let alone type.
"""

import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from galaius_core import (
    SCREEN_CLOCK_SKEW,
    SCREEN_KEY_INFO,
    SCREEN_PASSKEY_ALGORITHMS,
    SCREEN_PROOF_LABEL,
    SCREEN_SESSION_MAX,
    ScreenAssertion,
    ScreenOffer,
    ScreenPasskey,
    ScreenSealed,
    ScreenTrust,
    b64url,
    unb64url,
)
from pydantic import ValidationError

from galaius.paths import UserPaths
from galaius.private_files import PRIVATE_FILES

logger = logging.getLogger("galaius")

#: WebAuthn authenticator-data flags: the person was present (UP) and verified by the device (UV).
USER_PRESENT, USER_VERIFIED = 0x01, 0x04


class ScreenRefused(PermissionError):
    """An offer or a message this computer will not take, in words its owner reads on the web page."""


class ScreenTrustFile:
    """The passkeys this computer trusts, in this user's own private file. Nothing the server sends
    adds a trusted key once there is one: a new passkey waits in `pending` for `trust` typed here."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or UserPaths.config() / "screen-passkeys.json"

    def read(self) -> ScreenTrust:
        """What this computer trusts; nothing when the file is absent, unreadable or not private —
        never a key by accident."""
        try:
            return ScreenTrust.model_validate_json(PRIVATE_FILES.read_text(self.path))
        except FileNotFoundError:
            return ScreenTrust()
        except (OSError, ValueError, ValidationError):
            logger.warning("screen passkeys unreadable; none trusted", exc_info=True)
            return ScreenTrust()

    def write(self, trust: ScreenTrust) -> ScreenTrust:
        PRIVATE_FILES.write_text(self.path, trust.model_dump_json())
        return trust

    def offered(self, passkey: ScreenPasskey) -> ScreenTrust:
        """`passkey` held for its owner's word here (`trust`); the oldest pending drops past the cap."""
        trust = self.read()
        if any(key.credential_id == passkey.credential_id for key in (*trust.trusted, *trust.pending)):
            return trust
        held = passkey.model_copy(update={"added_at": datetime.now(UTC), "counter": 0})
        return self.write(trust.model_copy(update={"pending": (*trust.pending, held)[-ScreenTrust.PENDING_MAX:]}))

    def pin(self, passkey: ScreenPasskey) -> ScreenTrust:
        """`passkey` trusted (the first one, or the owner's `trust` here)."""
        trust = self.read()
        kept = tuple(key for key in trust.trusted if key.credential_id != passkey.credential_id)
        pending = tuple(key for key in trust.pending if key.credential_id != passkey.credential_id)
        pinned = passkey.model_copy(update={"added_at": passkey.added_at or datetime.now(UTC)})
        return self.write(ScreenTrust(trusted=(*kept, pinned), pending=pending))

    def trust(self, prefix: str) -> ScreenPasskey:
        """The pending passkey whose id starts with `prefix`, now trusted. LookupError unless exactly
        one matches: the owner reads the ids in `galaius screen status`."""
        matches = [key for key in self.read().pending if key.credential_id.startswith(prefix)]
        if len(matches) != 1:
            raise LookupError(f"{len(matches)} waiting passkeys start with {prefix!r}")
        self.pin(matches[0])
        return matches[0]

    def forget(self, prefix: str) -> int:
        """Every trusted or pending passkey whose id starts with `prefix` dropped; how many."""
        trust = self.read()
        trusted = tuple(key for key in trust.trusted if not key.credential_id.startswith(prefix))
        pending = tuple(key for key in trust.pending if not key.credential_id.startswith(prefix))
        self.write(ScreenTrust(trusted=trusted, pending=pending))
        return len(trust.trusted) + len(trust.pending) - len(trusted) - len(pending)

    def counted(self, credential_id: str, counter: int) -> None:
        """The authenticator's counter after a verified assertion (a cloned key shows a lower one)."""
        trust = self.read()
        self.write(trust.model_copy(update={"trusted": tuple(
            key.model_copy(update={"counter": counter}) if key.credential_id == credential_id else key for key in trust.trusted)}))


class ScreenIdentity:
    """This computer's own P-256 key, made here once and never sent: the browser pins its public
    half and checks every `ScreenOpened.proof` with it, so a server answering with its own session
    key in the computer's place is caught."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or UserPaths.config() / "screen-identity.pem"
        self._key: ec.EllipticCurvePrivateKey | None = None

    @property
    def key(self) -> ec.EllipticCurvePrivateKey:
        if self._key is None:
            try:
                loaded = serialization.load_pem_private_key(PRIVATE_FILES.read_secret(self.path).encode(), password=None)
            except ValueError:
                loaded = None  # absent (first use) or unreadable: a new identity, which the browser reports as changed
            if not isinstance(loaded, ec.EllipticCurvePrivateKey):
                loaded = ec.generate_private_key(ec.SECP256R1())
                PRIVATE_FILES.write_secret(self.path, loaded.private_bytes(
                    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
            self._key = loaded
        return self._key

    @property
    def public(self) -> str:
        return b64url(self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))

    def prove(self, challenge: bytes, pc_key: bytes, session: UUID) -> str:
        """ECDSA P-256 SHA-256 over the answer, as raw r||s (WebCrypto's form, not DER)."""
        der = self.key.sign(SCREEN_PROOF_LABEL + challenge + pc_key + session.bytes, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


class ScreenKeys:
    """One session's end-to-end keys: ECDH P-256 between the viewer's ephemeral key (in the signed
    offer) and this computer's, HKDF-SHA256 salted with the offer's challenge, one AES-256-GCM key
    per direction. `seal` numbers what leaves; `open` refuses anything not newer than the last."""

    def __init__(self, session: UUID, viewer_key: bytes, challenge: bytes) -> None:
        self.session = session
        own = ec.generate_private_key(ec.SECP256R1())
        try:
            viewer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), viewer_key)
        except ValueError as error:
            raise ScreenRefused("the viewer's session key is not a P-256 point") from error
        shared = own.exchange(ec.ECDH(), viewer)
        self.pc_key = own.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self._send = AESGCM(self._derive(shared, challenge, "pc"))
        self._receive = AESGCM(self._derive(shared, challenge, "viewer"))
        self.sent = 0
        self.received = 0

    @staticmethod
    def _derive(shared: bytes, challenge: bytes, direction: Literal["pc", "viewer"]) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=challenge, info=SCREEN_KEY_INFO[direction]).derive(shared)

    def seal(self, plain: bytes) -> ScreenSealed:
        self.sent += 1
        sealed = ScreenSealed(session=self.session, seq=self.sent, data=b"")
        return sealed.model_copy(update={"data": self._send.encrypt(sealed.iv("pc"), plain, self.session.bytes)})

    def open(self, sealed: ScreenSealed) -> bytes:
        """The viewer's plaintext; ScreenRefused for another session, a replay or a forgery."""
        if sealed.session != self.session or sealed.seq <= self.received:
            raise ScreenRefused("a viewer message was replayed or reordered")
        try:
            plain = self._receive.decrypt(sealed.iv("viewer"), sealed.data, self.session.bytes)
        except InvalidTag as error:
            raise ScreenRefused("a viewer message was not sealed by the viewer") from error
        self.received = sealed.seq
        return plain


class Admitted:
    """An offer this computer verified: what was asked, until when, and its session's keys."""

    def __init__(self, offer: ScreenOffer, challenge: bytes, passkey: ScreenPasskey, paired: bool, ends_at: datetime) -> None:
        self.offer = offer
        self.challenge = challenge
        self.passkey = passkey
        self.paired = paired
        self.ends_at = ends_at
        self.session = uuid4()
        self.keys = ScreenKeys(self.session, unb64url(offer.viewer_key), challenge)


class ScreenGate:
    """The check a session offer passes on this computer before a single pixel leaves.

    `origin` and `rp_id` come from this computer's own server address (its `galaius login`), never
    from the server's say: the page that asked must be on it and the passkey must belong to its
    host (the WebAuthn relying party). Nonces are kept until their offer expires, so one press
    opens one session."""

    #: Nonces remembered at most: past it the soonest-expiring goes.
    NONCES_MAX: ClassVar[int] = 4096

    def __init__(self, machine_id: UUID, server_url: str, trust: ScreenTrustFile | None = None) -> None:
        self.machine_id = machine_id
        parts = urlsplit(server_url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.rp_id = parts.hostname or ""
        self.trust = trust or ScreenTrustFile()
        self._nonces: dict[str, datetime] = {}

    def admit(self, offer_text: str, assertion: ScreenAssertion, switch_on: bool, now: datetime | None = None) -> Admitted:
        """The verified offer, or ScreenRefused naming why: the offer's own terms first, then the
        passkey, then its signature."""
        now = now or datetime.now(UTC)
        try:
            offer = ScreenOffer.model_validate_json(offer_text)
        except ValidationError as error:
            raise ScreenRefused("the session offer is malformed") from error
        if not switch_on:
            raise ScreenRefused("Screen sharing is off on this computer: its owner runs `galaius screen on` there.")
        if offer.machine_id != self.machine_id:
            raise ScreenRefused("the offer names another computer")
        if offer.origin != self.origin:
            raise ScreenRefused(f"the offer comes from {offer.origin}, not from {self.origin}")
        if offer.expires_at.tzinfo is None or offer.expires_at <= now or offer.expires_at > now + SCREEN_SESSION_MAX + SCREEN_CLOCK_SKEW:
            raise ScreenRefused("the offer has expired or asks for longer than 30 minutes")
        self._nonces = {nonce: until for nonce, until in self._nonces.items() if until > now}
        if offer.nonce in self._nonces:
            raise ScreenRefused("this offer was already used")
        challenge = ScreenOffer.challenge(offer_text)
        passkey, paired = self._passkey(assertion)
        counter = self._verify(assertion, passkey, challenge)
        if len(self._nonces) >= self.NONCES_MAX:
            self._nonces.pop(min(self._nonces, key=self._nonces.__getitem__))
        self._nonces[offer.nonce] = offer.expires_at
        if paired:
            self.trust.pin(passkey.model_copy(update={"counter": counter}))
            logger.warning("screen: first passkey pinned on this computer (%s…)", passkey.credential_id[:8])
        elif counter:
            self.trust.counted(passkey.credential_id, counter)
        return Admitted(offer, challenge, passkey, paired, min(offer.expires_at, now + SCREEN_SESSION_MAX))

    def _passkey(self, assertion: ScreenAssertion) -> tuple[ScreenPasskey, bool]:
        """The trusted passkey the assertion names; the offered one when this computer trusts none
        yet (its switch is on: the owner opened it here); else held for `trust` here and refused."""
        trust = self.trust.read()
        found = next((key for key in trust.trusted if key.credential_id == assertion.credential_id), None)
        if found is not None:
            return found, False
        offered = assertion.passkey
        if offered is None or offered.credential_id != assertion.credential_id:
            raise ScreenRefused("this passkey is not one this computer trusts")
        if offered.algorithm not in SCREEN_PASSKEY_ALGORITHMS:
            raise ScreenRefused("this passkey's algorithm is not one a screen session accepts")
        if trust.trusted:
            self.trust.offered(offered)
            raise ScreenRefused("This passkey is new to this computer: run `galaius screen trust` on it to let it watch.")
        return offered, True

    def _verify(self, assertion: ScreenAssertion, passkey: ScreenPasskey, challenge: bytes) -> int:
        """The WebAuthn assertion checked here (W3C WebAuthn §7.2); the authenticator's counter."""
        try:
            client_raw = unb64url(assertion.client_data_json)
            client = json.loads(client_raw)
            data = unb64url(assertion.authenticator_data)
            signature = unb64url(assertion.signature)
        except (ValueError, UnicodeDecodeError) as error:
            raise ScreenRefused("the passkey's answer is malformed") from error
        if not isinstance(client, dict) or client.get("type") != "webauthn.get":
            raise ScreenRefused("the passkey's answer is not a sign-in assertion")
        if client.get("challenge") != b64url(challenge):
            raise ScreenRefused("the passkey signed another offer")
        if client.get("origin") != self.origin or client.get("crossOrigin") is True:
            raise ScreenRefused(f"the passkey was used on {client.get('origin')}, not {self.origin}")
        if len(data) < 37 or data[:32] != hashlib.sha256(self.rp_id.encode()).digest():
            raise ScreenRefused(f"the passkey belongs to another site than {self.rp_id}")
        flags = data[32]
        if not flags & USER_PRESENT or not flags & USER_VERIFIED:
            raise ScreenRefused("the passkey was used without its owner verified on the device")
        counter = int.from_bytes(data[33:37], "big")
        if counter and passkey.counter and counter <= passkey.counter:
            raise ScreenRefused("the passkey's counter went back: it may have been copied")
        signed = data + hashlib.sha256(client_raw).digest()
        try:
            public = serialization.load_der_public_key(unb64url(passkey.public_key))
            if passkey.algorithm == -7 and isinstance(public, ec.EllipticCurvePublicKey):
                public.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
            elif passkey.algorithm == -8 and isinstance(public, ed25519.Ed25519PublicKey):
                public.verify(signature, signed)
            elif passkey.algorithm == -257 and isinstance(public, rsa.RSAPublicKey):
                public.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
            else:
                raise ScreenRefused("the passkey's key does not match its algorithm")
        except (InvalidSignature, ValueError) as error:
            raise ScreenRefused("the passkey's signature does not verify") from error
        return counter


def new_nonce() -> str:
    """A fresh offer nonce, 18 random bytes (what a browser makes)."""
    return b64url(secrets.token_bytes(18))
