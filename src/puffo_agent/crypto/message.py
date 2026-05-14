from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from .canonical import canonicalize_for_signing
from .encoding import base64url_decode, base64url_encode
from .primitives import (
    MESSAGE_HPKE_INFO,
    Ed25519KeyPair,
    KemKeyPair,
    aead_decrypt,
    aead_encrypt,
    ed25519_verify,
    generate_aead_nonce,
    generate_content_key,
    hpke_open,
    hpke_seal,
)
from .v2_aad import compute_outer_aad, compute_wrap_aad


@dataclass
class RecipientDevice:
    device_id: str
    kem_public_key: bytes  # 32 bytes


@dataclass
class EncryptInput:
    envelope_kind: str  # "channel" or "dm"
    sender_slug: str
    sender_subkey_id: str
    is_visible_to_human: bool
    space_id: Optional[str] = None
    channel_id: Optional[str] = None
    recipient_slug: Optional[str] = None
    thread_root_id: Optional[str] = None
    reply_to_id: Optional[str] = None
    content_type: str = "text/plain"
    content: Any = None
    recipients: list[RecipientDevice] | None = None


@dataclass
class MessagePayload:
    """In-memory envelope+payload bundle.

    ``envelope_id`` is held on this dataclass for downstream
    consumers but NOT serialized into the inner plaintext JSON —
    the wire format binds the envelope id via the wrap + outer AAD,
    and repeating it inside the encrypted payload would diverge
    from the canonical bytes produced by the Rust server.
    """

    payload_type: str
    version: int
    envelope_id: str
    envelope_kind: str
    sender_slug: str
    sender_subkey_id: str
    sent_at: int
    message_nonce: str
    content_type: str
    content: Any
    is_visible_to_human: bool
    space_id: Optional[str] = None
    channel_id: Optional[str] = None
    recipient_slug: Optional[str] = None
    thread_root_id: Optional[str] = None
    reply_to_id: Optional[str] = None

    def to_payload_dict(self) -> dict:
        """JSON shape matching the server's ``MessagePayload``.

        Optional fields MUST serialize as ``null`` when unset (not
        omitted): the server's verified-payload validator calls
        ``expect_null`` on the inactive route fields per envelope
        kind, so omitting them returns InvalidInput.
        """
        return {
            "type": self.payload_type,
            "version": self.version,
            "envelope_kind": self.envelope_kind,
            "sender_slug": self.sender_slug,
            "sender_subkey_id": self.sender_subkey_id,
            "space_id": self.space_id,
            "channel_id": self.channel_id,
            "recipient_slug": self.recipient_slug,
            "sent_at": self.sent_at,
            "message_nonce": self.message_nonce,
            "thread_root_id": self.thread_root_id,
            "reply_to_id": self.reply_to_id,
            "content_type": self.content_type,
            "content": self.content,
            "is_visible_to_human": self.is_visible_to_human,
        }


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def encrypt_message(
    inp: EncryptInput,
    signing_key: Ed25519KeyPair,
    *,
    now_ms: int | None = None,
) -> dict:
    if not inp.recipients:
        raise ValueError("no recipients")

    # EnvelopeId MUST be ``msg_<UUID>`` — server's prefix validator
    # rejects anything else before crypto runs.
    envelope_id = f"msg_{uuid.uuid4()}"
    message_nonce = base64url_encode(os.urandom(16))

    if now_ms is None:
        now_ms = _now_ms()

    payload = MessagePayload(
        payload_type="message_payload",
        version=1,
        envelope_id=envelope_id,
        envelope_kind=inp.envelope_kind,
        sender_slug=inp.sender_slug,
        sender_subkey_id=inp.sender_subkey_id,
        space_id=inp.space_id,
        channel_id=inp.channel_id,
        recipient_slug=inp.recipient_slug,
        sent_at=now_ms,
        message_nonce=message_nonce,
        thread_root_id=inp.thread_root_id,
        reply_to_id=inp.reply_to_id,
        content_type=inp.content_type,
        content=inp.content,
        is_visible_to_human=inp.is_visible_to_human,
    )

    payload_dict = payload.to_payload_dict()
    canonical = canonicalize_for_signing(payload_dict)
    sig = signing_key.sign(canonical)

    signed = {
        "payload": payload_dict,
        "signature": base64url_encode(sig),
    }
    plaintext = json.dumps(signed, separators=(",", ":")).encode()

    content_key = generate_content_key()
    nonce = generate_aead_nonce()

    outer_aad = compute_outer_aad(
        envelope_id=envelope_id,
        envelope_kind=inp.envelope_kind,
        sender_slug=inp.sender_slug,
        sent_at_ms=now_ms,
        space_id=inp.space_id,
        channel_id=inp.channel_id,
        recipient_slug=inp.recipient_slug,
    )

    ciphertext = aead_encrypt(content_key, nonce, plaintext, outer_aad)

    recipient_entries = []
    for device in inp.recipients:
        wrap_aad = compute_wrap_aad(envelope_id, device.device_id)
        hpke_out = hpke_seal(
            device.kem_public_key, MESSAGE_HPKE_INFO, wrap_aad, content_key,
        )
        recipient_entries.append({
            "device_id": device.device_id,
            "hpke_enc": base64url_encode(hpke_out.enc),
            "wrapped_content_key": base64url_encode(hpke_out.ciphertext),
        })

    # space_id / channel_id / recipient_slug MUST always be present
    # (``null`` for the inactive route) — same canonical-bytes
    # constraint as the inner payload.
    envelope: dict[str, Any] = {
        "type": "message_envelope",
        "version": 1,
        "envelope_id": envelope_id,
        "envelope_kind": inp.envelope_kind,
        "sender_slug": inp.sender_slug,
        "sent_at": now_ms,
        "space_id": inp.space_id,
        "channel_id": inp.channel_id,
        "recipient_slug": inp.recipient_slug,
        "content_nonce": base64url_encode(nonce),
        "content_ciphertext": base64url_encode(ciphertext),
        "recipients": recipient_entries,
    }

    return envelope


