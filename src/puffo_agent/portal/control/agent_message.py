"""Machine → operator reverse-channel messages (Agent Portal v0.4).

Mirrors the puffo message envelope: one content-key AEAD-seals the Layer-2
payload, the content-key is HPKE-wrapped per operator device, and the machine
signs the canonical envelope. The relay only sees opaque ciphertext. Used to
stream ``agent.status`` (LLM logs) to the agent's owner operator.

AAD contract (the web receiver must mirror it):
  * content AEAD aad = ``message_id`` (utf-8)
  * per-device wrap aad = ``f"{message_id}:{device_id}"`` (utf-8)
  * HPKE info = ``MACHINE_MSG_HPKE_INFO``
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import aiohttp

from ...crypto.canonical import canonicalize_for_signing
from ...crypto.encoding import base64url_decode, base64url_encode
from ...crypto.primitives import (
    aead_encrypt,
    ed25519_verify,
    generate_aead_nonce,
    generate_content_key,
    hpke_seal,
)
from . import machine_auth
from .store import MachineControlIdentity, now_ms

MACHINE_MSG_HPKE_INFO = b"puffo/machine-msg/v1"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)


@dataclass
class Recipient:
    device_id: str
    kem_public_key: bytes


def recipients_from_device_list(
    devices: list[dict], operator_root_pubkey: str
) -> list[Recipient]:
    """KEM keys from ``/devices/active``, keeping only certs that chain to the
    pinned operator root — so the relay can't inject a rogue recipient."""
    try:
        root_pk = base64url_decode(operator_root_pubkey)
    except Exception:  # noqa: BLE001
        return []
    out: list[Recipient] = []
    for entry in devices:
        cert = entry.get("device_cert") if isinstance(entry, dict) else None
        if not isinstance(cert, dict):
            continue
        if cert.get("root_public_key") != operator_root_pubkey:
            continue
        sig = cert.get("signature")
        if not isinstance(sig, str):
            continue
        try:
            if not ed25519_verify(
                root_pk, canonicalize_for_signing(cert), base64url_decode(sig)
            ):
                continue
            out.append(
                Recipient(
                    device_id=cert["device_id"],
                    kem_public_key=base64url_decode(
                        cert["keys"]["encryption"]["public_key"]
                    ),
                )
            )
        except Exception:  # noqa: BLE001 — skip any malformed cert
            continue
    return out


def build_machine_message_envelope(
    machine: MachineControlIdentity,
    recipients: list[Recipient],
    payload: dict,
    *,
    message_id: str | None = None,
    ts: int | None = None,
) -> dict:
    """Layer-1 transport envelope: content-key AEAD over ``payload`` + per-device
    HPKE-wrapped content-key + machine signature."""
    if not recipients:
        raise ValueError("no recipients")
    message_id = message_id or f"mmsg_{uuid.uuid4()}"
    ts = ts if ts is not None else now_ms()

    content_key = generate_content_key()
    nonce = generate_aead_nonce()
    plaintext = json.dumps(payload, separators=(",", ":")).encode()
    ciphertext = aead_encrypt(content_key, nonce, plaintext, message_id.encode())

    recipient_entries = []
    for r in recipients:
        wrap_aad = f"{message_id}:{r.device_id}".encode()
        sealed = hpke_seal(r.kem_public_key, MACHINE_MSG_HPKE_INFO, wrap_aad, content_key)
        recipient_entries.append(
            {
                "device_id": r.device_id,
                "hpke_enc": base64url_encode(sealed.enc),
                "wrapped_content_key": base64url_encode(sealed.ciphertext),
            }
        )

    envelope = {
        "v": 1,
        "machine_id": machine.machine_id,
        "message_id": message_id,
        "ts": ts,
        "nonce": base64url_encode(nonce),
        "ciphertext": base64url_encode(ciphertext),
        "recipients": recipient_entries,
        "signature": "",
    }
    sig = machine.signing_keypair().sign(canonicalize_for_signing(envelope))
    envelope["signature"] = base64url_encode(sig)
    return envelope


async def fetch_active_recipients(
    base_url: str,
    machine: MachineControlIdentity,
    operator_slug: str,
    operator_root_pubkey: str,
) -> list[Recipient]:
    """GET the operator's active device certs (machine-authed) → verified
    recipients. Returns [] on any error so callers can no-op cleanly."""
    path = f"/v2/machines/{machine.machine_id}/operators/{operator_slug}/devices/active"
    headers = machine_auth.signed_headers(machine, "GET", path)
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.get(f"{base_url.rstrip('/')}{path}", headers=headers) as resp:
                if resp.status >= 400:
                    return []
                data = await resp.json()
    except Exception:  # noqa: BLE001
        return []
    devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, list):
        return []
    return recipients_from_device_list(devices, operator_root_pubkey)
