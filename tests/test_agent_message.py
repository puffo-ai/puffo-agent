"""Unit tests for the machine→operator reverse-channel envelope component."""

from __future__ import annotations

import json

from puffo_agent.crypto.canonical import canonicalize_for_signing
from puffo_agent.crypto.encoding import base64url_decode, base64url_encode
from puffo_agent.crypto.primitives import (
    Ed25519KeyPair,
    KemKeyPair,
    aead_decrypt,
    ed25519_verify,
    hpke_open,
)
from puffo_agent.portal.control.agent_message import (
    MACHINE_MSG_HPKE_INFO,
    Recipient,
    build_machine_message_envelope,
    recipients_from_device_list,
)
from puffo_agent.portal.control.store import MachineControlIdentity


def _machine() -> MachineControlIdentity:
    signing = Ed25519KeyPair.generate()
    kem = KemKeyPair.generate()
    return MachineControlIdentity(
        machine_id="mac_test",
        signing_secret=base64url_encode(signing.secret_bytes()),
        kem_secret=base64url_encode(kem.secret_bytes()),
    )


def _device_cert(root: Ed25519KeyPair, device_id: str, kem_pub: bytes) -> dict:
    cert = {
        "type": "device_cert",
        "version": 1,
        "device_id": device_id,
        "root_public_key": base64url_encode(root.public_key_bytes()),
        "keys": {
            "signing": {"algorithm": "ed25519", "public_key": base64url_encode(b"\x01" * 32)},
            "encryption": {"algorithm": "x25519", "public_key": base64url_encode(kem_pub)},
        },
        "issued_at": 1,
        "signature": "",
    }
    cert["signature"] = base64url_encode(root.sign(canonicalize_for_signing(cert)))
    return cert


def test_envelope_roundtrip_decrypts_for_recipient():
    machine = _machine()
    dev_kem = KemKeyPair.generate()
    recip = Recipient(device_id="dev_1", kem_public_key=dev_kem.public_key_bytes())
    payload = {"type": "agent.status", "agent_slug": "scout", "event": "tool_use",
               "payload": {"tool": "Bash"}}

    env = build_machine_message_envelope(machine, [recip], payload)
    mid = env["message_id"]
    entry = next(r for r in env["recipients"] if r["device_id"] == "dev_1")

    content_key = hpke_open(
        dev_kem,
        base64url_decode(entry["hpke_enc"]),
        MACHINE_MSG_HPKE_INFO,
        f"{mid}:dev_1".encode(),
        base64url_decode(entry["wrapped_content_key"]),
    )
    plaintext = aead_decrypt(
        content_key,
        base64url_decode(env["nonce"]),
        base64url_decode(env["ciphertext"]),
        mid.encode(),
    )
    assert json.loads(plaintext) == payload


def test_envelope_machine_signature_verifies():
    machine = _machine()
    dev_kem = KemKeyPair.generate()
    env = build_machine_message_envelope(
        machine,
        [Recipient("dev_1", dev_kem.public_key_bytes())],
        {"type": "agent.status", "agent_slug": "a", "event": "turn_complete"},
    )
    assert ed25519_verify(
        machine.signing_keypair().public_key_bytes(),
        canonicalize_for_signing(env),
        base64url_decode(env["signature"]),
    )


def test_envelope_seals_to_every_recipient():
    machine = _machine()
    kems = [KemKeyPair.generate() for _ in range(3)]
    recips = [Recipient(f"dev_{i}", k.public_key_bytes()) for i, k in enumerate(kems)]
    env = build_machine_message_envelope(machine, recips, {"x": 1})
    assert len(env["recipients"]) == 3
    assert {r["device_id"] for r in env["recipients"]} == {"dev_0", "dev_1", "dev_2"}


def test_recipients_keeps_only_certs_chaining_to_pinned_root():
    root = Ed25519KeyPair.generate()
    other_root = Ed25519KeyPair.generate()
    dev_kem = KemKeyPair.generate()
    cert = _device_cert(root, "dev_1", dev_kem.public_key_bytes())

    # Chains to the pinned root → kept.
    keep = recipients_from_device_list(
        [{"device_cert": cert, "is_active": True}],
        base64url_encode(root.public_key_bytes()),
    )
    assert len(keep) == 1
    assert keep[0].device_id == "dev_1"
    assert keep[0].kem_public_key == dev_kem.public_key_bytes()

    # Pinned to a different root → dropped (relay can't inject a recipient).
    assert recipients_from_device_list(
        [{"device_cert": cert, "is_active": True}],
        base64url_encode(other_root.public_key_bytes()),
    ) == []


def test_recipients_drops_tampered_signature():
    root = Ed25519KeyPair.generate()
    dev_kem = KemKeyPair.generate()
    cert = _device_cert(root, "dev_1", dev_kem.public_key_bytes())
    cert["signature"] = base64url_encode(b"\x00" * 64)
    assert recipients_from_device_list(
        [{"device_cert": cert, "is_active": True}],
        base64url_encode(root.public_key_bytes()),
    ) == []


def test_build_rejects_no_recipients():
    import pytest

    with pytest.raises(ValueError):
        build_machine_message_envelope(_machine(), [], {"x": 1})
