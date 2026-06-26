"""E2E control envelope crypto (machine side).

Defines the wire contract shared with the web client:

control cert (operator signs in browser, machine verifies + pins)::

    { kind, machine_id, control_public_key, control_kem_public_key,
      operator_root_public_key, name, issued_at, signature }

command envelope (operator → machine)::

    { v, command_id, to_machine_id, agent_slug, ts, nonce,
      hpke_enc, ciphertext, signature }

The signature is Ed25519 by the operator root over the RFC-8785 canonical
form of the object minus its ``signature`` field. ``ciphertext`` is the HPKE
seal (to the machine control KEM key) of the plaintext body ``{op, params}``.
"""

from __future__ import annotations

import json

from ...crypto.canonical import canonicalize_for_signing
from ...crypto.encoding import base64url_decode
from ...crypto.primitives import ed25519_verify, hpke_open
from .store import MachineControlIdentity

PORTAL_CMD_INFO = b"puffo/portal-cmd/v1"
# Commands older than this (or this far in the future) are rejected.
TS_WINDOW_MS = 5 * 60 * 1000


class ControlError(Exception):
    """A control cert / command envelope failed verification."""


def verify_control_cert(
    cert: dict, expected_machine_id: str, expected_control_pubkey: str
) -> str:
    """Verify an operator-signed machine control cert and return the operator
    root pubkey (b64url) to pin. Raises ``ControlError`` on any mismatch."""
    if not isinstance(cert, dict):
        raise ControlError("control cert must be an object")
    op_root = cert.get("operator_root_public_key")
    sig = cert.get("signature")
    if not isinstance(op_root, str) or not isinstance(sig, str):
        raise ControlError("control cert missing operator_root_public_key/signature")
    if cert.get("machine_id") != expected_machine_id:
        raise ControlError("control cert machine_id mismatch")
    if cert.get("control_public_key") != expected_control_pubkey:
        raise ControlError("control cert control_public_key mismatch")
    try:
        ok = ed25519_verify(
            base64url_decode(op_root),
            canonicalize_for_signing(cert),
            base64url_decode(sig),
        )
    except Exception as exc:  # noqa: BLE001 — any decode/verify failure is a bad cert
        raise ControlError(f"control cert signature error: {exc}") from exc
    if not ok:
        raise ControlError("control cert signature does not verify")
    return op_root


def decrypt_command(
    envelope: dict, machine: MachineControlIdentity, operator_root_pubkey: str, now_ms: int
) -> dict:
    """Verify the operator signature + freshness, HPKE-open the body, and
    return ``{command_id, agent_slug, op, params}``. Raises ``ControlError``."""
    if not isinstance(envelope, dict):
        raise ControlError("envelope must be an object")
    sig = envelope.get("signature")
    if not isinstance(sig, str):
        raise ControlError("envelope missing signature")

    try:
        ok = ed25519_verify(
            base64url_decode(operator_root_pubkey),
            canonicalize_for_signing(envelope),
            base64url_decode(sig),
        )
    except Exception as exc:  # noqa: BLE001
        raise ControlError(f"envelope signature error: {exc}") from exc
    if not ok:
        # Signature mismatch = treat as forged; never execute.
        raise ControlError("envelope signature does not verify")

    ts = envelope.get("ts")
    if not isinstance(ts, int) or abs(now_ms - ts) > TS_WINDOW_MS:
        raise ControlError("envelope timestamp outside window")

    command_id = envelope.get("command_id")
    hpke_enc = envelope.get("hpke_enc")
    ciphertext = envelope.get("ciphertext")
    if not (isinstance(command_id, str) and isinstance(hpke_enc, str) and isinstance(ciphertext, str)):
        raise ControlError("envelope missing command_id/hpke_enc/ciphertext")

    try:
        plaintext = hpke_open(
            machine.kem_keypair(),
            base64url_decode(hpke_enc),
            PORTAL_CMD_INFO,
            command_id.encode("utf-8"),
            base64url_decode(ciphertext),
        )
        body = json.loads(plaintext)
    except Exception as exc:  # noqa: BLE001
        raise ControlError(f"envelope decrypt error: {exc}") from exc

    op = body.get("op")
    if not isinstance(op, str):
        raise ControlError("command body missing op")
    return {
        "command_id": command_id,
        "agent_slug": envelope.get("agent_slug"),
        "op": op,
        "params": body.get("params") or {},
    }
