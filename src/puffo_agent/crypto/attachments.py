"""Per-attachment encrypt/decrypt helpers, wire-compatible with
``client/web/src/message/attachments.ts``.

Each attachment carries its own ChaCha20-Poly1305 32-byte key and
12-byte nonce. AAD layout: ``puffo/attachment/v1`` || 0x00 ||
mime_type || 0x00 || filename. Server only ever sees opaque
ciphertext via /blobs/upload + /blobs/{id} (already SubkeyAuth'd).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .encoding import base64url_decode, base64url_encode
from .primitives import aead_decrypt, aead_encrypt

ATTACHMENT_AAD_LABEL = b"puffo/attachment/v1"
ATTACHMENT_CONTENT_TYPE = "puffo/message+attachments/v1"


@dataclass
class AttachmentMeta:
    blob_id: str
    filename: str
    mime_type: str
    size: int
    key: str  # base64url, 32 bytes
    nonce: str  # base64url, 12 bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "blob_id": self.blob_id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "key": self.key,
            "nonce": self.nonce,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AttachmentMeta":
        return AttachmentMeta(
            blob_id=str(d["blob_id"]),
            filename=str(d["filename"]),
            mime_type=str(d["mime_type"]),
            size=int(d["size"]),
            key=str(d["key"]),
            nonce=str(d["nonce"]),
        )


def build_aad(filename: str, mime_type: str) -> bytes:
    """label || 0x00 || mime || 0x00 || filename. Must match the
    web client's ``buildAad`` byte-for-byte; any drift fails the
    AEAD tag check on the receive side."""
    return (
        ATTACHMENT_AAD_LABEL
        + b"\x00"
        + mime_type.encode("utf-8")
        + b"\x00"
        + filename.encode("utf-8")
    )


def encrypt_attachment(
    *,
    plaintext: bytes,
    filename: str,
    mime_type: str,
    blob_id: str,
) -> tuple[bytes, AttachmentMeta]:
    """Generate a fresh key + nonce, AEAD-seal ``plaintext``, and
    return ``(ciphertext, meta)``. Caller uploads ciphertext to
    /blobs/upload; ``blob_id`` is accepted here so the returned meta
    is fully populated.
    """
    key = os.urandom(32)
    nonce = os.urandom(12)
    aad = build_aad(filename, mime_type)
    ciphertext = aead_encrypt(key, nonce, plaintext, aad)
    meta = AttachmentMeta(
        blob_id=blob_id,
        filename=filename,
        mime_type=mime_type,
        size=len(plaintext),
        key=base64url_encode(key),
        nonce=base64url_encode(nonce),
    )
    return ciphertext, meta


def decrypt_attachment(ciphertext: bytes, meta: AttachmentMeta) -> bytes:
    """Inverse of ``encrypt_attachment`` — pulls the key + nonce out
    of meta, runs AEAD-open with the canonical AAD."""
    key = base64url_decode(meta.key)
    nonce = base64url_decode(meta.nonce)
    aad = build_aad(meta.filename, meta.mime_type)
    return aead_decrypt(key, nonce, ciphertext, aad)
