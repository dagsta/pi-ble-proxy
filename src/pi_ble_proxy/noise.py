"""Noise_NNpsk0_25519_ChaChaPoly_SHA256, wie ihn die ESPHome Native API verwendet.

Nur X25519 und ChaCha20-Poly1305 kommen aus ``cryptography``; Schlüsselableitung
(HKDF über HMAC), Hashing und das Handshake-Protokoll sind hier geschrieben.
Referenz: https://noiseprotocol.org/noise.html (Revision 34).
"""

from __future__ import annotations

import hashlib
import hmac

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

PROTOCOL_NAME = b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"
PROLOGUE = b"NoiseAPIInit\x00\x00"
DHLEN = 32
TAG_LEN = 16


class HandshakeError(Exception):
    """Handshake fehlgeschlagen (falscher Schlüssel, kaputte Nachricht)."""


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _hkdf(chaining_key: bytes, ikm: bytes, outputs: int) -> tuple[bytes, ...]:
    temp = _hmac(chaining_key, ikm)
    out1 = _hmac(temp, b"\x01")
    out2 = _hmac(temp, out1 + b"\x02")
    if outputs == 2:
        return out1, out2
    return out1, out2, _hmac(temp, out2 + b"\x03")


class CipherState:
    def __init__(self, key: bytes | None = None) -> None:
        self._aead = ChaCha20Poly1305(key) if key else None
        self._n = 0

    @property
    def has_key(self) -> bool:
        return self._aead is not None

    def _nonce(self) -> bytes:
        nonce = b"\x00" * 4 + self._n.to_bytes(8, "little")
        self._n += 1
        return nonce

    def encrypt(self, ad: bytes, plaintext: bytes) -> bytes:
        if self._aead is None:
            return plaintext
        return self._aead.encrypt(self._nonce(), plaintext, ad)

    def decrypt(self, ad: bytes, ciphertext: bytes) -> bytes:
        if self._aead is None:
            return ciphertext
        return self._aead.decrypt(self._nonce(), ciphertext, ad)


class _SymmetricState:
    def __init__(self, protocol_name: bytes, prologue: bytes) -> None:
        if len(protocol_name) <= 32:
            self.h = protocol_name.ljust(32, b"\x00")
        else:
            self.h = hashlib.sha256(protocol_name).digest()
        self.ck = self.h
        self.cipher = CipherState()
        self.mix_hash(prologue)

    def mix_hash(self, data: bytes) -> None:
        self.h = hashlib.sha256(self.h + data).digest()

    def mix_key(self, ikm: bytes) -> None:
        self.ck, temp_k = _hkdf(self.ck, ikm, 2)
        self.cipher = CipherState(temp_k)

    def mix_key_and_hash(self, ikm: bytes) -> None:
        self.ck, temp_h, temp_k = _hkdf(self.ck, ikm, 3)
        self.mix_hash(temp_h)
        self.cipher = CipherState(temp_k)

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ciphertext = self.cipher.encrypt(self.h, plaintext)
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        plaintext = self.cipher.decrypt(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return plaintext

    def split(self) -> tuple[CipherState, CipherState]:
        k1, k2 = _hkdf(self.ck, b"", 2)
        return CipherState(k1), CipherState(k2)


class Handshake:
    """Zwei Nachrichten: ``-> psk, e`` und ``<- e, ee`` (jeweils mit leerer Payload)."""

    def __init__(self, psk: bytes, *, initiator: bool, prologue: bytes = PROLOGUE) -> None:
        if len(psk) != 32:
            raise ValueError("Der PSK muss 32 Bytes lang sein")
        self._psk = psk
        self._initiator = initiator
        self._ss = _SymmetricState(PROTOCOL_NAME, prologue)
        self._e = X25519PrivateKey.generate()

    def _e_public(self) -> bytes:
        return self._e.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def _dh(self, remote_public: bytes) -> bytes:
        try:
            return self._e.exchange(X25519PublicKey.from_public_bytes(remote_public))
        except ValueError as exc:  # Punkt mit kleiner Ordnung o. ä.
            raise HandshakeError("Ungültiger öffentlicher Schlüssel") from exc

    def _write_e(self) -> bytes:
        pub = self._e_public()
        self._ss.mix_hash(pub)
        self._ss.mix_key(pub)  # PSK-Modus: der "e"-Token mischt zusätzlich den Schlüssel
        return pub

    def _read_e(self, msg: bytes) -> bytes:
        msg = bytes(msg)
        if len(msg) != DHLEN + TAG_LEN:
            raise HandshakeError(f"Handshake-Nachricht hat falsche Länge ({len(msg)})")
        re = msg[:DHLEN]
        self._ss.mix_hash(re)
        self._ss.mix_key(re)
        return re

    def _decrypt_payload(self, ciphertext: bytes) -> None:
        try:
            self._ss.decrypt_and_hash(ciphertext)
        except InvalidTag as exc:
            raise HandshakeError("Handshake MAC failure") from exc

    # Initiator -------------------------------------------------------------
    def write_message1(self) -> bytes:
        self._ss.mix_key_and_hash(self._psk)
        return self._write_e() + self._ss.encrypt_and_hash(b"")

    def read_message2(self, msg: bytes) -> None:
        re = self._read_e(msg)
        self._ss.mix_key(self._dh(re))
        self._decrypt_payload(bytes(msg)[DHLEN:])

    # Responder -------------------------------------------------------------
    def read_message1(self, msg: bytes) -> None:
        self._ss.mix_key_and_hash(self._psk)
        self._re = self._read_e(msg)
        self._decrypt_payload(bytes(msg)[DHLEN:])

    def write_message2(self) -> bytes:
        out = self._write_e()
        self._ss.mix_key(self._dh(self._re))
        return out + self._ss.encrypt_and_hash(b"")

    def split(self) -> tuple[CipherState, CipherState]:
        """Liefert ``(senden, empfangen)`` aus Sicht dieser Rolle."""
        c1, c2 = self._ss.split()
        return (c1, c2) if self._initiator else (c2, c1)
