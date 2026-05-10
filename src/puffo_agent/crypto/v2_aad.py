"""Byte-exact mirrors of the server's AAD helpers.

Any byte-level drift produces AEAD tag-mismatch on the receive
side; treat every constant and layout in this module as part of
the wire format."""

from __future__ import annotations

from typing import Optional

# AAD labels. Must match the server's domain constants exactly.
MESSAGE_OUTER_AAD_LABEL = b"puffo/message-envelope-outer/v1"
MESSAGE_WRAP_AAD_LABEL = b"puffo/message-envelope-wrap/v1"
ROOT_KEY_ENVELOPE_AAD_LABEL = b"puffo/root-key-envelope/v1"

# Envelope-kind discriminator (single byte).
_ENVELOPE_KIND_CHANNEL = 0x01
_ENVELOPE_KIND_DM = 0x02


def _non_empty_utf8(value: str) -> bytes:
    if not value:
        raise ValueError("non_empty_utf8: value is empty")
    return value.encode("utf-8")


def _len_prefixed_utf8(value: str) -> bytes:
    b = _non_empty_utf8(value)
    if len(b) > 0xFFFF:
        raise ValueError("len_prefixed_utf8: value too long")
    return len(b).to_bytes(2, "big") + b


def _i64_be_from_u64(value: int) -> bytes:
    if value < 0 or value > (1 << 63) - 1:
        raise ValueError("push_i64_be_from_u64: value out of i64 range")
    return value.to_bytes(8, "big", signed=True)


def compute_outer_aad(
    *,
    envelope_id: str,
    envelope_kind: str,
    sender_slug: str,
    sent_at_ms: int,
    space_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    recipient_slug: Optional[str] = None,
) -> bytes:
    """Outer AAD for the message-envelope AEAD seal/open. Layout:
    label || envelope_id || kind_tag (1B) || len-prefixed sender_slug
    || sent_at_ms (i64 BE) || (channel: space_id || channel_id) or
    (dm: len-prefixed recipient_slug)."""
    out = bytearray()
    out += MESSAGE_OUTER_AAD_LABEL
    out += _non_empty_utf8(envelope_id)
    if envelope_kind == "channel":
        out.append(_ENVELOPE_KIND_CHANNEL)
    elif envelope_kind == "dm":
        out.append(_ENVELOPE_KIND_DM)
    else:
        raise ValueError(f"unknown envelope_kind {envelope_kind!r}")
    out += _len_prefixed_utf8(sender_slug)
    out += _i64_be_from_u64(sent_at_ms)
    if envelope_kind == "channel":
        if not space_id or not channel_id:
            raise ValueError("channel envelopes require space_id and channel_id")
        out += _non_empty_utf8(space_id)
        out += _non_empty_utf8(channel_id)
    else:  # dm
        if not recipient_slug:
            raise ValueError("dm envelopes require recipient_slug")
        out += _len_prefixed_utf8(recipient_slug)
    return bytes(out)


def compute_wrap_aad(envelope_id: str, device_id: str) -> bytes:
    """Inner AAD for per-recipient HPKE: label || envelope_id || device_id."""
    return (
        MESSAGE_WRAP_AAD_LABEL
        + _non_empty_utf8(envelope_id)
        + _non_empty_utf8(device_id)
    )


def compute_root_key_envelope_aad(
    enrollment_nonce: bytes,
    recipient_kem_public_key: bytes,
    root_public_key_fingerprint: bytes,
) -> bytes:
    """Enrollment-envelope AAD: label || nonce(32) || kem_pk(32) ||
    root_pk_fingerprint(32). Fixed-width, no separators."""
    return (
        ROOT_KEY_ENVELOPE_AAD_LABEL
        + enrollment_nonce
        + recipient_kem_public_key
        + root_public_key_fingerprint
    )
