"""Machine-side control crypto: cert pinning + command envelope round-trip.

Simulates the operator (web client) signing a control cert + command
envelope, and asserts the machine verifies, decrypts, and rejects tampering.
"""

from __future__ import annotations

import json

import pytest

from puffo_agent.crypto.canonical import canonicalize_for_signing
from puffo_agent.crypto.encoding import base64url_encode
from puffo_agent.crypto.primitives import Ed25519KeyPair, hpke_seal
from puffo_agent.portal.control import store
from puffo_agent.portal.control.envelope import (
    PORTAL_CMD_INFO,
    ControlError,
    decrypt_command,
    verify_control_cert,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    return tmp_path


def _sign(operator: Ed25519KeyPair, obj: dict) -> dict:
    obj = dict(obj)
    obj["signature"] = base64url_encode(operator.sign(canonicalize_for_signing(obj)))
    return obj


def _control_cert(operator: Ed25519KeyPair, machine) -> dict:
    op_root = base64url_encode(operator.public_key_bytes())
    return _sign(
        operator,
        {
            "kind": "machine_control_cert",
            "machine_id": machine.machine_id,
            "control_public_key": machine.control_pubkey,
            "control_kem_public_key": machine.kem_pubkey,
            "operator_root_public_key": op_root,
            "name": "MacBook",
            "issued_at": 1,
        },
    )


def _command(operator: Ed25519KeyPair, machine, command_id: str, op: str, ts: int) -> dict:
    body = json.dumps({"op": op, "params": {"foo": "bar"}}).encode("utf-8")
    sealed = hpke_seal(
        machine.kem_keypair().public_key_bytes(),
        PORTAL_CMD_INFO,
        command_id.encode("utf-8"),
        body,
    )
    return _sign(
        operator,
        {
            "v": 1,
            "command_id": command_id,
            "to_machine_id": machine.machine_id,
            "agent_slug": "scout-0001",
            "ts": ts,
            "nonce": "nonce123",
            "hpke_enc": base64url_encode(sealed.enc),
            "ciphertext": base64url_encode(sealed.ciphertext),
        },
    )


def test_machine_identity_is_stable(home):
    a = store.load_or_create_machine()
    b = store.load_or_create_machine()
    assert a.machine_id == b.machine_id
    assert a.machine_id.startswith("mac_")


def test_control_cert_verifies_and_pins_operator(home):
    machine = store.load_or_create_machine()
    operator = Ed25519KeyPair.generate()
    cert = _control_cert(operator, machine)
    op_root = verify_control_cert(cert, machine.machine_id, machine.control_pubkey)
    assert op_root == base64url_encode(operator.public_key_bytes())


def test_control_cert_wrong_machine_rejected(home):
    machine = store.load_or_create_machine()
    operator = Ed25519KeyPair.generate()
    cert = _control_cert(operator, machine)
    cert["machine_id"] = "dev_someoneelse"
    with pytest.raises(ControlError):
        verify_control_cert(cert, machine.machine_id, machine.control_pubkey)


def test_command_round_trip(home):
    machine = store.load_or_create_machine()
    operator = Ed25519KeyPair.generate()
    op_root = base64url_encode(operator.public_key_bytes())
    ts = store.now_ms()
    env = _command(operator, machine, "cmd_1", "pause", ts)
    out = decrypt_command(env, machine, op_root, ts)
    assert out["op"] == "pause"
    assert out["command_id"] == "cmd_1"
    assert out["agent_slug"] == "scout-0001"
    assert out["params"] == {"foo": "bar"}


def test_forged_command_signature_rejected(home):
    machine = store.load_or_create_machine()
    operator = Ed25519KeyPair.generate()
    attacker = Ed25519KeyPair.generate()
    op_root = base64url_encode(operator.public_key_bytes())
    ts = store.now_ms()
    # Signed by the attacker, not the pinned operator → must be rejected.
    env = _command(attacker, machine, "cmd_2", "archive", ts)
    with pytest.raises(ControlError):
        decrypt_command(env, machine, op_root, ts)


def test_stale_command_rejected(home):
    machine = store.load_or_create_machine()
    operator = Ed25519KeyPair.generate()
    op_root = base64url_encode(operator.public_key_bytes())
    ts = store.now_ms()
    env = _command(operator, machine, "cmd_3", "pause", ts)
    # 10 minutes later → outside the ±5min window.
    with pytest.raises(ControlError):
        decrypt_command(env, machine, op_root, ts + 10 * 60 * 1000)


def test_pairing_persist_round_trip(home):
    p = store.ControlPairing(
        operator_slug="operator-0001",
        operator_root_pubkey="oprootpk",
        control_cert={"kind": "machine_control_cert"},
        server_url="http://localhost:3000",
        name="MacBook",
        created_at=store.now_ms(),
    )
    store.save_pairing(p)
    assert store.get_pairing("operator-0001").operator_root_pubkey == "oprootpk"
    assert store.delete_pairing("operator-0001") is True
    assert store.get_pairing("operator-0001") is None
