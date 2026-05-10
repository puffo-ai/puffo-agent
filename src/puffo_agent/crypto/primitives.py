from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305 as _ChaCha
from pyhpke import AEADId, CipherSuite, KDFId, KEMId, KEMKey

_HPKE_SUITE = CipherSuite.new(
    KEMId.DHKEM_X25519_HKDF_SHA256,
    KDFId.HKDF_SHA256,
    AEADId.CHACHA20_POLY1305,
)

# HPKE info strings. Must match the server's domain constants
# exactly; drift produces AEAD tag-mismatch on the receive side.
MESSAGE_HPKE_INFO = b"puffo/msg-hpke/v1"
ROOT_KEY_ENVELOPE_HPKE_INFO = b"puffo/rke-hpke/v1"


# --------------- Ed25519 ---------------


@dataclass
class Ed25519KeyPair:
    _sk: Ed25519PrivateKey

    @staticmethod
    def generate() -> Ed25519KeyPair:
        return Ed25519KeyPair(Ed25519PrivateKey.generate())

    @staticmethod
    def from_secret_bytes(secret: bytes) -> Ed25519KeyPair:
        return Ed25519KeyPair(Ed25519PrivateKey.from_private_bytes(secret))

    def sign(self, message: bytes) -> bytes:
        return self._sk.sign(message)

    def public_key_bytes(self) -> bytes:
        return self._sk.public_key().public_bytes_raw()

    def secret_bytes(self) -> bytes:
        return self._sk.private_bytes_raw()


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    pk = Ed25519PublicKey.from_public_bytes(public_key)
    try:
        pk.verify(signature, message)
        return True
    except Exception:
        return False


# --------------- HPKE (X25519 + HKDF-SHA256 + ChaCha20Poly1305) ---------------


@dataclass
class HpkeOutput:
    enc: bytes
    ciphertext: bytes


@dataclass
class KemKeyPair:
    _sk: X25519PrivateKey

    @staticmethod
    def generate() -> KemKeyPair:
        return KemKeyPair(X25519PrivateKey.generate())

    @staticmethod
    def from_secret_bytes(secret: bytes) -> KemKeyPair:
        return KemKeyPair(X25519PrivateKey.from_private_bytes(secret))

    def public_key_bytes(self) -> bytes:
        return self._sk.public_key().public_bytes_raw()

    def secret_bytes(self) -> bytes:
        return self._sk.private_bytes_raw()


def hpke_seal(
    recipient_pk: bytes, info: bytes, aad: bytes, plaintext: bytes,
) -> HpkeOutput:
    pk = X25519PublicKey.from_public_bytes(recipient_pk)
    kem_pk = KEMKey.from_pyca_cryptography_key(pk)
    enc, ctx = _HPKE_SUITE.create_sender_context(kem_pk, info=info)
    ct = ctx.seal(plaintext, aad)
    return HpkeOutput(enc=enc, ciphertext=ct)


def hpke_open(
    recipient_kp: KemKeyPair, enc: bytes, info: bytes, aad: bytes, ciphertext: bytes,
) -> bytes:
    kem_sk = KEMKey.from_pyca_cryptography_key(recipient_kp._sk)
    ctx = _HPKE_SUITE.create_recipient_context(enc, kem_sk, info=info)
    return ctx.open(ciphertext, aad)


# --------------- ChaCha20Poly1305 AEAD ---------------


def aead_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    return _ChaCha(key).encrypt(nonce, plaintext, aad)


def aead_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    return _ChaCha(key).decrypt(nonce, ciphertext, aad)


def generate_content_key() -> bytes:
    return os.urandom(32)


def generate_aead_nonce() -> bytes:
    return os.urandom(12)


# --------------- SHA-256 ---------------


def sha256(data: bytes) -> bytes:
    h = hashes.Hash(hashes.SHA256())
    h.update(data)
    return h.finalize()
