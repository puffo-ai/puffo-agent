"""Credential design v2 wire format (v2 §2, §4.1, §5.1).

Shared with Rust and TypeScript; ``credential_vectors.json`` pins the bytes.
"""

from __future__ import annotations

import uuid

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .canonical import canonicalize_for_signing
from .encoding import base64url_decode, base64url_encode
from .primitives import (
    Ed25519KeyPair,
    KemKeyPair,
    ed25519_verify,
    hpke_open,
    hpke_seal,
)

# Wire-pinned, version included (v2 §2).
_KEM_HKDF_INFO = "puffo-credential-kem-v{version}"

CREDENTIAL_HPKE_INFO = b"puffo/credential-hpke/v1"
CREDENTIAL_WRAP_AAD_LABEL = b"puffo/credential-wrap/v1"

CERT_TYPE = "credential_key_cert"

# blob = enc(32) || ciphertext
_ENC_LEN = 32


class CredentialKeyError(Exception):
    """A cert or wrap that must not be trusted."""


def derive_credential_kem_keypair(root_secret: bytes, key_version: int = 1) -> KemKeyPair:
    """HKDF-SHA256 -> X25519. ``salt=None`` is 32 zero bytes (RFC 5869 §2.2)."""
    if key_version < 1:
        raise ValueError("key_version starts at 1")
    okm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_KEM_HKDF_INFO.format(version=key_version).encode(),
    ).derive(root_secret)
    return KemKeyPair.from_secret_bytes(okm)


def create_credential_key_cert(
    root: Ed25519KeyPair,
    slug: str,
    kem_public_key: bytes,
    key_version: int,
    issued_at: int,
) -> dict:
    """Root-signed cert publishing a derived key.

    RFC 8785 over the object without ``signature``; ``slug`` is signed.
    """
    cert = {
        "type": CERT_TYPE,
        "version": 1,
        "slug": slug,
        "kem_public_key": base64url_encode(kem_public_key),
        "key_version": key_version,
        "issued_at": issued_at,
        "signature": "",
    }
    cert["signature"] = base64url_encode(root.sign(canonicalize_for_signing(cert)))
    return cert


def verify_credential_key_cert(
    cert: dict, *, root_public_key: bytes, slug: str, key_version: int
) -> bytes:
    """Return the published KEM key, or raise.

    A wrapping party's ``root_public_key`` must not come from the server: it
    can swap cert and root together (amendment 6).
    """
    if cert.get("type") != CERT_TYPE or cert.get("version") != 1:
        raise CredentialKeyError("not a v1 credential key cert")
    if cert.get("slug") != slug:
        raise CredentialKeyError("cert is for a different slug")
    if cert.get("key_version") != key_version:
        raise CredentialKeyError("cert is for a different key_version")
    try:
        signature = base64url_decode(cert["signature"])
        kem_public_key = base64url_decode(cert["kem_public_key"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CredentialKeyError(f"malformed cert: {type(exc).__name__}") from exc
    if len(kem_public_key) != 32:
        raise CredentialKeyError("kem_public_key is not 32 bytes")
    if not ed25519_verify(root_public_key, canonicalize_for_signing(cert), signature):
        raise CredentialKeyError("cert signature does not verify")
    return kem_public_key


_CREDENTIAL_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "puffo:credentials")


def credential_id(owner_slug: str, credential_type: str, index: int) -> str:
    """The id the server assigns to ``(owner, type, index)``.

    Pinned by puffo-server ``types.rs``; a reader derives it to catch a
    response naming another credential.
    """
    return str(uuid.uuid5(_CREDENTIAL_ID_NAMESPACE, f"{owner_slug}/{credential_type}/{index}"))


def compute_credential_wrap_aad(
    *,
    credential_id: str,
    version: int,
    recipient_slug: str,
    credential_type: str,
    key_version: int,
) -> bytes:
    """``label || uuid(16) || version(i64 BE) || lp(recipient_slug) || lp(type)
    || key_version(i64 BE)``, where ``lp`` is a u16 BE length then UTF-8.

    Integers and prefixes follow ``v2_aad``.
    """
    return (
        CREDENTIAL_WRAP_AAD_LABEL
        + uuid.UUID(credential_id).bytes
        + _i64_be(version)
        + _len_prefixed_utf8(recipient_slug)
        + _len_prefixed_utf8(credential_type)
        + _i64_be(key_version)
    )


def seal_credential(recipient_kem_public_key: bytes, aad: bytes, plaintext: bytes) -> bytes:
    """One recipient's wrap: ``enc(32) || ciphertext``."""
    out = hpke_seal(recipient_kem_public_key, CREDENTIAL_HPKE_INFO, aad, plaintext)
    return out.enc + out.ciphertext


def open_credential(recipient: KemKeyPair, aad: bytes, blob: bytes) -> bytes:
    if len(blob) <= _ENC_LEN:
        raise CredentialKeyError("wrap is too short to hold a key and a ciphertext")
    try:
        return hpke_open(recipient, blob[:_ENC_LEN], CREDENTIAL_HPKE_INFO, aad, blob[_ENC_LEN:])
    except Exception as exc:  # noqa: BLE001 - any failure means "not ours"
        raise CredentialKeyError("wrap does not open under this key and AAD") from exc


def _i64_be(value: int) -> bytes:
    if value < 0 or value > (1 << 63) - 1:
        raise ValueError("value out of i64 range")
    return value.to_bytes(8, "big", signed=True)


def _len_prefixed_utf8(value: str) -> bytes:
    raw = value.encode("utf-8")
    if not raw or len(raw) > 0xFFFF:
        raise ValueError("length-prefixed field must be 1..65535 bytes")
    return len(raw).to_bytes(2, "big") + raw
