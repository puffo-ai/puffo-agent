"""Machine self-signed cert + machine-authenticated request/WS signing.

Wire contract with puffo-server's self-contained machine subsystem:
- machine_cert: RFC-8785 canonical, self-signed (verified server-side with
  serde_jcs + ed25519-dalek).
- request auth: headers + signature over ``METHOD\\nPATH\\nTS\\nNONCE\\nbody``.
- WS handshake: signature over ``ws-connect\\n{machine_id}\\n{nonce}\\n{ts}``.
"""

from __future__ import annotations

import time

from ...crypto.canonical import canonicalize_for_signing
from ...crypto.encoding import base64url_encode, generate_nonce
from .store import MachineControlIdentity


def now_ms() -> int:
    return int(time.time() * 1000)


def machine_cert(machine: MachineControlIdentity, hostname: str) -> dict:
    """The machine's self-signed registration cert."""
    cert = {
        "kind": "machine_cert",
        "machine_id": machine.machine_id,
        "signing_public_key": machine.control_pubkey,
        "kem_public_key": machine.kem_pubkey,
        "hostname": hostname,
        "issued_at": now_ms(),
        "signature": "",
    }
    sig = machine.signing_keypair().sign(canonicalize_for_signing(cert))
    cert["signature"] = base64url_encode(sig)
    return cert


def signed_headers(
    machine: MachineControlIdentity, method: str, path: str, body: bytes = b""
) -> dict[str, str]:
    """Headers authenticating a machine HTTP request."""
    ts = now_ms()
    nonce = generate_nonce()
    message = f"{method}\n{path}\n{ts}\n{nonce}\n".encode() + body
    sig = machine.signing_keypair().sign(message)
    return {
        "x-puffo-machine-id": machine.machine_id,
        "x-puffo-timestamp": str(ts),
        "x-puffo-nonce": nonce,
        "x-puffo-signature": base64url_encode(sig),
    }


def ws_connect_frame(machine: MachineControlIdentity) -> dict:
    """The control-WS handshake frame the machine sends first."""
    ts = now_ms()
    nonce = generate_nonce()
    message = f"ws-connect\n{machine.machine_id}\n{nonce}\n{ts}".encode()
    sig = machine.signing_keypair().sign(message)
    return {
        "type": "connect",
        "machine_id": machine.machine_id,
        "ts": ts,
        "nonce": nonce,
        "signature": base64url_encode(sig),
    }
