"""Receive-side plaintext (non-E2EE) envelope verification."""

import os

import pytest

from puffo_agent.crypto.canonical import canonicalize_for_signing
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.message import read_plaintext_message
from puffo_agent.crypto.primitives import Ed25519KeyPair


def _plaintext_envelope(signing_key: Ed25519KeyPair) -> dict:
    payload = {
        "type": "message_payload",
        "version": 1,
        "envelope_kind": "dm",
        "sender_slug": "alice-0001",
        "sender_subkey_id": "sk_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "space_id": None,
        "channel_id": None,
        "recipient_slug": "bob-0001",
        "sent_at": 1_700_000_000_000,
        "message_nonce": base64url_encode(os.urandom(16)),
        "thread_root_id": None,
        "reply_to_id": None,
        "content_type": "text/plain",
        "content": "hi in the clear",
        "is_visible_to_human": True,
    }
    sig = signing_key.sign(canonicalize_for_signing(payload))
    return {
        "type": "plaintext_message_envelope",
        "version": 1,
        "envelope_id": "msg_00000000-0000-4000-8000-0000000000aa",
        "signed_payload": {"payload": payload, "signature": base64url_encode(sig)},
    }


def test_reads_valid_plaintext_envelope() -> None:
    signing_key = Ed25519KeyPair.generate()
    env = _plaintext_envelope(signing_key)

    payload = read_plaintext_message(env, signing_key.public_key_bytes())

    assert payload.envelope_id == env["envelope_id"]
    assert payload.envelope_kind == "dm"
    assert payload.sender_slug == "alice-0001"
    assert payload.recipient_slug == "bob-0001"
    assert payload.content == "hi in the clear"
    assert payload.is_visible_to_human is True


def test_rejects_wrong_signer() -> None:
    signing_key = Ed25519KeyPair.generate()
    wrong_key = Ed25519KeyPair.generate()
    env = _plaintext_envelope(signing_key)

    with pytest.raises(ValueError, match="signature verification failed"):
        read_plaintext_message(env, wrong_key.public_key_bytes())


def test_rejects_tampered_payload() -> None:
    signing_key = Ed25519KeyPair.generate()
    env = _plaintext_envelope(signing_key)
    env["signed_payload"]["payload"]["content"] = "tampered"

    with pytest.raises(ValueError, match="signature verification failed"):
        read_plaintext_message(env, signing_key.public_key_bytes())


def test_unknown_payload_field_is_ignored() -> None:
    signing_key = Ed25519KeyPair.generate()
    payload = {
        "type": "message_payload",
        "version": 1,
        "envelope_kind": "channel",
        "sender_slug": "alice-0001",
        "sender_subkey_id": "sk_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "space_id": "sp_00000000-0000-4000-8000-0000000000cc",
        "channel_id": "ch_00000000-0000-4000-8000-0000000000dd",
        "recipient_slug": None,
        "sent_at": 1_700_000_000_000,
        "message_nonce": base64url_encode(os.urandom(16)),
        "content_type": "text/plain",
        "content": "channel in the clear",
        "future_field": {"anything": [1, 2, 3]},
    }
    sig = signing_key.sign(canonicalize_for_signing(payload))
    env = {
        "type": "plaintext_message_envelope",
        "version": 1,
        "envelope_id": "msg_00000000-0000-4000-8000-0000000000bb",
        "signed_payload": {"payload": payload, "signature": base64url_encode(sig)},
    }

    result = read_plaintext_message(env, signing_key.public_key_bytes())
    assert result.channel_id == "ch_00000000-0000-4000-8000-0000000000dd"
    assert result.content == "channel in the clear"
