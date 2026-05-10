"""Mirrors core-v2 ``PublicKeyFingerprint::derive_ed25519_public_key_bytes``
(``service/ids/fingerprint.rs``). Used as the context binder in the
root-key envelope."""

from __future__ import annotations

from .primitives import sha256

ROOT_PUBLIC_KEY_FINGERPRINT_DOMAIN = b"puffo/root-public-key-fingerprint/v1"
ED25519_ALGORITHM_LABEL = b"ed25519"


def root_public_key_fingerprint(root_public_key: bytes) -> bytes:
    """SHA-256(domain || 0x00 || "ed25519" || 0x00 || pk_bytes). 32-byte
    digest; byte layout must match the Rust impl exactly."""
    if len(root_public_key) != 32:
        raise ValueError(f"root pubkey must be 32 bytes, got {len(root_public_key)}")
    buf = (
        ROOT_PUBLIC_KEY_FINGERPRINT_DOMAIN
        + b"\x00"
        + ED25519_ALGORITHM_LABEL
        + b"\x00"
        + root_public_key
    )
    return sha256(buf)