def decrypt_message(
    envelope: dict,
    device_id: str,
    kem_keypair: KemKeyPair,
    sender_public_key: bytes,
) -> MessagePayload:
    recipients = envelope.get("recipients", [])
    entry = None
    for r in recipients:
        if r["device_id"] == device_id:
            entry = r
            break
    if entry is None:
        raise ValueError(f"no recipient entry for device {device_id}")

    envelope_id = envelope["envelope_id"]
    envelope_kind = envelope.get("envelope_kind", "channel")

    enc = base64url_decode(entry["hpke_enc"])
    wrapped_key = base64url_decode(entry["wrapped_content_key"])
    wrap_aad = compute_wrap_aad(envelope_id, device_id)
    content_key = hpke_open(kem_keypair, enc, MESSAGE_HPKE_INFO, wrap_aad, wrapped_key)

    if len(content_key) != 32:
        raise ValueError(f"invalid content key length: expected 32, got {len(content_key)}")

    nonce = base64url_decode(envelope["content_nonce"])
    if len(nonce) != 12:
        raise ValueError(f"invalid nonce length: expected 12, got {len(nonce)}")

    ct = base64url_decode(envelope["content_ciphertext"])

    outer_aad = compute_outer_aad(
        envelope_id=envelope_id,
        envelope_kind=envelope_kind,
        sender_slug=envelope["sender_slug"],
        sent_at_ms=envelope["sent_at"],
        space_id=envelope.get("space_id"),
        channel_id=envelope.get("channel_id"),
        recipient_slug=envelope.get("recipient_slug"),
    )

    plaintext = aead_decrypt(content_key, nonce, ct, outer_aad)

    signed = json.loads(plaintext)
    payload_dict = signed["payload"]
    sig_bytes = base64url_decode(signed["signature"])

    canonical = canonicalize_for_signing(payload_dict)
    if not ed25519_verify(sender_public_key, canonical, sig_bytes):
        raise ValueError("signature verification failed")

    # No envelope_id check needed — the inner payload doesn't carry
    # one, and a tampered envelope_id flips the outer AAD so AEAD-
    # open fails before we reach this point.
    if payload_dict.get("sender_slug") != envelope["sender_slug"]:
        raise ValueError("sender_slug mismatch")

    return MessagePayload(
        payload_type=payload_dict["type"],
        version=payload_dict["version"],
        envelope_id=envelope_id,
        envelope_kind=payload_dict["envelope_kind"],
        sender_slug=payload_dict["sender_slug"],
        sender_subkey_id=payload_dict["sender_subkey_id"],
        space_id=payload_dict.get("space_id"),
        channel_id=payload_dict.get("channel_id"),
        recipient_slug=payload_dict.get("recipient_slug"),
        sent_at=payload_dict["sent_at"],
        message_nonce=payload_dict["message_nonce"],
        thread_root_id=payload_dict.get("thread_root_id"),
        reply_to_id=payload_dict.get("reply_to_id"),
        content_type=payload_dict["content_type"],
        content=payload_dict["content"],
        is_visible_to_human=payload_dict.get("is_visible_to_human", True),
    )
