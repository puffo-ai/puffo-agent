"""SignedEvent builder for events posted to /spaces/events."""

from __future__ import annotations

import os
import uuid
from typing import Any

from ..crypto.canonical import canonicalize_for_signing
from ..crypto.encoding import base64url_encode
from ..crypto.primitives import Ed25519KeyPair


def random_event_id() -> str:
    return f"ev_{uuid.uuid4()}"


def random_nonce() -> str:
    """32-byte base64url nonce for event-payload replay protection."""
    return base64url_encode(os.urandom(32))


def sign_event(
    *,
    kind: str,
    payload: dict[str, Any],
    signer_slug: str,
    signer_device_id: str,
    signer_subkey_id: str,
    signing_key: Ed25519KeyPair,
) -> dict[str, Any]:
    """Build + sign a SignedEvent ready to POST to /spaces/events.

    All three signer fields (``slug``, ``device_id``, ``subkey_id``)
    are required by the server's deserializer; omitting any returns
    a 400.
    """
    event = {
        "type": "signed_event",
        "version": 1,
        "event_id": random_event_id(),
        "kind": kind,
        "payload": payload,
        "signer_slug": signer_slug,
        "signer_device_id": signer_device_id,
        "signer_subkey_id": signer_subkey_id,
        "signature": "",
    }
    canonical = canonicalize_for_signing(event)
    sig = signing_key.sign(canonical)
    event["signature"] = base64url_encode(sig)
    return event
