"""Credential design v2 wire format: key derivation, key cert, and wraps.

Everything here is part of the wire contract with the server and the web
client. Rust and TypeScript implement the same bytes, and the only symptom
of drift is a wrap that will not open or a cert that will not verify — with
no hint of which byte moved. ``credential_vectors.json`` pins every layout
below; change one and a vector fails.

Three pieces, each fixed by the design doc (v2 §2, §4.1, §5.1) plus the
amendments that pinned what the doc left open:

- **Derivation.** Each principal derives one X25519 key from its root, not
  from a device, so a wrap survives the device being re-enrolled.
- **Key cert.** The derived public key cannot be computed from the root's
  public key, so the root signs a cert publishing it.
- **Wrap.** A credential value (or the S2 share) HPKE-sealed to one
  recipient, with the AAD binding it to one credential, version, recipient,
  type and key version, so a blob cannot be moved between rows or recipients.
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

# v2 §2 fixes this string, version included; a v2 key changes the suffix.
_KEM_HKDF_INFO = "puffo-credential-kem-v{version}"

CREDENTIAL_HPKE_INFO = b"puffo/credential-hpke/v1"
CREDENTIAL_WRAP_AAD_LABEL = b"puffo/credential-wrap/v1"

CERT_TYPE = "credential_key_cert"

# X25519 encapsulated key length; the wrap blob is ``enc || ciphertext``.
_ENC_LEN = 32


class CredentialKeyError(Exception):
    """A cert or wrap that must not be trusted."""


def derive_credential_kem_keypair(root_secret: bytes, key_version: int = 1) -> KemKeyPair:
    """``HKDF-SHA256(ikm=root_secret, salt=none, info=..., L=32)`` as X25519.

    No salt, per RFC 5869 §2.2 (a zero-filled HashLen salt). Clamping is left
    to X25519 itself, which is what every implementation does on use.
    """
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
    """The root-signed cert that publishes a derived key.

    Same shape and signing rule as the subkey cert: RFC 8785 over the object
    without ``signature``. ``slug`` is inside the signature so a cert cannot be
    re-registered under another identity with the same root.
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

    ``root_public_key`` is the caller's trust anchor, and where it comes from
    is the whole point. The server runs this to stop one principal registering
    a key for another. A party about to wrap a secret runs it against an anchor
    the server did not hand it (amendment 6), because a live server can swap
    the cert and the root key together and the check would then pass.
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

    Server-pinned (puffo-server ``types.rs::credential_id``). A reader derives
    it too, so that a response naming some other credential is caught before
    its value is handed back as the one that was asked for.
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

    The UUID goes in as its 16 raw bytes rather than text, so upper- and
    lower-case renderings of the same id cannot produce two different AADs.
    Integers and length prefixes follow ``v2_aad``.
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
    except Exception as exc:  # noqa: BLE001 - any failure means "not for us, or moved"
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
