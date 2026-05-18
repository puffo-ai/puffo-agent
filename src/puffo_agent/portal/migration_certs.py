"""Cert + envelope builders used by import-side migration.

Mirrors the TypeScript reference in
``puffo-core-han-group/client/web/src/enrollment/certs.ts`` and
``enroll.ts``. Byte layouts must match core-v2 — see the
``puffo/rke-hpke/v1`` / ``puffo/root-key-envelope/v1`` constants
below.
"""

from __future__ import annotations

import time

from ..crypto.canonical import canonicalize_for_signing
from ..crypto.certs import derive_public_key_id
from ..crypto.encoding import base64url_encode
from ..crypto.fingerprint import root_public_key_fingerprint
from ..crypto.primitives import Ed25519KeyPair, hpke_seal
from ..crypto.v2_aad import compute_root_key_envelope_aad

ROOT_KEY_ENVELOPE_HPKE_INFO = b"puffo/rke-hpke/v1"


def _now_ms() -> int:
    return int(time.time() * 1000)


def create_device_cert(
    root_signing_key: Ed25519KeyPair,
    device_signing_pk: bytes,
    device_kem_pk: bytes,
    *,
    issued_at_ms: int | None = None,
) -> dict:
    cert = {
        "type": "device_cert",
        "version": 1,
        "device_id": derive_public_key_id("dev", device_signing_pk),
        "root_public_key": base64url_encode(root_signing_key.public_key_bytes()),
        "keys": {
            "signing": {"algorithm": "ed25519", "public_key": base64url_encode(device_signing_pk)},
            "encryption": {"algorithm": "x25519", "public_key": base64url_encode(device_kem_pk)},
        },
        "issued_at": issued_at_ms if issued_at_ms is not None else _now_ms(),
        "expires_at": None,
        "signature": "",
    }
    sig = root_signing_key.sign(canonicalize_for_signing(cert))
    cert["signature"] = base64url_encode(sig)
    return cert


def create_slug_binding(
    root_signing_key: Ed25519KeyPair, slug: str, *, issued_at_ms: int | None = None,
) -> dict:
    binding = {
        "type": "slug_binding",
        "version": 1,
        "root_public_key": base64url_encode(root_signing_key.public_key_bytes()),
        "slug": slug,
        "issued_at": issued_at_ms if issued_at_ms is not None else _now_ms(),
        "self_signature": "",
    }
    sig = root_signing_key.sign(canonicalize_for_signing(binding))
    binding["self_signature"] = base64url_encode(sig)
    return binding


def create_device_revocation(
    root_signing_key: Ed25519KeyPair,
    device_id: str,
    *,
    effective_from_ms: int | None = None,
    issued_at_ms: int | None = None,
) -> dict:
    now = _now_ms()
    rev = {
        "type": "device_revocation",
        "version": 1,
        "device_id": device_id,
        "root_public_key": base64url_encode(root_signing_key.public_key_bytes()),
        "effective_from": effective_from_ms if effective_from_ms is not None else now,
        "issued_at": issued_at_ms if issued_at_ms is not None else now,
        "signature": "",
    }
    sig = root_signing_key.sign(canonicalize_for_signing(rev))
    rev["signature"] = base64url_encode(sig)
    return rev


def build_root_key_envelope(
    root_secret_key: bytes,
    enrollment_nonce: str,
    new_device_kem_pk: bytes,
) -> dict:
    nonce_bytes = _base64url_decode(enrollment_nonce)
    root_pk = Ed25519KeyPair.from_secret_bytes(root_secret_key).public_key_bytes()
    fingerprint = root_public_key_fingerprint(root_pk)
    aad = compute_root_key_envelope_aad(nonce_bytes, new_device_kem_pk, fingerprint)
    hpke_out = hpke_seal(new_device_kem_pk, ROOT_KEY_ENVELOPE_HPKE_INFO, aad, root_secret_key)
    combined = hpke_out.enc + hpke_out.ciphertext
    return {
        "type": "root_key_envelope",
        "version": 1,
        "enrollment_nonce": enrollment_nonce,
        "recipient_kem_public_key": base64url_encode(new_device_kem_pk),
        "hpke_ciphertext": base64url_encode(combined),
        "root_public_key_fingerprint": base64url_encode(fingerprint),
    }


def _base64url_decode(s: str) -> bytes:
    from ..crypto.encoding import base64url_decode as _d

    return _d(s)
