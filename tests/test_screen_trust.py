"""Who may watch this computer, decided on it (`galaius.desktop.screen_trust`).

Threat review 2026-10-23 (binding): « Each session = an offer signed by that passkey … The runner
checks the signature itself and ignores the server's say-so. » These pin it with a software
passkey that builds the exact bytes a browser's authenticator signs (WebAuthn authenticator data +
client data JSON, ECDSA P-256): an offer opens only under a passkey this computer trusts, every
way the server could bend one is refused, and both ends derive the same session keys — so what the
server relays it can neither read, forge nor replay.
"""

import hashlib
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from galaius_core import (
    SCREEN_PROOF_LABEL,
    ScreenAssertion,
    ScreenOffer,
    ScreenSealed,
    b64url,
    unb64url,
)

from galaius.desktop.screen_trust import (
    ScreenEnd,
    ScreenGate,
    ScreenIdentity,
    ScreenRefused,
    ScreenTrustFile,
    new_nonce,
)
from tests.support.screen import MACHINE, ORIGIN, SoftPasskey, Viewer


@pytest.fixture
def gate(tmp_path) -> ScreenGate:
    return ScreenGate(MACHINE, f"{ORIGIN}/plateform", ScreenTrustFile(tmp_path / "screen-passkeys.json"))


def test_a_first_passkey_pairs_and_both_ends_hold_the_same_keys_that_refuse_a_replay(gate, tmp_path) -> None:
    phone, viewer = SoftPasskey(), Viewer()
    text = viewer.offer()
    admitted = gate.admit(text, phone.assert_(text, new=True), switch_on=True)
    assert admitted.paired and gate.trust.read().trusted[0].credential_id == phone.credential_id

    from_pc, to_pc = viewer.keys(b64url(admitted.keys.pc_key), ScreenOffer.challenge(text))
    frame = admitted.keys.seal(b"\x01picture")
    assert from_pc.decrypt(frame.iv("pc"), frame.data, admitted.session.bytes) == b"\x01picture"
    act = ScreenSealed(session=admitted.session, seq=1, data=b"")
    act = act.model_copy(update={"data": to_pc.encrypt(act.iv("viewer"), b'{"ack":1}', admitted.session.bytes)})
    assert admitted.keys.open(act) == b'{"ack":1}'
    with pytest.raises(ScreenRefused, match="replayed"):
        admitted.keys.open(act)
    forged = ScreenSealed(session=admitted.session, seq=2, data=act.data)
    with pytest.raises(ScreenRefused, match="not sealed"):
        admitted.keys.open(forged)
    # Threat review r2: a relay that drops ONE act (an Escape before typed text) must not go unseen.
    skipped = ScreenSealed(session=admitted.session, seq=3, data=b"")
    skipped = skipped.model_copy(update={"data": to_pc.encrypt(skipped.iv("viewer"), b'{"ack":2}', admitted.session.bytes)})
    with pytest.raises(ScreenEnd, match="lost on the way") as ended:
        admitted.keys.open(skipped)
    assert ended.value.refusal == "message_lost"

    # The browser pins `identity` and checks the proof as WebCrypto does (raw r||s).
    identity = ScreenIdentity(tmp_path / "screen-identity.pem")
    proof = unb64url(identity.prove(admitted.challenge, admitted.keys.pc_key, admitted.session))
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64url(identity.public))
    public.verify(encode_dss_signature(int.from_bytes(proof[:32], "big"), int.from_bytes(proof[32:], "big")),
                  SCREEN_PROOF_LABEL + admitted.challenge + admitted.keys.pc_key + admitted.session.bytes, ec.ECDSA(hashes.SHA256()))
    assert ScreenIdentity(tmp_path / "screen-identity.pem").public == identity.public  # kept, never re-made
    # What `galaius screen on` prints and the browser shows at its first pin, so he compares them once.
    assert identity.fingerprint == " ".join(hashlib.sha256(unb64url(identity.public)).hexdigest()[i:i + 4] for i in range(0, 32, 4))


def _bent(case: str, phone: SoftPasskey, viewer: Viewer) -> tuple[str, ScreenAssertion, bool]:
    """One way an offer reaching this computer differs from what its owner's passkey signed."""
    if case == "switch off":
        text = viewer.offer()
        return text, phone.assert_(text), False
    if case == "another computer":
        text = viewer.offer(machine=uuid4())
        return text, phone.assert_(text), True
    if case == "another page":
        text = viewer.offer(origin="https://evil.example")
        return text, phone.assert_(text, origin="https://evil.example"), True
    if case == "longer than 30 minutes":
        text = viewer.offer(minutes=90)
        return text, phone.assert_(text), True
    if case == "expired":
        text = viewer.offer(minutes=-1)
        return text, phone.assert_(text), True
    if case == "offer changed after signing":
        signed = viewer.offer()
        return signed.replace('"view"', '"control"'), phone.assert_(signed), True
    if case == "not verified on the device":
        text = viewer.offer()
        return text, phone.assert_(text, flags=0x01), True
    if case == "another site's passkey":
        other = SoftPasskey(rp_id="evil.example")
        other.key, other.credential_id = phone.key, phone.credential_id
        text = viewer.offer()
        return text, other.assert_(text), True
    if case == "unknown passkey":
        stranger = SoftPasskey()
        text = viewer.offer()
        return text, stranger.assert_(text, new=True), True
    raise AssertionError(case)


@pytest.mark.parametrize("case", ["switch off", "another computer", "another page", "longer than 30 minutes", "expired",
                                  "offer changed after signing", "not verified on the device", "another site's passkey", "unknown passkey"])
def test_an_offer_the_owner_did_not_sign_for_this_computer_now_is_refused(gate, case) -> None:
    phone = SoftPasskey()
    first = Viewer().offer()
    gate.admit(first, phone.assert_(first, new=True), switch_on=True)  # paired
    text, assertion, switch_on = _bent(case, phone, Viewer())
    with pytest.raises(ScreenRefused):
        gate.admit(text, assertion, switch_on=switch_on)
    assert [key.credential_id for key in gate.trust.read().trusted] == [phone.credential_id]


def test_one_press_opens_one_session_and_a_new_passkey_waits_for_the_owner_here(gate) -> None:
    phone, laptop, viewer = SoftPasskey(), SoftPasskey(), Viewer()
    nonce = new_nonce()
    text = viewer.offer(nonce=nonce)
    gate.admit(text, phone.assert_(text, new=True), switch_on=True)
    with pytest.raises(ScreenRefused, match="already used"):
        gate.admit(text, phone.assert_(text), switch_on=True)

    text = viewer.offer()
    with pytest.raises(ScreenRefused, match="galaius screen trust"):
        gate.admit(text, laptop.assert_(text, new=True), switch_on=True)
    assert [key.credential_id for key in gate.trust.read().pending] == [laptop.credential_id]
    gate.trust.trust(laptop.credential_id[:6])
    text = viewer.offer()
    assert not gate.admit(text, laptop.assert_(text), switch_on=True).paired
