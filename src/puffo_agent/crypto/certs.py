from __future__ import annotations

import time
from typing import Literal

from .canonical import canonicalize_for_signing
from .encoding import base64url_encode
from .primitives import Ed25519KeyPair, sha256

SUBKEY_TTL_HOURS = 47  # server caps at strict 48h; 47h avoids NTP drift rejections
ROTATION_MARGIN_MS = 5 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def derive_public_key_id(prefix: Literal["dev", "sk"], public_key: bytes) -> str:
    """``<prefix>_<base64url(sha256(public_key))>``.

    The id MUST be the SHA-256 of the bound public key — the server
    enforces ``DeviceId::derive(signing_pk) == cert.device_id`` and
    rejects anything else (e.g. random UUIDs).
    """
    return f"{prefix}_{base64url_encode(sha256(public_key))}"


def create_subkey_cert(
    device_signing_key: Ed25519KeyPair,
    device_id: str,
    subkey_pk_bytes: bytes,
    ttl_hours: int = SUBKEY_TTL_HOURS,
    *,
    issued_at: int | None = None,
) -> dict:
    if issued_at is None:
        issued_at = _now_ms()
    expires_at = issued_at + ttl_hours * 3_600_000
    subkey_id = derive_public_key_id("sk", subkey_pk_bytes)

    cert = {
        "type": "subkey_cert",
        "version": 1,
        "subkey_id": subkey_id,
        "device_id": device_id,
        "subkey_public_key": base64url_encode(subkey_pk_bytes),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "signature": "",
    }

    canonical = canonicalize_for_signing(cert)
    sig = device_signing_key.sign(canonical)
    cert["signature"] = base64url_encode(sig)

    return cert


def is_subkey_expired(cert: dict, now_ms: int | None = None) -> bool:
    if now_ms is None:
        now_ms = _now_ms()
    expires_at = cert.get("expires_at")
    if expires_at is None:
        return False  # v2 allows non-expiring subkeys
    return now_ms >= expires_at


def needs_rotation(expires_at: int | None) -> bool:
    if expires_at is None:
        return False  # non-expiring, never rotate
    return expires_at <= _now_ms() + ROTATION_MARGIN_MS
