from __future__ import annotations

import time
from dataclasses import dataclass

from .encoding import base64url_decode, base64url_encode, generate_nonce
from .primitives import Ed25519KeyPair, ed25519_verify

AUTH_VERSION = "v1"


@dataclass
class AuthHeaders:
    version: str
    slug: str
    signer_id: str
    timestamp: str
    nonce: str
    signature: str

    def to_dict(self) -> dict[str, str]:
        return {
            "x-puffo-version": self.version,
            "x-puffo-slug": self.slug,
            "x-puffo-signer-id": self.signer_id,
            "x-puffo-timestamp": self.timestamp,
            "x-puffo-nonce": self.nonce,
            "x-puffo-signature": self.signature,
            "content-type": "application/json",
        }


def _build_signing_message(
    method: str, path: str, timestamp: str, nonce: str, body: bytes,
) -> bytes:
    prefix = f"{method}\n{path}\n{timestamp}\n{nonce}\n".encode()
    return prefix + body


def _now_ms() -> int:
    return int(time.time() * 1000)


def sign_request(
    signing_key: Ed25519KeyPair,
    slug: str,
    signer_id: str,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    timestamp_ms: int | None = None,
    nonce: str | None = None,
) -> AuthHeaders:
    if timestamp_ms is None:
        timestamp_ms = _now_ms()
    if nonce is None:
        nonce = generate_nonce()

    timestamp = str(timestamp_ms)
    message = _build_signing_message(method, path, timestamp, nonce, body)
    sig = signing_key.sign(message)

    return AuthHeaders(
        version=AUTH_VERSION,
        slug=slug,
        signer_id=signer_id,
        timestamp=timestamp,
        nonce=nonce,
        signature=base64url_encode(sig),
    )


class VerifyError(Exception):
    """Raised when an x-puffo-* signed request fails verification."""


def verify_request(
    *,
    public_key: bytes,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
    signature_b64: str,
) -> None:
    """Verify an ed25519-signed request against ``public_key``.

    Canonical signing string matches ``sign_request``:
    ``METHOD\\nPATH\\nTIMESTAMP\\nNONCE\\n<body>``. Method is
    upper-cased so a browser-lowercased verb verifies against a
    signer that used the canonical upper-case form.
    """
    try:
        sig = base64url_decode(signature_b64)
    except Exception as exc:
        raise VerifyError(f"signature base64 decode failed: {exc}") from exc
    if len(sig) != 64:
        raise VerifyError(f"signature must be 64 bytes, got {len(sig)}")
    message = _build_signing_message(method.upper(), path, timestamp, nonce, body)
    if not ed25519_verify(public_key, message, sig):
        raise VerifyError("signature verification failed")


def is_timestamp_fresh(
    timestamp_ms_str: str, *, max_skew_ms: int = 5 * 60 * 1000,
) -> bool:
    """True iff ``timestamp_ms_str`` is within ``max_skew_ms`` of now
    in either direction. Default ±5min matches puffo-server: tight
    enough to bound replay, loose enough to survive client clock
    drift.
    """
    try:
        ts = int(timestamp_ms_str)
    except (TypeError, ValueError):
        return False
    return abs(_now_ms() - ts) <= max_skew_ms
