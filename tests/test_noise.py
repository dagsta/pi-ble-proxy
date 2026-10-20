"""Interoperabilität mit der unabhängigen Referenz ``noiseprotocol`` in beide Richtungen."""

import os

import pytest

from pi_ble_proxy.noise import PROLOGUE, Handshake, HandshakeError

noise = pytest.importorskip("noise.connection")
NAME = b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"


def _ref(psk: bytes, initiator: bool):
    c = noise.NoiseConnection.from_name(NAME)
    c.set_psks(psk)
    c.set_prologue(PROLOGUE)
    (c.set_as_initiator if initiator else c.set_as_responder)()
    c.start_handshake()
    return c


def test_ours_responder_vs_reference_initiator():
    psk = os.urandom(32)
    ref, ours = _ref(psk, True), Handshake(psk, initiator=False)
    ours.read_message1(ref.write_message())
    ref.read_message(ours.write_message2())
    send, recv = ours.split()
    assert ref.handshake_finished
    assert ref.decrypt(send.encrypt(b"", b"hallo")) == b"hallo"
    assert recv.decrypt(b"", ref.encrypt(b"zurueck")) == b"zurueck"
    # mehrere Nachrichten: Nonce-Zähler müssen synchron bleiben
    for i in range(5):
        assert ref.decrypt(send.encrypt(b"", bytes([i]) * 7)) == bytes([i]) * 7


def test_ours_initiator_vs_reference_responder():
    psk = os.urandom(32)
    ref, ours = _ref(psk, False), Handshake(psk, initiator=True)
    ref.read_message(ours.write_message1())
    ours.read_message2(ref.write_message())
    send, recv = ours.split()
    assert ref.decrypt(send.encrypt(b"", b"ping")) == b"ping"
    assert recv.decrypt(b"", ref.encrypt(b"pong")) == b"pong"


def test_wrong_psk_is_rejected():
    ref = _ref(os.urandom(32), True)
    ours = Handshake(os.urandom(32), initiator=False)
    with pytest.raises(HandshakeError, match="MAC"):
        ours.read_message1(ref.write_message())


@pytest.mark.parametrize("size", [0, 10, 47, 49, 200])
def test_bad_length_is_rejected(size):
    with pytest.raises(HandshakeError):
        Handshake(os.urandom(32), initiator=False).read_message1(os.urandom(size))


def test_psk_length_checked():
    with pytest.raises(ValueError):
        Handshake(b"short", initiator=True)
